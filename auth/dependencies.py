"""FastAPI 依赖：从请求头提取并验证 JWT，注入 current_user；页面权限校验。"""
from fastapi import Depends, HTTPException, Request

from auth.jwt_utils import verify_token
from auth.models import ALL_PAGES, get_user_by_id


def get_current_user(request: Request) -> dict:
    """FastAPI 依赖：验证 Authorization: Bearer <token>，返回 {id, username, permissions}。

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


def require_permission(page: str):
    """FastAPI 依赖工厂：要求当前用户拥有指定页面权限，否则 403。

    admin（id=1）恒为全部权限，直接通过。
    用法：user = Depends(require_permission("users"))
    """
    if page not in ALL_PAGES:
        raise ValueError(f"未知页面权限: {page}")

    def dep(user: dict = Depends(get_current_user)) -> dict:
        if user["id"] != 1 and page not in user.get("permissions", []):
            raise HTTPException(403, f"没有「{ALL_PAGES[page]}」页面的访问权限")
        return user

    return dep
