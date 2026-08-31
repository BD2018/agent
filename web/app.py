"""FastAPI Web 应用：提供认证、文件上传、对话、知识库管理等接口。

核心设计：
- JWT 认证：所有 /api/* 接口（除 auth）需 Bearer token。
- 按用户隔离：Agent(user_id)、KnowledgeBase(user_id)、MemoryStore(user_id)。
- 上传写入 Chroma 后 chat 检索立即可见，无需重启。
- /api/chat/stream 为 SSE 流式（打字机效果）。
- 静态页面：/ 返回对话页，/console 返回管理系统，/login 返回登录页。
"""
import asyncio
import json
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import config
from agent.core import Agent
from agent.llm_settings import (
    create_model_config,
    delete_model_config,
    enable_model_config,
    get_llm_settings,
    get_model_config,
    list_model_configs,
    mask_key,
    test_llm_connection,
    update_model_config,
)
from agent.tools import TOOLS
from auth.dependencies import get_current_user, require_permission
from auth.jwt_utils import create_token
from auth.models import (
    ALL_PAGES,
    authenticate,
    create_user,
    delete_user,
    get_user_by_id,
    init_db,
    list_users,
    update_password,
    update_permissions,
)
from knowledge.extractors import SUPPORTED_EXTS
from knowledge.ingest import ingest_file
from knowledge.retriever import get_kb

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Agent 管理系统")

# ---------- 启动初始化 ----------
init_db()  # 创建 users 表 + 预置 admin 账号

# ---------- Agent 缓存 ----------
_agent_cache: dict[int, Agent] = {}


def get_agent(user_id: int) -> Agent:
    if user_id not in _agent_cache:
        _agent_cache[user_id] = Agent(user_id)
    return _agent_cache[user_id]


def user_docs_dir(user_id: int) -> Path:
    d = config.DOCS_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- 静态页面 ----------
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/login")
async def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/")
async def chat_page():
    return FileResponse(str(STATIC_DIR / "chat.html"))


@app.get("/console")
async def console_page():
    return FileResponse(str(STATIC_DIR / "console.html"))


