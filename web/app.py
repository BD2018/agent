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
from agent.tools import TOOLS
from auth.dependencies import get_admin_user, get_current_user
from auth.jwt_utils import create_token
from auth.models import (
    authenticate,
    create_user,
    delete_user,
    init_db,
    list_users,
    update_password,
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


# ---------- 用户管理（仅 admin）----------
@app.get("/api/users")
async def users_list(admin: dict = Depends(get_admin_user)):
    return {"users": list_users()}


@app.post("/api/users")
async def users_create(payload: dict, admin: dict = Depends(get_admin_user)):
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    if len(password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    user = create_user(username, password)
    if not user:
        raise HTTPException(409, "用户名已存在")
    return {"user": user}


@app.delete("/api/users/{user_id}")
async def users_delete(user_id: int, admin: dict = Depends(get_admin_user)):
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
    user_id: int, payload: dict, admin: dict = Depends(get_admin_user)
):
    new_password = (payload.get("password") or "").strip()
    if len(new_password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if not update_password(user_id, new_password):
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


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

    return {
        "model": config.MODEL,
        "base_url": config.DEEPSEEK_BASE_URL,
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
