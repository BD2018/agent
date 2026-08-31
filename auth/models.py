"""用户存储：SQLite + pbkdf2_hmac 密码哈希（无外部依赖）。

首次启动时自动创建 admin 账号（密码 123456）。
permissions 字段存页面权限（JSON 数组），控制该用户在管理页可见的菜单；
admin（id=1）恒为全部权限，不允许修改。
"""
import hashlib
import json
import os
import sqlite3
import threading

import config

_lock = threading.Lock()

# 管理页 key -> 中文名（console.html 侧边栏与权限配置共用）
ALL_PAGES = {
    "dashboard": "概览",
    "knowledge": "知识库管理",
    "doclist": "文档列表",
    "chunks": "向量片段",
    "history": "对话历史",
    "system": "系统信息",
    "llm": "模型设置",
    "users": "用户管理",
}

# 新用户默认可见的常规页面（模型设置/用户管理需管理员单独勾选）
DEFAULT_PAGES = ["dashboard", "knowledge", "doclist", "chunks", "history", "system"]


def _clean_permissions(perms) -> list[str]:
    """过滤非法 key，按 ALL_PAGES 顺序稳定输出；None 时返回默认权限。"""
    if perms is None:
        return list(DEFAULT_PAGES)
    if not isinstance(perms, list):
        raise ValueError("permissions 必须是页面 key 数组")
    allowed = set(perms)
    return [k for k in ALL_PAGES if k in allowed]


def _decode_permissions(user_id: int, raw) -> list[str]:
    """把 DB 中的 JSON 文本解析为权限列表；admin 恒为全部。"""
    if user_id == 1:
        return list(ALL_PAGES.keys())
    try:
        perms = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        perms = None
    return _clean_permissions(perms)


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
    """创建 users 表（含权限列），迁移旧库，并预置 admin 账号。"""
    os.makedirs(os.path.dirname(config.USERS_DB), exist_ok=True)
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                permissions TEXT
            )
        """)
        # 旧库迁移：users 表缺 permissions 列时补上
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "permissions" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN permissions TEXT")
        # 存量用户回填默认权限
        conn.execute(
            "UPDATE users SET permissions = ? WHERE permissions IS NULL OR permissions = ''",
            (json.dumps(DEFAULT_PAGES, ensure_ascii=False),),
        )
        existing = conn.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO users (username, password_hash, permissions) VALUES (?, ?, ?)",
                ("admin", _hash_password("123456"), json.dumps(list(ALL_PAGES.keys()))),
            )
        conn.commit()


def create_user(username: str, password: str, permissions=None) -> dict | None:
    """注册新用户，成功返回 {id, username}，用户名已存在返回 None。

    permissions 为页面 key 数组，None 时使用 DEFAULT_PAGES。
    """
    perms = _clean_permissions(permissions)
    with _lock, _get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        if existing:
            return None
        cursor = conn.execute(
            "INSERT INTO users (username, password_hash, permissions) VALUES (?, ?, ?)",
            (username, _hash_password(password), json.dumps(perms, ensure_ascii=False)),
        )
        conn.commit()
        return {"id": cursor.lastrowid, "username": username}


def authenticate(username: str, password: str) -> dict | None:
    """验证用户名+密码，成功返回 {id, username}，失败返回 None。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, permissions FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row:
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "permissions": _decode_permissions(row["id"], row["permissions"]),
        }


def get_user_by_id(user_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, permissions FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "permissions": _decode_permissions(row["id"], row["permissions"]),
        }


def list_users() -> list[dict]:
    """列出所有用户（需 users 权限）。"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at, permissions FROM users ORDER BY id"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "created_at": r["created_at"],
                "permissions": _decode_permissions(r["id"], r["permissions"]),
            }
            for r in rows
        ]


def update_permissions(user_id: int, permissions) -> bool:
    """更新页面权限。admin（id=1）恒为全部权限，不允许修改。"""
    if user_id == 1:
        return False
    perms = _clean_permissions(permissions)
    with _lock, _get_conn() as conn:
        cursor = conn.execute(
            "UPDATE users SET permissions = ? WHERE id = ? AND id != 1",
            (json.dumps(perms, ensure_ascii=False), user_id),
        )
        conn.commit()
        return cursor.rowcount > 0


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
