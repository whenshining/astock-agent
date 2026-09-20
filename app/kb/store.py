"""知识库存储：文档导入、分块、增删查。

切块策略：按空行分段 -> 按标题累积 -> 攒到目标长度切一块，并保留少量
重叠（避免一句话被拦腰截断后检索不到）。每个片段都记下所属标题，
用于给模型提供引用上下文（"《文件名》· 第 2 章 均线多头"）。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .extract import ExtractError, extract

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    kind        TEXT,
    size        INTEGER DEFAULT 0,
    chars       INTEGER DEFAULT 0,
    chunk_count INTEGER DEFAULT 0,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   INTEGER NOT NULL,
    seq      INTEGER NOT NULL,
    heading  TEXT,
    text     TEXT NOT NULL,
    position INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc ON kb_chunks(doc_id);

CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_lock = threading.RLock()
_initialized: set[str] = set()

TARGET_CHUNK = 520      # 目标片段长度（字符）
OVERLAP = 80            # 相邻片段重叠长度
MIN_CHUNK = 24          # 过短的片段丢弃


def connect(db_file: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(db_file: str | Path) -> None:
    with _lock:
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db_file)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def ensure_init(db_file: str | Path) -> None:
    key = str(db_file)
    if key not in _initialized:
        init(db_file)
        _initialized.add(key)


# ------------------------------------------------------------------ 切块

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")


def _split_long_block(block: str, limit: int) -> list[str]:
    """把超长段落按句子切开（避免一个段落就撑爆单个片段）。"""
    if len(block) <= limit:
        return [block]
    sentences = [s for s in _SENTENCE_SPLIT.split(block) if s.strip()]
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current) + len(sentence) > limit and current:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts or [block]


def chunk_text(text: str, target: int = TARGET_CHUNK, overlap: int = OVERLAP) -> list[dict[str, Any]]:
    """把整篇文本切成片段，每个片段带上「所属章节」。

    段落先按标题归入章节，再按长度累积成片段；跨章节时强制切分，
    这样片段与章节对齐，引用时能精确定位到「《文件》· 某章节」。
    """
    # 1) 给每个段落标记它属于哪个章节（标题行本身不进正文）
    sections: list[tuple[str, str]] = []
    heading = ""
    for raw_block in text.split("\n\n"):
        block = raw_block.strip()
        if not block:
            continue
        # Markdown 里标题后面常常紧跟正文、中间只有一个换行，
        # 这时整块会以 "#" 开头——必须把标题行摘掉、保留正文，
        # 否则整段内容会被当成标题丢掉（曾经真的踩过这个坑）。
        lines = block.split("\n")
        if lines[0].lstrip().startswith("#"):
            heading = lines[0].lstrip().lstrip("#").strip() or heading
            block = "\n".join(lines[1:]).strip()
            if not block:
                continue
        for part in _split_long_block(block, target * 2):
            sections.append((heading, part))

    # 2) 累积成片段
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    current_len = 0
    current_heading = ""

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        body = "\n\n".join(current).strip()
        if len(body) >= MIN_CHUNK:
            chunks.append({"heading": current_heading, "text": body})
        # 用尾部做重叠，保证被切断的句子仍能被检索到
        tail = body[-overlap:] if len(body) > overlap else ""
        current = [tail] if tail else []
        current_len = len(tail)

    for section_heading, part in sections:
        # 换了章节就从新的一块开始，避免不同主题混在一条引用里
        if section_heading != current_heading and current_len >= MIN_CHUNK:
            flush()
        if not current:
            current_heading = section_heading
        if current_len and current_len + len(part) > target:
            flush()
            current_heading = section_heading
        current.append(part)
        current_len += len(part) + 2
    flush()

    return chunks


# ------------------------------------------------------------------ 文档 CRUD

def _bump_revision(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO kb_meta (key, value) VALUES ('revision', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"
    )


def import_document(db_file: str | Path, filename: str, raw: bytes) -> dict[str, Any]:
    """导入一个文档。重名会覆盖旧版本（避免重复导入造成检索噪声）。"""
    ensure_init(db_file)
    name = Path(filename).name.strip() or "未命名"
    text = extract(name, raw)          # 解析失败会抛 ExtractError
    chunks = chunk_text(text)
    if not chunks:
        raise ExtractError("文档内容太少，没有可用的知识片段")

    now = int(time.time())
    kind = Path(name).suffix.lower().lstrip(".") or "txt"

    with _lock:
        conn = connect(db_file)
        try:
            old = conn.execute("SELECT id FROM kb_docs WHERE name = ?", (name,)).fetchone()
            if old:
                conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (old["id"],))
                conn.execute("DELETE FROM kb_docs WHERE id = ?", (old["id"],))
            cursor = conn.execute(
                "INSERT INTO kb_docs (name, kind, size, chars, chunk_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, kind, len(raw), len(text), len(chunks), now),
            )
            doc_id = int(cursor.lastrowid or 0)
            conn.executemany(
                "INSERT INTO kb_chunks (doc_id, seq, heading, text, position) VALUES (?, ?, ?, ?, ?)",
                [(doc_id, i + 1, c["heading"], c["text"], i) for i, c in enumerate(chunks)],
            )
            _bump_revision(conn)
            conn.commit()
        finally:
            conn.close()

    return {
        "id": doc_id,
        "name": name,
        "kind": kind,
        "chars": len(text),
        "chunks": len(chunks),
        "replaced": bool(old),
    }


def list_documents(db_file: str | Path) -> list[dict[str, Any]]:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute(
                "SELECT id, name, kind, size, chars, chunk_count, created_at "
                "FROM kb_docs ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def delete_document(db_file: str | Path, doc_id: int) -> bool:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            row = conn.execute("SELECT id FROM kb_docs WHERE id = ?", (doc_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM kb_chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM kb_docs WHERE id = ?", (doc_id,))
            _bump_revision(conn)
            conn.commit()
            return True
        finally:
            conn.close()


def clear_documents(db_file: str | Path) -> int:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM kb_docs").fetchone()["n"]
            conn.execute("DELETE FROM kb_chunks")
            conn.execute("DELETE FROM kb_docs")
            _bump_revision(conn)
            conn.commit()
            return int(count)
        finally:
            conn.close()


def load_chunks(db_file: str | Path) -> list[dict[str, Any]]:
    """取出全部片段（连同所属文档名），用于建检索索引。"""
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute(
                """
                SELECT c.id AS chunk_id, c.doc_id, c.seq, c.heading, c.text, d.name AS doc_name
                FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id
                ORDER BY c.doc_id, c.seq
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def revision(db_file: str | Path) -> int:
    """内容版本号，用于判断检索索引要不要重建。"""
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            row = conn.execute("SELECT value FROM kb_meta WHERE key = 'revision'").fetchone()
            return int(row["value"]) if row else 0
        finally:
            conn.close()


def stats(db_file: str | Path) -> dict[str, Any]:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            docs = conn.execute("SELECT COUNT(*) AS n FROM kb_docs").fetchone()["n"]
            chunks = conn.execute("SELECT COUNT(*) AS n FROM kb_chunks").fetchone()["n"]
            chars = conn.execute("SELECT COALESCE(SUM(chars), 0) AS n FROM kb_docs").fetchone()["n"]
            return {
                "documents": int(docs),
                "chunks": int(chunks),
                "chars": int(chars),
                "ready": int(docs) > 0,
            }
        finally:
            conn.close()
