"""知识库（RAG）：文档导入、分块、BM25 检索、引用溯源。

设计约束与取舍见 index.py 顶部说明（零依赖、无 embeddings 接口）。
"""

from . import extract, index, store

__all__ = ["extract", "index", "store"]
