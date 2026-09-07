"""BM25 关键词索引：jieba 分词 + rank-bm25 内存打分，供混合检索使用。

设计要点：
- 不重复存储切片内容，直接从用户对应的 Chroma collection 读取，
  避免与向量库双写产生一致性问题。
- 索引按用户缓存在内存中；KnowledgeBase 的写操作（add/delete_by_source）
  完成后调用 invalidate(user_id)，下次查询自动重建，无需重启。
- BM25 弥补向量检索的短板：专有名词、编号、精确关键词等场景召回更准。
"""
import threading

import jieba
from rank_bm25 import BM25Okapi

_lock = threading.Lock()
_cache: dict[int, "BM25Index"] = {}


def _tokenize(text: str) -> list:
    return [t for t in jieba.lcut(text or "") if t.strip()]


class BM25Index:
    """从 Chroma collection 全量构建的内存 BM25 索引。"""

    def __init__(self, user_id: int, col):
        self.user_id = user_id
        result = col.get(include=["documents", "metadatas"])
        self.ids = result.get("ids", [])
        self.docs = result.get("documents", [])
        self.metas = result.get("metadatas", [])
        self._bm25 = None
        if self.ids:
            tokenized = [_tokenize(d) or ["<empty>"] for d in self.docs]
            self._bm25 = BM25Okapi(tokenized)

    def search(self, query: str, top_k: int):
        """返回 [(chunk_id, content, source, score)]，按 BM25 分数降序。"""
        if not self._bm25:
            return []
        tokens = _tokenize(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        out = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                break
            out.append((
                self.ids[i],
                self.docs[i],
                (self.metas[i] or {}).get("source", "未知"),
                float(scores[i]),
            ))
        return out


def get_index(user_id: int, col) -> BM25Index:
    """返回该用户的 BM25 索引（缓存，懒加载）。"""
    with _lock:
        idx = _cache.get(user_id)
        if idx is None:
            idx = BM25Index(user_id, col)
            _cache[user_id] = idx
        return idx


def invalidate(user_id: int):
    """知识库写操作后调用，下次查询时重建索引。"""
    with _lock:
        _cache.pop(user_id, None)
