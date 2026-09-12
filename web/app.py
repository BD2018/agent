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
import threading
import time
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

import config
from agent import datasource, tool_store
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
from agent.tools import get_all_tools
from auth.api_keys import (
    bump_usage,
    create_api_key,
    delete_api_key,
    list_api_keys,
    set_api_key_enabled,
    verify_api_key,
)
from auth.dependencies import get_current_user, require_permission
from auth.jwt_utils import create_token
from auth.models import (
    ALL_PAGES,
    authenticate,
    create_user,
    delete_user,
    get_role_prompt,
    get_user_by_id,
    init_db,
    list_users,
    set_role_prompt,
    update_password,
    update_permissions,
)
from knowledge.extractors import SUPPORTED_EXTS
from knowledge.ingest import ingest_file
from knowledge.retriever import get_kb
from knowledge import qa_store

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Agent 管理系统")

# ---------- 启动初始化 ----------
init_db()  # 创建 users 表 + 预置 admin 账号

# ---------- 模型预热 ----------
# 向量模型等重资源在启动时由后台线程预载，用户请求不再承担冷启动耗时；
# /api/status 通过 _model_ready 暴露就绪状态。
_model_ready = threading.Event()


def _warmup_models():
    def _load():
        try:
            import jieba
            jieba.initialize()  # 中文分词词典（BM25 检索依赖）
            from knowledge.retriever import warmup
            warmup()  # Embedding 模型 + Chroma 客户端
            _model_ready.set()
            print("向量模型预热完成，知识库已就绪")
        except Exception as e:
            print(f"向量模型预热失败（知识库暂不可用，对话不受影响）：{e}")

    threading.Thread(target=_load, daemon=True, name="model-warmup").start()


@app.on_event("startup")
async def _startup_warmup():
    _warmup_models()


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

NOCACHE_HEADERS = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/login")
async def login_page():
    return FileResponse(str(STATIC_DIR / "login.html"), headers=NOCACHE_HEADERS)


@app.get("/")
async def chat_page():
    return FileResponse(str(STATIC_DIR / "chat.html"), headers=NOCACHE_HEADERS)


@app.get("/console")
async def console_page():
    return FileResponse(str(STATIC_DIR / "console.html"), headers=NOCACHE_HEADERS)


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

    content = await file.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")

    dest = user_docs_dir(uid) / filename
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
    docs = kb.list_sources()
    return {"docs": docs, "total": len(docs)}


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
    if not _model_ready.is_set():
        # 预热中不触发模型加载，避免请求被冷启动阻塞
        return {"status": "warming", "model_loaded": False, "chunks": None}
    return {"status": "ok", "model_loaded": True, "chunks": get_kb(uid).count()}


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
    for t in get_all_tools():
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
        "top_k": config.FINAL_TOP_K,
        "hybrid_search": config.HYBRID_SEARCH_ENABLED,
        "max_tool_rounds": config.MAX_TOOL_ROUNDS,
        "history_window": config.HISTORY_WINDOW,
        "chroma_dir": str(config.CHROMA_DIR),
        "docs_dir": str(config.DOCS_DIR),
        "supported_exts": list(SUPPORTED_EXTS),
        "tools": tools_info,
    }


# ---------- 工具管理（需 tools 权限）----------
@app.get("/api/tools")
async def tools_list(user: dict = Depends(require_permission("tools"))):
    """内置工具 + 自定义工具合并列表。"""
    from agent.tools import TOOLS

    flags = tool_store.get_builtin_flags()
    builtin = []
    for t in TOOLS:
        f = t["function"]
        builtin.append({
            "builtin": True,
            "name": f["name"],
            "description": f["description"],
            "parameters": f["parameters"],
            "enabled": flags.get(f["name"], True),
        })
    custom = []
    for ct in tool_store.list_custom_tools():
        custom.append({
            "builtin": False,
            "id": ct["id"],
            "name": ct["name"],
            "type": ct["type"],
            "description": ct["description"],
            "params": tool_store.schema_to_params(ct["parameters"]),
            "parameters": ct["parameters"],
            "config": ct["config"],
            "enabled": ct["enabled"],
            "created_at": ct["created_at"],
            "updated_at": ct["updated_at"],
        })
    return {"builtin": builtin, "custom": custom}


