"""FastAPI 依赖：从请求头提取并验证 JWT，注入 current_user。"""
from fastapi import Depends, HTTPException, Request

from auth.jwt_utils import verify_token
from auth.models import get_user_by_id


def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：验证 Authorization: Bearer <token>，返回 {id, username}。

    用法：@app.get("/api/xxx") async def route(user = Depends(get_current_user))
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "未提供认证令牌")

    token = auth_header[7:]
    payload = verify_token(token)
    if not payload:
        raise HTTPException(401, "认证令牌无效或已过期")

    user_id = int(payload["sub"])
    user = get_user_by_id(user_id)
    if not user:
        raise HTTPException(401, "用户不存在")

    return user


def get_admin_user(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI 依赖：仅 admin（id=1）可访问，否则 403。"""
    if user["id"] != 1:
        raise HTTPException(403, "仅管理员可执行此操作")
    return user
