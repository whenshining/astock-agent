"""记忆存储：多轮会话持久化 + 跨会话长期记忆（SQLite）。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  INTEGER,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    role         TEXT NOT NULL,
    content      TEXT,
    tool_calls   TEXT,
    reasoning    TEXT,
    tool_call_id TEXT,
    name         TEXT,
    meta         TEXT,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT,
    content    TEXT UNIQUE,
    source     TEXT,
    created_at INTEGER,
    updated_at INTEGER
);
"""

_lock = threading.RLock()


def connect(db_file: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(db_file: str | Path) -> None:
    with _lock:
        conn = connect(db_file)
        try:
            conn.executescript(SCHEMA)
            # 轻量迁移：给老库补上后加的列（SQLite 没有 ADD COLUMN IF NOT EXISTS）
            for statement in ("ALTER TABLE messages ADD COLUMN reasoning TEXT",):
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------------ 会话

def create_session(db_file: str | Path, title: str = "新对话") -> str:
    session_id = uuid.uuid4().hex[:16]
    now = int(time.time())
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (session_id, title, now, now),
            )
            conn.commit()
        finally:
            conn.close()
    return session_id


def list_sessions(db_file: str | Path, limit: int = 100) -> list[dict[str, Any]]:
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count
                FROM sessions s
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_session(db_file: str | Path, session_id: str) -> dict[str, Any] | None:
    with _lock:
        conn = connect(db_file)
        try:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def rename_session(db_file: str | Path, session_id: str, title: str) -> None:
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title[:60], session_id))
            conn.commit()
        finally:
            conn.close()


def delete_session(db_file: str | Path, session_id: str) -> None:
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def clear_messages(db_file: str | Path, session_id: str) -> None:
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.commit()
        finally:
            conn.close()


# ------------------------------------------------------------------ 消息

def add_message(
    db_file: str | Path,
    session_id: str,
    role: str,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_call_id: str | None = None,
    name: str | None = None,
    meta: dict[str, Any] | None = None,
    reasoning: str | None = None,
) -> int:
    now = int(time.time())
    with _lock:
        conn = connect(db_file)
        try:
            cur = conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, tool_calls, reasoning, tool_call_id, name, meta, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    reasoning,
                    tool_call_id,
                    name,
                    json.dumps(meta, ensure_ascii=False) if meta else None,
                    now,
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def get_messages(db_file: str | Path, session_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """按时间正序返回会话消息（用于前端回显，跳过原始工具结果以免刷屏）。"""
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        finally:
            conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if item.get("tool_calls"):
            try:
                item["tool_calls"] = json.loads(item["tool_calls"])
            except ValueError:
                item["tool_calls"] = None
        if item.get("meta"):
            try:
                item["meta"] = json.loads(item["meta"])
            except ValueError:
                item["meta"] = None
        # 工具结果消息不直接回显（内容已通过 meta 里的摘要展示）
        out.append(item)
    return out


def build_llm_messages(db_file: str | Path, session_id: str, max_turns: int = 24) -> list[dict[str, Any]]:
    """把会话历史转成 API 消息格式（保留工具调用链路，但只取最近若干轮）。"""
    messages = get_messages(db_file, session_id)
    # 按轮截断：保留最后 max_turns 条 user 消息之后的所有内容
    user_indexes = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(user_indexes) > max_turns:
        start = user_indexes[-max_turns]
        messages = messages[start:]

    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        if role == "user":
            out.append({"role": "user", "content": msg.get("content") or ""})
        elif role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            # 官方要求：请求带 tools 时历史轮次的 reasoning_content 必须回传
            if msg.get("reasoning"):
                item["reasoning_content"] = msg["reasoning"]
            if msg.get("tool_calls"):
                item["tool_calls"] = msg["tool_calls"]
            out.append(item)
        elif role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id") or "",
                "content": msg.get("content") or "",
            })
    return out


# ------------------------------------------------------------------ 长期记忆

def add_memory(db_file: str | Path, content: str, kind: str = "preference", source: str = "manual") -> bool:
    """写入一条长期记忆（内容重复则忽略）。返回是否新增。"""
    text = (content or "").strip()
    if not text:
        return False
    now = int(time.time())
    with _lock:
        conn = connect(db_file)
        try:
            exists = conn.execute("SELECT id FROM memories WHERE content = ?", (text,)).fetchone()
            if exists:
                conn.execute("UPDATE memories SET updated_at = ? WHERE id = ?", (now, exists["id"]))
                conn.commit()
                return False
            conn.execute(
                "INSERT INTO memories (kind, content, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (kind, text, source, now, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def list_memories(db_file: str | Path) -> list[dict[str, Any]]:
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute("SELECT * FROM memories ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def delete_memory(db_file: str | Path, memory_id: int) -> None:
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            conn.commit()
        finally:
            conn.close()


def memories_prompt(db_file: str | Path, limit: int = 40) -> str:
    """把长期记忆拼成给模型的上下文片段。"""
    items = list_memories(db_file)[:limit]
    if not items:
        return ""
    labels = {
        "preference": "偏好",
        "strategy": "常用策略",
        "profile": "背景",
        "note": "备注",
    }
    lines = []
    for item in items:
        label = labels.get(item.get("kind") or "", "记忆")
        lines.append(f"- [{label}] {item['content']}")
    return "\n".join(lines)
