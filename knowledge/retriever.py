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

检索策略（search 入口自动路由）：
- CAG：知识库片段数 <= CAG_MAX_CHUNKS 且总字符数 <= CAG_TOKEN_THRESHOLD 时，
  跳过检索，全部内容直接进上下文；
- 混合检索（默认）：BM25 关键词（bm25_store）+ 向量双路召回，RRF 融合排名；
- HYBRID_SEARCH_ENABLED=false 时退回纯向量检索。
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
        self._user_id = user_id
        self._col = _client.get_or_create_collection(
            name=f"user_{user_id}_docs",
            embedding_function=_ef,
            metadata={"hnsw:space": "cosine"},
        )
        self._lock = threading.Lock()

    def add(self, chunks, metadatas, ids):
        with self._lock:
            self._col.add(documents=chunks, metadatas=metadatas, ids=ids)
        from knowledge import bm25_store
        bm25_store.invalidate(self._user_id)

    def delete_by_source(self, source):
        with self._lock:
            try:
                self._col.delete(where={"source": source})
            except Exception:
                pass
        from knowledge import bm25_store
        bm25_store.invalidate(self._user_id)

    def count(self):
        return self._col.count()

    def search(self, query, top_k=None):
        # 优先匹配固定问答（语义相似度达标则直接返回预设回答）
        from knowledge.qa_store import search_qa
        qa_answer = search_qa(self._user_id, query, QUERY_INSTRUCTION)
        if qa_answer:
            return qa_answer

        count = self._col.count()
        if count == 0:
            return "知识库当前为空，请先上传文档入库。"

        # CAG 路由：小知识库不做检索，全部内容直接进上下文
        if count <= config.CAG_MAX_CHUNKS:
            result = self._col.get(include=["documents", "metadatas"])
            docs = result.get("documents", [])
            total_chars = sum(len(d or "") for d in docs)
            if total_chars <= config.CAG_TOKEN_THRESHOLD:
                parts = [
                    f"【来源：{(m or {}).get('source', '未知')}】\n{d}"
                    for d, m in zip(docs, result.get("metadatas", []))
                ]
                header = (
                    f"[检索模式：CAG] 知识库较小（共 {len(docs)} 个片段、约 {total_chars} 字），"
                    f"以下为知识库全部内容，请直接依据全文回答：\n\n"
                )
                return header + "\n\n---\n\n".join(parts)

        # RAG 模式：混合检索（默认）或纯向量检索
        if config.HYBRID_SEARCH_ENABLED:
            return self._hybrid_search(query)
        return self._vector_search(query, top_k or config.FINAL_TOP_K)

    def _vector_search(self, query, top_k):
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

    def _hybrid_search(self, query):
        """BM25 关键词 + 向量双路召回，RRF 融合排名后取前 FINAL_TOP_K。"""
        from knowledge import bm25_store

        res = self._col.query(
            query_texts=[QUERY_INSTRUCTION + query],
            n_results=min(config.VECTOR_TOP_K, self._col.count()),
            include=["documents", "metadatas"],
        )
        content_by_id = {}
        vector_order = []
        for cid, doc, meta in zip(res["ids"][0], res["documents"][0], res["metadatas"][0]):
            content_by_id[cid] = (doc, (meta or {}).get("source", "未知"))
            vector_order.append(cid)

        bm25_order = []
        index = bm25_store.get_index(self._user_id, self._col)
        for cid, doc, source, _score in index.search(query, config.BM25_TOP_K):
            content_by_id.setdefault(cid, (doc, source))
            bm25_order.append(cid)

        # RRF：score = Σ 1/(k + rank)，两路排名融合，对分数尺度不敏感
        scores = {}
        for rank, cid in enumerate(vector_order):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (config.RRF_K + rank + 1)
        for rank, cid in enumerate(bm25_order):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (config.RRF_K + rank + 1)

        top = sorted(scores, key=scores.get, reverse=True)[: config.FINAL_TOP_K]
        parts = [
            f"【来源：{content_by_id[cid][1]}】\n{content_by_id[cid][0]}"
            for cid in top
        ]
        header = (
            f"[检索模式：混合检索] BM25 关键词 + 向量双路召回、RRF 融合排名，"
            f"以下为最相关的 {len(top)} 个片段：\n\n"
        )
        return header + "\n\n---\n\n".join(parts)

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
