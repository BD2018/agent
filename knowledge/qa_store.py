"""固定问答对：语义相似度匹配后返回预设回答。

存储结构：
- SQLite (settings.db) 的 qa_pairs 表存元数据（问题、回答、阈值、启用状态）
- Chroma 的 user_{id}_qa collection 存问题向量，metadata 中关联 qa_id

检索时：用户问题 → 向量化 → 在 QA collection 中查最近邻 →
        相似度 ≥ 阈值 → 返回 SQLite 中的固定回答
"""
import sqlite3
import threading

import config

_lock = threading.Lock()


def _get_conn():
    conn = sqlite3.connect(config.SETTINGS_DB, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _lock, _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                similarity_threshold REAL NOT NULL DEFAULT 0.70,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


_init_db()


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["enabled"] = bool(d["enabled"])
    return d


# ── CRUD ──

def list_qa_pairs(user_id: int) -> list:
    with _lock, _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM qa_pairs WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_qa_pair(user_id: int, qa_id: int):
    with _lock, _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM qa_pairs WHERE id = ? AND user_id = ?",
            (qa_id, user_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_qa_pair(user_id: int, question: str, answer: str,
                   threshold: float = 0.70) -> dict:
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    if not answer:
        raise ValueError("回答不能为空")
    if not 0.5 <= threshold <= 0.99:
        raise ValueError("相似度阈值需在 0.5-0.99 之间")

    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO qa_pairs (user_id, question, answer, similarity_threshold) "
            "VALUES (?, ?, ?, ?)",
            (user_id, question, answer, threshold),
        )
        conn.commit()
        new_id = cur.lastrowid
    _add_to_chroma(user_id, new_id, question, threshold)
    return get_qa_pair(user_id, new_id)


def update_qa_pair(user_id: int, qa_id: int, question: str, answer: str,
                   threshold: float = 0.70) -> dict:
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question:
        raise ValueError("问题不能为空")
    if not answer:
        raise ValueError("回答不能为空")
    if not 0.5 <= threshold <= 0.99:
        raise ValueError("相似度阈值需在 0.5-0.99 之间")

    existing = get_qa_pair(user_id, qa_id)
    if not existing:
        raise ValueError("问答对不存在")

    with _lock, _get_conn() as conn:
        conn.execute(
            "UPDATE qa_pairs SET question=?, answer=?, similarity_threshold=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
            (question, answer, threshold, qa_id, user_id),
        )
        conn.commit()

    if existing["enabled"]:
        _update_chroma(user_id, qa_id, question, threshold)
    return get_qa_pair(user_id, qa_id)


def delete_qa_pair(user_id: int, qa_id: int) -> bool:
    existing = get_qa_pair(user_id, qa_id)
    if not existing:
        return False
    with _lock, _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM qa_pairs WHERE id=? AND user_id=?", (qa_id, user_id)
        )
        conn.commit()
    _delete_from_chroma(user_id, qa_id)
    return cur.rowcount > 0


def set_qa_enabled(user_id: int, qa_id: int, enabled: bool):
    existing = get_qa_pair(user_id, qa_id)
    if not existing:
        return None
    with _lock, _get_conn() as conn:
        conn.execute(
            "UPDATE qa_pairs SET enabled=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND user_id=?",
            (1 if enabled else 0, qa_id, user_id),
        )
        conn.commit()
    if enabled:
        _add_to_chroma(user_id, qa_id, existing["question"],
                        existing["similarity_threshold"])
    else:
        _delete_from_chroma(user_id, qa_id)
    return get_qa_pair(user_id, qa_id)


# ── Chroma 向量管理 ──

def _get_qa_collection(user_id: int):
    import knowledge.retriever as ret
    ret._ensure_shared()
    return ret._client.get_or_create_collection(
        name=f"user_{user_id}_qa",
        embedding_function=ret._ef,
        metadata={"hnsw:space": "cosine"},
    )


def _add_to_chroma(user_id: int, qa_id: int, question: str, threshold: float):
    col = _get_qa_collection(user_id)
    try:
        col.delete(ids=[f"qa_{qa_id}"])
    except Exception:
        pass
    col.add(
        documents=[question],
        metadatas=[{"qa_id": qa_id, "threshold": threshold}],
        ids=[f"qa_{qa_id}"],
    )


def _update_chroma(user_id: int, qa_id: int, question: str, threshold: float):
    _add_to_chroma(user_id, qa_id, question, threshold)


def _delete_from_chroma(user_id: int, qa_id: int):
    try:
        col = _get_qa_collection(user_id)
        col.delete(ids=[f"qa_{qa_id}"])
    except Exception:
        pass


# ── 语义检索 ──

def search_qa(user_id: int, query: str, query_instruction: str = ""):
    """在 QA collection 中语义检索，超过阈值返回固定回答，否则返回 None。"""
    col = _get_qa_collection(user_id)
    if col.count() == 0:
        return None

    res = col.query(
        query_texts=[query_instruction + query],
        n_results=1,
        include=["distances", "metadatas"],
    )
    if not res["distances"][0]:
        return None

    distance = res["distances"][0][0]
    similarity = 1.0 - distance
    meta = res["metadatas"][0][0] or {}
    qa_id = meta.get("qa_id")
    threshold = meta.get("threshold", 0.75)

    if similarity < threshold:
        return None

    qa = get_qa_pair(user_id, qa_id) if qa_id else None
    if not qa or not qa["enabled"]:
        return None
    return qa["answer"]
