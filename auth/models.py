"""用户存储：SQLite + pbkdf2_hmac 密码哈希（无外部依赖）。

首次启动时自动创建 admin 账号（密码 123456）。
"""
import hashlib
import os
import sqlite3
import threading

import config

_lock = threading.Lock()


def _hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256，盐随机，输出格式: salt_hex$hash_hex"""
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=100000)
    return salt.hex() + "$" + h.hex()


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations=100000)
        return h == expected
    except (ValueError, AttributeError):
        return False


def _get_conn():
    conn = sqlite3.connect(config.USERS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """创建 users 表，并预置 admin 账号。"""
    os.makedirs(os.path.dirname(config.USERS_DB), exist_ok=True)
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        existing = conn.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                ("admin", _hash_password("123456")),
            )
        conn.commit()


def create_user(username: str, password: str) -> dict | None:
    """注册新用户，成功返回 {id, username}，用户名已存在返回 None。"""
    with _lock, _get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return None
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, _hash_password(password)),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "username": username}


def authenticate(username: str, password: str) -> dict | None:
    """验证用户名+密码，成功返回 {id, username}，失败返回 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return {"id": row["id"], "username": row["username"]}


def get_user_by_id(user_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {"id": row["id"], "username": row["username"]}


def list_users() -> list[dict]:
    """列出所有用户（admin 专用）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at FROM users ORDER BY id"
        ).fetchall()
        return [
            {"id": r["id"], "username": r["username"], "created_at": r["created_at"]}
            for r in rows
        ]


def delete_user(user_id: int) -> bool:
    """删除用户。禁止删除 admin（id=1）。"""
    if user_id == 1:
        return False
    with _lock, _get_conn() as conn:
        cursor = conn.execute("DELETE FROM users WHERE id = ? AND id != 1", (user_id,))
        conn.commit()
        return cursor.rowcount > 0


def update_password(user_id: int, new_password: str) -> bool:
    """重置用户密码。"""
    with _lock, _get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(new_password), user_id),
        )
        conn.commit()
        return cursor.rowcount > 0
