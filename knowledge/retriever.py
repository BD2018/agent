"""向量检索：负责 Embedding 模型加载、Chroma 向量库的读写。

设计要点：
- 按用户隔离：get_kb(user_id) 返回该用户专属的 KnowledgeBase，
  collection 名为 user_{id}_docs，不同用户的知识库互不可见。
- Embedding 模型和 Chroma client 跨用户共享（只加载一次），
  仅 collection 按用户隔离。
- Chroma 使用 PersistentClient，数据落盘在 data/chroma/，重启不丢失。
- bge 系列模型检索时建议在 query 前加指令前缀，能明显提升中文检索效果。
- 写操作（add/delete）加锁，防止 Web 多线程上传时并发写入冲突。
  同进程内写入后读取立即可见，无需重启或刷新。
"""
import threading

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

import config

# bge 模型官方推荐的查询指令前缀（仅查询侧使用，入库侧不加）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

# 跨用户共享的 Embedding 函数和 Chroma client（只加载一次）
_ef = None
_client = None
_init_lock = threading.Lock()


def _ensure_shared():
    global _ef, _client
    if _ef is None or _client is None:
        with _init_lock:
            if _ef is None:
                _ef = SentenceTransformerEmbeddingFunction(model_name=config.EMBEDDING_MODEL)
            if _client is None:
                _client = chromadb.PersistentClient(path=config.CHROMA_DIR)


class KnowledgeBase:
    """每个用户一个实例，绑定各自的 Chroma collection。"""

    def __init__(self, user_id: int):
        _ensure_shared()
        self._col = _client.get_or_create_collection(
            name=f"user_{user_id}_docs",
            embedding_function=_ef,
            metadata={"hnsw:space": "cosine"},
        )
        self._lock = threading.Lock()

    def add(self, chunks, metadatas, ids):
        with self._lock:
            self._col.add(documents=chunks, metadatas=metadatas, ids=ids)

    def delete_by_source(self, source):
        with self._lock:
            try:
                self._col.delete(where={"source": source})
            except Exception:
                pass

    def count(self):
        return self._col.count()

    def search(self, query, top_k=3):
        if self._col.count() == 0:
            return "知识库当前为空，请先上传文档入库。"
        res = self._col.query(
            query_texts=[QUERY_INSTRUCTION + query],
            n_results=min(top_k, self._col.count()),
        )
        docs = res["documents"][0]
        metas = res["metadatas"][0]
        parts = [
            f"【来源：{m.get('source', '未知')}】\n{d}"
            for d, m in zip(docs, metas)
        ]
        return "\n\n---\n\n".join(parts)

    def list_sources(self):
        try:
            result = self._col.get(include=["metadatas"])
            sources = {m.get("source") for m in result.get("metadatas", []) if m}
            return sorted(s for s in sources if s)
        except Exception:
            return []

    def get_all_chunks(self):
        """返回所有切片：[{id, source, content, preview}]"""
        try:
            result = self._col.get(include=["documents", "metadatas"])
            items = []
            for cid, doc, meta in zip(
                result.get("ids", []),
                result.get("documents", []),
                result.get("metadatas", []),
            ):
                items.append({
                    "id": cid,
                    "source": (meta or {}).get("source", "未知"),
                    "content": doc,
                    "preview": doc[:200] + ("..." if len(doc) > 200 else ""),
                })
            return items
        except Exception:
            return []

    def get_chunks_by_source(self, source):
        """返回某个文档的所有切片"""
        try:
            result = self._col.get(where={"source": source}, include=["documents", "metadatas"])
            items = []
            for cid, doc, meta in zip(
                result.get("ids", []),
                result.get("documents", []),
                result.get("metadatas", []),
            ):
                items.append({
                    "id": cid,
                    "source": source,
                    "content": doc,
                    "preview": doc[:200] + ("..." if len(doc) > 200 else ""),
                })
            return items
        except Exception:
            return []


_kb_cache: dict[int, KnowledgeBase] = {}


def get_kb(user_id: int) -> KnowledgeBase:
    """按 user_id 返回对应的知识库实例（缓存，懒加载）。"""
    if user_id not in _kb_cache:
        _kb_cache[user_id] = KnowledgeBase(user_id)
    return _kb_cache[user_id]
