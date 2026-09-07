"""开放接口 API Key 管理：供外部系统调用 /open 接口。

- Key 形如 sk-xxxx，明文仅在创建时返回一次，之后无法再查看
- 数据库只存 SHA-256 哈希与前缀（用于列表展示）
- 每个用户管理自己的 Key；开放接口的数据操作归属于 Key 所属用户
"""
import hashlib
import secrets
import sqlite3
import threading
from datetime import datetime

import config

_lock = threading.Lock()

KEY_PREFIX = "sk-"


def _get_conn():
    conn = sqlite3.connect(config.SETTINGS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                key_hash TEXT UNIQUE NOT NULL,
                key_prefix TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT
            )
        """)
        conn.commit()


_init_db()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_api_key(user_id: int, name: str = "") -> dict:
    """创建 Key，返回结果中包含一次性明文 key。"""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    display_prefix = key[:12] + "..."
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO api_keys (user_id, name, key_hash, key_prefix) "
            "VALUES (?, ?, ?, ?)",
            (user_id, (name or "").strip(), _hash(key), display_prefix),
        )
        conn.commit()
        new_id = cur.lastrowid
        row = conn.execute(
            "SELECT * FROM api_keys WHERE id = ?", (new_id,)
        ).fetchone()
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    d["key"] = key  # 仅此一次返回明文
    return d


def list_api_keys(user_id: int) -> list:
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, key_prefix, enabled, created_at, last_used_at "
            "FROM api_keys WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d["enabled"])
        result.append(d)
    return result


def set_api_key_enabled(key_id: int, user_id: int, enabled: bool) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "UPDATE api_keys SET enabled = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, key_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def delete_api_key(key_id: int, user_id: int) -> bool:
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM api_keys WHERE id = ? AND user_id = ?",
            (key_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def verify_api_key(key: str):
    """校验 Key，有效则返回 {key_id, user_id} 并刷新最后使用时间。"""
    if not key or not key.startswith(KEY_PREFIX):
        return None
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, enabled FROM api_keys WHERE key_hash = ?",
            (_hash(key),),
        ).fetchone()
        if not row or not row["enabled"]:
            return None
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), row["id"]),
        )
        conn.commit()
    return {"key_id": row["id"], "user_id": row["user_id"]}
