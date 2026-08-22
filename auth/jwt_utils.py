"""JWT 签发与验证。"""
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt

import config


def create_token(user_id: int, username: str) -> str:
    """签发 JWT，有效期由 config.JWT_EXPIRE_HOURS 控制。"""
    expire = datetime.now(timezone.utc) + timedelta(hours=config.JWT_EXPIRE_HOURS)
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def verify_token(token: str) -> dict | None:
    """验证 JWT，返回 payload 或 None。"""
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
        return payload
    except JWTError:
        return None
