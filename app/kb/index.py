"""知识库检索：BM25（纯标准库实现）。

为什么不用向量检索：
  · 本项目坚持零第三方依赖（不能用 sentence-transformers / faiss / chromadb）
  · DeepSeek 官方 API 也没有 embeddings 接口
中文按「字符二元组」切分后做 BM25，对策略、规则、术语这类关键词密集的
文档效果足够好，而且完全离线、结果可解释（能说清为什么命中）。

检索结果自带引用信息（文件名 · 章节 · 第几段），用于强制模型标注来源。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from . import store

# 中文连续片段 -> 二元组；英文单词/数字单独成词
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_WORD = re.compile(r"[A-Za-z]{2,}|\d+(?:\.\d+)?")

K1 = 1.5
B = 0.75
# 低于这个分数视为「不相关」：宁可不给建议，也不硬凑内容塞给模型。
# 阈值是拿中文样例文档实测校准过的——定高了会漏掉"止损"这类短查询。
MIN_SCORE = 0.4


def tokenize(text: str) -> list[str]:
    """中文切二元组、英文数字按词切。无需分词库即可用于中文检索。"""
    tokens: list[str] = []
    for match in _CJK_RUN.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    for match in _WORD.finditer(text):
        tokens.append(match.group(0).lower())
    return tokens


class BM25Index:
    def __init__(self, chunks: list[dict[str, Any]], k1: float = K1, b: float = B) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.n = len(chunks)

        # 文件名和章节标题也参与检索（用户常按章节名提问）
        self.term_freq: list[Counter[str]] = []
        self.doc_len: list[int] = []
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.df: Counter[str] = Counter()

        for index, chunk in enumerate(chunks):
            haystack = " ".join(filter(None, (
                chunk.get("doc_name") or "",
                chunk.get("heading") or "",
                chunk.get("text") or "",
            )))
            counts = Counter(tokenize(haystack))
            self.term_freq.append(counts)
            self.doc_len.append(sum(counts.values()) or 1)
            for term, freq in counts.items():
                self.postings[term].append((index, freq))
                self.df[term] += 1

        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 1.0

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        if not df:
            return 0.0
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = MIN_SCORE,
        max_per_doc: int = 3,
    ) -> list[dict[str, Any]]:
        """返回命中的片段（含引用信息）；不相关的内容不会返回。"""
        if not self.n:
            return []
        query_terms = set(tokenize(query))
        if not query_terms:
            return []

        scores: dict[int, float] = defaultdict(float)
        matched_terms: dict[int, set[str]] = defaultdict(set)
        for term in query_terms:
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self._idf(term)
            if idf <= 0:
                continue
            for doc_index, freq in postings:
                denom = freq + self.k1 * (1 - self.b + self.b * self.doc_len[doc_index] / self.avgdl)
                scores[doc_index] += idf * freq * (self.k1 + 1) / denom
                matched_terms[doc_index].add(term)

        # 至少要有 1 个词命中；IDF 加权让"的/是"这类常见词单独命中时分数很低，
        # 不足以越过 MIN_SCORE，因此不需要额外硬性要求命中词数
        required = 1
        ranked = sorted(scores.items(), key=lambda item: -item[1])

        results: list[dict[str, Any]] = []
        per_doc: Counter[int] = Counter()
        for doc_index, score in ranked:
            if len(results) >= top_k:
                break
            if score < min_score:
                break
            if len(matched_terms[doc_index]) < required:
                continue
            chunk = self.chunks[doc_index]
            if per_doc[chunk["doc_id"]] >= max_per_doc:
                continue        # 同一篇文档最多贡献几条，避免刷屏
            per_doc[chunk["doc_id"]] += 1
            results.append({
                "chunk_id": chunk["chunk_id"],
                "doc_id": chunk["doc_id"],
                "doc_name": chunk["doc_name"],
                "seq": chunk["seq"],
                "heading": chunk.get("heading") or "",
                "text": chunk["text"],
                "score": round(score, 3),
                "matched_terms": len(matched_terms[doc_index]),
                "citation": build_citation(chunk),
            })
        return results


def build_citation(chunk: dict[str, Any]) -> str:
    """生成人类可读的出处标签。"""
    parts = [f"《{chunk.get('doc_name') or '知识库'}》"]
    heading = (chunk.get("heading") or "").strip()
    if heading:
        parts.append(heading)
    parts.append(f"第 {chunk.get('seq')} 段")
    return " · ".join(parts)


# ------------------------------------------------------------------ 索引缓存

_cache_lock = threading.Lock()
_cached_key: tuple[str, int] | None = None
_cached_index: BM25Index | None = None


def get_index(db_file: str | Path) -> BM25Index:
    """取检索索引；知识库内容变了会自动重建。"""
    global _cached_key, _cached_index
    current = (str(db_file), store.revision(db_file))
    with _cache_lock:
        if _cached_index is None or _cached_key != current:
            _cached_index = BM25Index(store.load_chunks(db_file))
            _cached_key = current
        return _cached_index


def reset_cache() -> None:
    global _cached_key, _cached_index
    with _cache_lock:
        _cached_key = None
        _cached_index = None


def search(db_file: str | Path, query: str, top_k: int = 5,
           min_score: float = MIN_SCORE) -> list[dict[str, Any]]:
    """对外入口：在知识库里检索。知识库为空时返回空列表。"""
    if not (query or "").strip():
        return []
    return get_index(db_file).search(query, top_k=top_k, min_score=min_score)