@app.put("/api/tools/builtin/{name}")
async def tools_builtin_toggle(
    name: str, payload: dict, user: dict = Depends(require_permission("tools"))
):
    """启用/禁用内置工具，立即生效。"""
    enabled = bool(payload.get("enabled"))
    if not tool_store.set_builtin_enabled(name, enabled):
        raise HTTPException(404, "内置工具不存在")
    return {"ok": True, "name": name, "enabled": enabled}


@app.post("/api/tools")
async def tools_create(payload: dict, user: dict = Depends(require_permission("tools"))):
    try:
        tool = tool_store.create_tool(
            payload.get("name"),
            payload.get("type"),
            payload.get("description"),
            payload.get("params") or [],
            payload.get("config") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tool": tool}


@app.get("/api/tools/{tool_id}")
async def tools_get(tool_id: int, user: dict = Depends(require_permission("tools"))):
    tool = tool_store.get_custom_tool(tool_id)
    if not tool:
        raise HTTPException(404, "工具不存在")
    return {"tool": tool}


@app.put("/api/tools/{tool_id}")
async def tools_update(
    tool_id: int, payload: dict, user: dict = Depends(require_permission("tools"))
):
    if not tool_store.get_custom_tool(tool_id):
        raise HTTPException(404, "工具不存在")
    try:
        tool = tool_store.update_tool(
            tool_id,
            payload.get("name"),
            payload.get("type"),
            payload.get("description"),
            payload.get("params") or [],
            payload.get("config") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tool": tool}


@app.delete("/api/tools/{tool_id}")
async def tools_delete(tool_id: int, user: dict = Depends(require_permission("tools"))):
    if not tool_store.delete_tool(tool_id):
        raise HTTPException(404, "工具不存在")
    return {"ok": True}


@app.put("/api/tools/{tool_id}/enabled")
async def tools_toggle(tool_id: int, payload: dict, user: dict = Depends(require_permission("tools"))):
    tool = tool_store.set_tool_enabled(tool_id, bool(payload.get("enabled")))
    if not tool:
        raise HTTPException(404, "工具不存在")
    return {"ok": True, "tool": tool}


@app.post("/api/tools/{tool_id}/test")
async def tools_test(
    tool_id: int, payload: dict, user: dict = Depends(require_permission("tools"))
):
    """试调工具：用给定参数走一遍真实执行链路。"""
    from agent.tools import execute_tool

    tool = tool_store.get_custom_tool(tool_id)
    if not tool:
        raise HTTPException(404, "工具不存在")
    arguments = payload.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise HTTPException(400, "arguments 必须是对象")
    result = await asyncio.to_thread(
        execute_tool, tool["name"], json.dumps(arguments, ensure_ascii=False), user["id"]
    )
    return {"result": result}


# ---------- 数据源管理（需 tools 权限）----------
@app.get("/api/datasources")
async def datasources_list(user: dict = Depends(require_permission("tools"))):
    return {"datasources": datasource.list_datasources()}


@app.post("/api/datasources")
async def datasources_create(payload: dict, user: dict = Depends(require_permission("tools"))):
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    db_name = (payload.get("db_name") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    try:
        port = int(payload.get("port") or 3306)
    except (TypeError, ValueError):
        raise HTTPException(400, "端口必须是整数")
    if not name or not host or not db_name or not username:
        raise HTTPException(400, "名称、主机、数据库名、用户名不能为空")
    try:
        ds = datasource.create_datasource(name, host, port, db_name, username, password)
    except Exception as e:
        raise HTTPException(400, f"保存失败（名称可能重复）: {e}")
    return {"datasource": ds}


@app.put("/api/datasources/{ds_id}")
async def datasources_update(
    ds_id: int, payload: dict, user: dict = Depends(require_permission("tools"))
):
    if not datasource.get_datasource(ds_id):
        raise HTTPException(404, "数据源不存在")
    name = (payload.get("name") or "").strip()
    host = (payload.get("host") or "").strip()
    db_name = (payload.get("db_name") or "").strip()
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""  # 空串 = 保持原密码
    try:
        port = int(payload.get("port") or 3306)
    except (TypeError, ValueError):
        raise HTTPException(400, "端口必须是整数")
    if not name or not host or not db_name or not username:
        raise HTTPException(400, "名称、主机、数据库名、用户名不能为空")
    try:
        ds = datasource.update_datasource(ds_id, name, host, port, db_name, username, password)
    except Exception as e:
        raise HTTPException(400, f"保存失败（名称可能重复）: {e}")
    return {"datasource": ds}


@app.delete("/api/datasources/{ds_id}")
async def datasources_delete(ds_id: int, user: dict = Depends(require_permission("tools"))):
    if tool_store.is_datasource_in_use(ds_id):
        raise HTTPException(400, "该数据源正被工具引用，请先修改或删除相关工具")
    if not datasource.delete_datasource(ds_id):
        raise HTTPException(404, "数据源不存在")
    return {"ok": True}


@app.post("/api/datasources/test")
async def datasources_test(payload: dict, user: dict = Depends(require_permission("tools"))):
    """用表单内容测试连接（保存前可用）。"""
    ds = {
        "host": (payload.get("host") or "").strip(),
        "port": int(payload.get("port") or 3306),
        "db_name": (payload.get("db_name") or "").strip(),
        "username": (payload.get("username") or "").strip(),
        "password": payload.get("password") or "",
    }
    ok, message = await asyncio.to_thread(datasource.test_connection, ds)
    return {"ok": ok, "message": message}


@app.post("/api/datasources/{ds_id}/test")
async def datasources_test_saved(ds_id: int, user: dict = Depends(require_permission("tools"))):
    """用已保存的数据源配置测试连接。"""
    ds = datasource.get_datasource(ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    ok, message = await asyncio.to_thread(datasource.test_connection, ds)
    return {"ok": ok, "message": message}


@app.post("/api/datasources/{ds_id}/query")
async def datasources_query(
    ds_id: int, payload: dict, user: dict = Depends(require_permission("tools"))
):
    """试跑 SQL 模板：参数化执行，返回结果行（用于工具表单调试）。"""
    ds = datasource.get_datasource(ds_id)
    if not ds:
        raise HTTPException(404, "数据源不存在")
    sql = (payload.get("sql") or "").strip()
    params = payload.get("params") or {}
    if not sql:
        raise HTTPException(400, "SQL 模板不能为空")
    if not isinstance(params, dict):
        raise HTTPException(400, "params 必须是对象")
    try:
        rows = await asyncio.to_thread(datasource.run_query, ds, sql, params)
    except Exception as e:
        raise HTTPException(400, f"查询失败: {e}")
    return {"rows": rows, "count": len(rows)}


# ---------- API Key 管理（登录用户管理自己的 Key）----------
@app.get("/api/api-keys")
async def api_keys_list(user: dict = Depends(get_current_user)):
    return {"keys": list_api_keys(user["id"])}


@app.post("/api/api-keys")
async def api_keys_create(payload: dict, user: dict = Depends(get_current_user)):
    name = (payload.get("name") or "").strip() or "未命名"
    mode = payload.get("mode") or "agent"
    try:
        key = create_api_key(user["id"], name, mode=mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"key": key}


@app.put("/api/api-keys/{key_id}/enabled")
async def api_keys_toggle(
    key_id: int, payload: dict, user: dict = Depends(get_current_user)
):
    if not set_api_key_enabled(key_id, user["id"], bool(payload.get("enabled"))):
        raise HTTPException(404, "API Key 不存在")
    return {"ok": True}


@app.delete("/api/api-keys/{key_id}")
async def api_keys_delete(key_id: int, user: dict = Depends(get_current_user)):
    if not delete_api_key(key_id, user["id"]):
        raise HTTPException(404, "API Key 不存在")
    return {"ok": True}


# ---------- 开放接口（API Key 认证，供外部系统调用）----------
def get_open_api_user(request: Request) -> dict:
    """从 Authorization: Bearer sk-xxx 或 X-API-Key 头解析并校验 API Key。"""
    key = (request.headers.get("X-API-Key") or "").strip()
    if not key:
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    if not key:
        raise HTTPException(401, "未提供 API Key（X-API-Key 或 Authorization: Bearer）")
    info = verify_api_key(key)
    if not info:
        raise HTTPException(401, "API Key 无效或已禁用")
    user = get_user_by_id(info["user_id"])
    if not user:
        raise HTTPException(401, "API Key 所属用户不存在")
    return user


@app.post("/open/v1/upload")
async def open_upload(
    request: Request,
    file: UploadFile = File(...),
):
    """开放上传：文件切片后写入 Key 所属用户的向量知识库。"""
    user = get_open_api_user(request)
    uid = user["id"]
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(400, f"不支持的格式: {ext}（支持：{'/'.join(SUPPORTED_EXTS)}）")

    content = await file.read()
    if len(content) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")

    dest = user_docs_dir(uid) / filename
    dest.write_bytes(content)

    try:
        chunks = await asyncio.to_thread(ingest_file, dest, uid)
    except Exception as e:
        raise HTTPException(500, f"入库失败: {e}")

    return {"filename": filename, "chunks": chunks, "total": get_kb(uid).count()}


@app.get("/open/v1/docs")
async def open_list_docs(request: Request):
    """列出 Key 所属用户已入库的文档。"""
    user = get_open_api_user(request)
    kb = get_kb(user["id"])
    docs = kb.list_sources()
    return {"docs": docs, "total": len(docs), "chunks": kb.count()}


@app.delete("/open/v1/docs/{filename}")
async def open_remove_doc(filename: str, request: Request):
    """删除 Key 所属用户的文档及其向量片段。"""
    user = get_open_api_user(request)
    uid = user["id"]
    path = user_docs_dir(uid) / filename
    kb = get_kb(uid)
    kb.delete_by_source(filename)
    if path.exists():
        path.unlink()
    return {"deleted": filename, "total": kb.count()}


# ---------- 开放对话接口（OpenAI 兼容，按 Key 模式分流）----------
_OPEN_CHAT_PARAMS = (
    "temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty",
)


def _openai_error(status: int, message: str, err_type: str = "invalid_request_error"):
    """OpenAI SDK 可解析的错误响应格式。"""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "param": None, "code": None}},
    )


def _open_chat_auth(request: Request):
    """开放对话鉴权，返回 (user, key_info)；失败返回错误响应而非抛异常。"""
    key = (request.headers.get("X-API-Key") or "").strip()
    if not key:
        auth = request.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            key = auth[7:].strip()
    info = verify_api_key(key) if key else None
    if not info:
        return None, _openai_error(
            401, "无效或缺失的 API Key（X-API-Key 或 Authorization: Bearer sk-xxx）",
            "authentication_error",
        )
    user = get_user_by_id(info["user_id"])
    if not user:
        return None, _openai_error(401, "API Key 所属用户不存在", "authentication_error")
    bump_usage(info["key_id"])
    return (user, info), None


def _last_user_text(messages: list) -> str:
    """取 messages 中最后一条 user 消息的文本（Agent 模式的输入）。"""
    for m in reversed(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):  # 多模态内容数组时取文本片段
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return str(c or "").strip()
    return ""


def _chat_completion_payload(model: str, content: str, finish: str = "stop") -> dict:
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


async def _open_proxy_chat(messages: list, stream: bool, params: dict):
    """proxy 模式：纯模型代理，把 messages 原样转发给当前启用的模型。"""
    cfg = get_llm_settings()
    if not cfg["has_api_key"]:
        return _openai_error(502, "服务端尚未配置可用模型，请管理员在控制台启用", "api_error")
    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"], timeout=60)
    model = cfg["model"]

    if not stream:
        try:
            resp = await asyncio.to_thread(
                client.chat.completions.create, model=model, messages=messages, **params,
            )
        except Exception as e:
            return _openai_error(502, f"上游模型调用失败：{e}", "api_error")
        return resp.model_dump(exclude_none=True)

    def generate():
        try:
            upstream = client.chat.completions.create(
                model=model, messages=messages, stream=True, **params,
            )
            for chunk in upstream:
                yield f"data: {chunk.model_dump_json(exclude_none=True)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            err = json.dumps(
                {"error": {"message": f"上游模型调用失败：{e}", "type": "api_error"}},
                ensure_ascii=False,
            )
            yield f"data: {err}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _open_agent_chat(user: dict, messages: list, stream: bool):
    """agent 模式：走 Agent 完整链路（QA 短路/知识库/工具），按 Key 归属用户隔离。"""
    text = _last_user_text(messages)
    if not text:
        return _openai_error(400, "messages 中未找到有效的 user 消息内容")
    agent = get_agent(user["id"])
    model = get_llm_settings()["model"]

    if not stream:
        try:
            answer = await asyncio.to_thread(agent.chat, text)
        except Exception as e:
            return _openai_error(500, f"对话执行失败：{e}", "api_error")
        return _chat_completion_payload(model, answer)

    def generate():
        try:
            for ev_type, content in agent.chat_stream(text):
                if ev_type != "text":
                    continue  # tool/done 事件在服务端消化，不进入 OpenAI 流
                chunk = {
                    "id": f"chatcmpl-{uuid4().hex[:8]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            final = {
                "id": f"chatcmpl-{uuid4().hex[:8]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            err = json.dumps(
                {"error": {"message": f"对话执行失败：{e}", "type": "api_error"}},
                ensure_ascii=False,
            )
            yield f"data: {err}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/open/v1/chat/completions")
