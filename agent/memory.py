"""对话记忆持久化：用 SQLite 存储对话历史，重启程序后自动恢复。

设计要点：
- 按用户隔离：MemoryStore(user_id)，每个用户的对话历史互不可见。
- 只存 user / assistant 的最终问答对，工具调用的中间过程不存
  （中间过程只服务于当轮推理，存了反而会污染后续上下文）。
- 数据库文件在 data/memory.db，零配置、随项目走。
- 向后兼容：自动检查旧表结构，缺少 user_id 列时自动迁移。
"""
import sqlite3
import threading

import config

DB_PATH = config.BASE_DIR / "data" / "memory.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);"

_MIGRATE = """
-- 旧表迁移：如果 messages 表没有 user_id 列，重建带 user_id 的新表
"""


class MemoryStore:
    def __init__(self, user_id: int = 1):
        self.user_id = user_id
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()]
            if "user_id" not in cols:
                self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1"
                )
            if "session_id" in cols:
                self._conn.execute(
                    "UPDATE messages SET session_id = 1 WHERE session_id IS NULL"
                )
                self._conn.execute(
                    "CREATE TABLE messages_new AS "
                    "SELECT id, user_id, role, content, created_at FROM messages"
                )
                self._conn.execute("DROP TABLE messages")
                self._conn.execute("ALTER TABLE messages_new RENAME TO messages")
                self._conn.execute(_INDEX)
            self._conn.commit()

    def append(self, role, content):
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
                (self.user_id, role, content),
            )
            self._conn.commit()

    def load_recent(self, limit):
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM messages
                    WHERE user_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (self.user_id, limit),
            ).fetchall()
        return [{"role": role, "content": content} for role, content in rows]

    def clear(self):
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE user_id = ?", (self.user_id,)
            )
            self._conn.commit()

    def count(self):
        with self._lock:
            (n,) = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?",
                (self.user_id,),
            ).fetchone()
        return n