# ---------- 认证 ----------
@app.post("/api/auth/register")
async def register(payload: dict):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")

    user = create_user(username, password)
    if not user:
        raise HTTPException(409, "用户名已存在")

    token = create_token(user["id"], user["username"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
async def login(payload: dict):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()

    user = authenticate(username, password)
    if not user:
        raise HTTPException(401, "用户名或密码错误")

    token = create_token(user["id"], user["username"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return {"user": user}


# ---------- 用户管理（需 users 权限）----------
@app.get("/api/users")
async def users_list(user: dict = Depends(require_permission("users"))):
    return {"users": list_users()}


@app.post("/api/users")
async def users_create(payload: dict, user: dict = Depends(require_permission("users"))):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    permissions = payload.get("permissions")
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if permissions is not None and not isinstance(permissions, list):
        raise HTTPException(400, "permissions 必须是页面 key 数组")
    try:
        new_user = create_user(username, password, permissions)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not new_user:
        raise HTTPException(409, "用户名已存在")
    return {"user": get_user_by_id(new_user["id"])}


@app.put("/api/users/{user_id}/permissions")
async def users_set_permissions(
    user_id: int, payload: dict, user: dict = Depends(require_permission("users"))
):
    """设置用户可见的管理页菜单。admin（id=1）恒为全部权限，不可修改。"""
    if user_id == 1:
        raise HTTPException(400, "管理员权限不可修改")
    permissions = payload.get("permissions")
    if not isinstance(permissions, list):
        raise HTTPException(400, "permissions 必须是页面 key 数组")
    if not update_permissions(user_id, permissions):
        raise HTTPException(404, "用户不存在")
    return {"ok": True, "user": get_user_by_id(user_id)}


@app.delete("/api/users/{user_id}")
async def users_delete(user_id: int, user: dict = Depends(require_permission("users"))):
    if user_id == 1:
        raise HTTPException(400, "不能删除管理员账号")
    if not delete_user(user_id):
        raise HTTPException(404, "用户不存在或无法删除")
    # 清除该用户的 Agent 缓存
    if user_id in _agent_cache:
        del _agent_cache[user_id]
    return {"ok": True}


@app.put("/api/users/{user_id}/password")
async def users_reset_password(
    user_id: int, payload: dict, user: dict = Depends(require_permission("users"))
):
    new_password = (payload.get("password") or "").strip()
    if len(new_password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if not update_password(user_id, new_password):
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


# ---------- LLM 模型设置（需 llm 权限）----------
@app.get("/api/settings/check")
async def llm_settings_check(user: dict = Depends(get_current_user)):
    """轻量检查：是否已配置 API Key。所有登录用户可访问。"""
    cfg = get_llm_settings()
    return {"has_api_key": cfg["has_api_key"]}


# ---------- LLM 模型管理（需 llm 权限）----------
@app.get("/api/settings/check")
async def llm_settings_check(user: dict = Depends(get_current_user)):
    """轻量检查：是否已配置 API Key。所有登录用户可访问。"""
    cfg = get_llm_settings()
    return {"has_api_key": cfg["has_api_key"]}


@app.get("/api/models")
async def models_list(user: dict = Depends(require_permission("llm"))):
    """返回所有模型配置列表。"""
    return {"models": list_model_configs()}


@app.post("/api/models")
async def models_create(payload: dict, user: dict = Depends(require_permission("llm"))):
    """新增模型配置。"""
    name = (payload.get("name") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    model = (payload.get("model") or "").strip()
    try:
        created = create_model_config(name, base_url, api_key, model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"model": created}


@app.get("/api/models/{model_id}")
async def models_get(model_id: int, user: dict = Depends(require_permission("llm"))):
    """返回单个模型配置详情。"""
    cfg = get_model_config(model_id)
    if not cfg:
        raise HTTPException(404, "模型配置不存在")
    return {"model": cfg}


@app.put("/api/models/{model_id}")
async def models_update(
    model_id: int, payload: dict, user: dict = Depends(require_permission("llm"))
):
    """更新模型配置。api_key 留空表示保持现有 Key 不变。"""
    name = (payload.get("name") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    model = (payload.get("model") or "").strip()
    try:
        updated = update_model_config(model_id, name, base_url, api_key, model)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not updated:
        raise HTTPException(404, "模型配置不存在")
    return {"model": updated}


@app.delete("/api/models/{model_id}")
async def models_delete(model_id: int, user: dict = Depends(require_permission("llm"))):
    """删除模型配置。不能删除当前启用的模型。"""
    try:
        if not delete_model_config(model_id):
            raise HTTPException(404, "模型配置不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/models/{model_id}/enable")
async def models_enable(model_id: int, user: dict = Depends(require_permission("llm"))):
    """启用指定模型配置，自动禁用其他。"""
    cfg = enable_model_config(model_id)
    if not cfg:
        raise HTTPException(404, "模型配置不存在")
    # 清除 Agent 缓存，让下次对话使用新模型
    _agent_cache.clear()
    return {"ok": True, "model": cfg}


@app.post("/api/models/test")
async def models_test(payload: dict, user: dict = Depends(require_permission("llm"))):
    """测试 LLM 连通性。可传 model_id 使用已保存的 Key，或直接传 api_key。"""
    base_url = (payload.get("base_url") or "").strip()
    model = (payload.get("model") or "").strip()
    api_key = (payload.get("api_key") or "").strip()
    model_id = payload.get("model_id")
    if not api_key and model_id:
        from agent.llm_settings import _get_conn, decrypt
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT api_key FROM model_configs WHERE id = ?", (model_id,)
            ).fetchone()
        if row and row["api_key"]:
            api_key = decrypt(row["api_key"])
    ok, message = await asyncio.to_thread(test_llm_connection, base_url, api_key, model)
    return {"ok": ok, "message": message}


# ---------- 文件上传与知识库管理 ----------
@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    uid = user["id"]
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(400, f"不支持的格式: {ext}（支持：{'/'.join(SUPPORTED_EXTS)}）")

    dest = user_docs_dir(uid) / filename
    content = await file.read()
    dest.write_bytes(content)

    try:
        chunks = await asyncio.to_thread(ingest_file, dest, uid)
    except Exception as e:
        raise HTTPException(500, f"入库失败: {e}")

    return {"filename": filename, "chunks": chunks, "total": get_kb(uid).count()}


@app.get("/api/docs")
async def list_docs(user: dict = Depends(get_current_user)):
    uid = user["id"]
    kb = get_kb(uid)
    return {"docs": kb.list_sources(), "total": kb.count()}


@app.delete("/api/docs/{filename}")
async def remove_doc(filename: str, user: dict = Depends(get_current_user)):
    uid = user["id"]
    path = user_docs_dir(uid) / filename
    kb = get_kb(uid)
    kb.delete_by_source(filename)
    if path.exists():
        path.unlink()
    return {"deleted": filename, "total": kb.count()}


@app.get("/api/docs/download/{filename}")
async def download_doc(filename: str, user: dict = Depends(get_current_user)):
    uid = user["id"]
    path = user_docs_dir(uid) / filename
    if not path.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(path), filename=filename)


# ---------- 对话 ----------
@app.post("/api/chat")
async def chat(payload: dict, user: dict = Depends(get_current_user)):
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")

    agent = get_agent(user["id"])
    events: list[str] = []

    def on_event(e):
        events.append(e)

    try:
        answer = await asyncio.to_thread(agent.chat, message, on_event)
    except Exception as e:
        raise HTTPException(500, f"对话失败: {e}")

    return {"answer": answer, "events": events}


@app.post("/api/chat/stream")
async def chat_stream(payload: dict, user: dict = Depends(get_current_user)):
    message = (payload.get("message") or "").strip()
    if not message:
        raise HTTPException(400, "消息不能为空")

    agent = get_agent(user["id"])

    def generate():
        try:
            for event_type, content in agent.chat_stream(message):
                data = json.dumps(
                    {"type": event_type, "content": content},
                    ensure_ascii=False,
                )
                yield f"data: {data}\n\n"
        except Exception as e:
            err = json.dumps({"type": "error", "content": str(e)}, ensure_ascii=False)
            yield f"data: {err}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def history(user: dict = Depends(get_current_user)):
    agent = get_agent(user["id"])
    return {"messages": agent.memory.load_recent(100)}


@app.post("/api/reset")
async def reset(user: dict = Depends(get_current_user)):
    agent = get_agent(user["id"])
    agent.reset()
    return {"ok": True}


@app.get("/api/status")
async def status(user: dict = Depends(get_current_user)):
    uid = user["id"]
    return {"status": "ok", "chunks": get_kb(uid).count()}


@app.get("/api/chunks")
async def list_chunks(user: dict = Depends(get_current_user)):
    uid = user["id"]
    return {"chunks": get_kb(uid).get_all_chunks()}


@app.get("/api/chunks/{filename}")
async def list_chunks_by_file(filename: str, user: dict = Depends(get_current_user)):
    uid = user["id"]
    return {"chunks": get_kb(uid).get_chunks_by_source(filename)}


@app.get("/api/system")
async def system_info(user: dict = Depends(get_current_user)):
    tools_info = []
    for t in TOOLS:
        f = t["function"]
        tools_info.append({
            "name": f["name"],
            "description": f["description"],
            "parameters": f["parameters"],
        })

    cfg = get_llm_settings()
    return {
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "embedding_model": config.EMBEDDING_MODEL,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "max_tool_rounds": config.MAX_TOOL_ROUNDS,
        "history_window": config.HISTORY_WINDOW,
        "chroma_dir": str(config.CHROMA_DIR),
        "docs_dir": str(config.DOCS_DIR),
        "supported_exts": list(SUPPORTED_EXTS),
        "tools": tools_info,
    }