async def open_chat_completions(request: Request):
    """OpenAI 兼容对话端点：行为由 Key 的模式决定（agent=完整链路 / proxy=纯代理）。"""
    auth, err = _open_chat_auth(request)
    if err:
        return err
    user, info = auth

    try:
        body = await request.json()
    except Exception:
        return _openai_error(400, "请求体不是合法 JSON")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _openai_error(400, "messages 必须是非空数组")
    stream = bool(body.get("stream"))
    params = {k: body[k] for k in _OPEN_CHAT_PARAMS if body.get(k) is not None}

    if info.get("mode") == "proxy":
        return await _open_proxy_chat(messages, stream, params)
    return await _open_agent_chat(user, messages, stream)


# ---------- 个人角色定义 ----------
@app.get("/api/profile/role")
async def profile_role_get(user: dict = Depends(get_current_user)):
    return {"role_prompt": get_role_prompt(user["id"])}


@app.put("/api/profile/role")
async def profile_role_set(payload: dict, user: dict = Depends(get_current_user)):
    try:
        set_role_prompt(user["id"], payload.get("role_prompt") or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------- 固定问答管理（需 knowledge 权限）----------
@app.get("/api/qa-pairs")
async def qa_pairs_list(user: dict = Depends(require_permission("knowledge"))):
    return {"qa_pairs": qa_store.list_qa_pairs(user["id"])}


@app.post("/api/qa-pairs")
async def qa_pairs_create(payload: dict, user: dict = Depends(require_permission("knowledge"))):
    try:
        qa = qa_store.create_qa_pair(
            user["id"],
            payload.get("question"),
            payload.get("answer"),
            float(payload.get("similarity_threshold") or 0.75),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"qa": qa}


@app.put("/api/qa-pairs/{qa_id}")
async def qa_pairs_update(qa_id: int, payload: dict, user: dict = Depends(require_permission("knowledge"))):
    try:
        qa = qa_store.update_qa_pair(
            user["id"], qa_id,
            payload.get("question"),
            payload.get("answer"),
            float(payload.get("similarity_threshold") or 0.75),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"qa": qa}


@app.delete("/api/qa-pairs/{qa_id}")
async def qa_pairs_delete(qa_id: int, user: dict = Depends(require_permission("knowledge"))):
    if not qa_store.delete_qa_pair(user["id"], qa_id):
        raise HTTPException(404, "问答对不存在")
    return {"ok": True}


@app.put("/api/qa-pairs/{qa_id}/enabled")
async def qa_pairs_toggle(qa_id: int, payload: dict, user: dict = Depends(require_permission("knowledge"))):
    qa = qa_store.set_qa_enabled(user["id"], qa_id, bool(payload.get("enabled")))
    if not qa:
        raise HTTPException(404, "问答对不存在")
    return {"ok": True, "qa": qa}


# ---------- 接口文档页 ----------
@app.get("/api-docs")
async def api_docs_page():
    return FileResponse(str(STATIC_DIR / "api_docs.html"))
