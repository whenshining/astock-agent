"""DeepSeek 用量统计、费用估算与余额查询。

定价取自官方 Models & Pricing（单位：美元 / 100 万 token）：

    模型              缓存命中    缓存未命中   输出
    deepseek-flash    $0.003     $0.15       $0.6
    deepseek-v4-pro   $0.022     $0.66       $1.98

以上是**低谷价**；高峰时段价格翻倍。高峰时段为 UTC 周一至周五
01:00-04:00 与 06:00-10:00（其余时间及周末均为低谷）。

费用按每条请求落库，方便统计今日/累计花费。
余额直接查官方 `GET /user/balance`，不落库（余额会随充值变化）。
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 每 1M token 的低谷价：(缓存命中, 缓存未命中, 输出)
OFFPEAK_PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.003, 0.15, 0.6),
    "deepseek-v4-pro": (0.022, 0.66, 1.98),
}
DEFAULT_MODEL_KEY = "deepseek-flash"

# 已下线的旧模型名统一按 Flash 计价
MODEL_ALIASES: dict[str, str] = {
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-chat": "deepseek-flash",
    "deepseek-reasoner": "deepseek-flash",
}

PEAK_MULTIPLIER = 2.0
# UTC 高峰时段（周一至周五）
PEAK_WINDOWS_UTC: tuple[tuple[int, int], ...] = ((1, 4), (6, 10))

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT,
    model              TEXT,
    cache_hit_tokens   INTEGER DEFAULT 0,
    cache_miss_tokens  INTEGER DEFAULT 0,
    prompt_tokens      INTEGER DEFAULT 0,
    completion_tokens  INTEGER DEFAULT 0,
    total_tokens       INTEGER DEFAULT 0,
    cost_usd           REAL DEFAULT 0,
    peak               INTEGER DEFAULT 0,
    created_at         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_usage_created ON usage_log(created_at);
"""

_lock = threading.Lock()
_balance_cache: dict[str, Any] = {"at": 0.0, "data": None}
BALANCE_TTL = 60  # 秒：余额查太频繁没必要


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


_initialized: set[str] = set()


def ensure_init(db_file: str | Path) -> None:
    """按需建表（每个库只做一次），这样调用方不必关心初始化顺序。"""
    key = str(db_file)
    if key not in _initialized:
        init(db_file)
        _initialized.add(key)


def normalize_model(model: str | None) -> str:
    name = (model or "").strip()
    if name in OFFPEAK_PRICES:
        return name
    if name in MODEL_ALIASES:
        return MODEL_ALIASES[name]
    return DEFAULT_MODEL_KEY


def price_for(model: str | None) -> tuple[float, float, float]:
    return OFFPEAK_PRICES.get(normalize_model(model), OFFPEAK_PRICES[DEFAULT_MODEL_KEY])


def is_peak(when: datetime | None = None) -> bool:
    """是否处于高峰计费时段（UTC 周一至周五）。"""
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if moment.weekday() >= 5:      # 周末全天低谷
        return False
    return any(start <= moment.hour < end for start, end in PEAK_WINDOWS_UTC)


def compute_cost(
    model: str | None,
    cache_hit_tokens: int,
    cache_miss_tokens: int,
    completion_tokens: int,
    when: datetime | None = None,
) -> float:
    """按官方价目估算单次请求费用（美元）。"""
    hit_rate, miss_rate, out_rate = price_for(model)
    multiplier = PEAK_MULTIPLIER if is_peak(when) else 1.0
    cost = (
        cache_hit_tokens * hit_rate
        + cache_miss_tokens * miss_rate
        + completion_tokens * out_rate
    ) / 1_000_000
    return cost * multiplier


def record(
    db_file: str | Path,
    session_id: str | None,
    model: str | None,
    usage: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """记录一次请求的 token 用量与费用。usage 来自接口返回。"""
    if not usage:
        return None

    def _int(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    cache_hit = _int("prompt_cache_hit_tokens", "cache_hit_tokens")
    cache_miss = _int("prompt_cache_miss_tokens", "cache_miss_tokens")
    prompt_tokens = _int("prompt_tokens")
    completion = _int("completion_tokens")

    # 有的响应只给 prompt_tokens 总数，那就按"全部未命中"处理（不会低估费用）
    if not cache_hit and not cache_miss and prompt_tokens:
        cache_miss = prompt_tokens

    if not (cache_hit or cache_miss or completion):
        return None

    ensure_init(db_file)
    now_utc = datetime.now(timezone.utc)
    peak = is_peak(now_utc)
    cost = compute_cost(model, cache_hit, cache_miss, completion, now_utc)
    total = prompt_tokens or (cache_hit + cache_miss)

    with _lock:
        conn = connect(db_file)
        try:
            conn.execute(
                """
                INSERT INTO usage_log
                    (session_id, model, cache_hit_tokens, cache_miss_tokens, prompt_tokens,
                     completion_tokens, total_tokens, cost_usd, peak, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id, normalize_model(model), cache_hit, cache_miss,
                    total, completion, total + completion, cost, int(peak), int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "prompt_tokens": total,
        "completion_tokens": completion,
        "total_tokens": total + completion,
        "cost_usd": cost,
        "peak": peak,
    }


def summary(db_file: str | Path) -> dict[str, Any]:
    """今日与累计用量汇总。"""
    ensure_init(db_file)
    start_of_day = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    )
    with _lock:
        conn = connect(db_file)
        try:
            try:
                today = conn.execute(
                    """
                    SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
                           COALESCE(SUM(total_tokens), 0) AS tokens,
                           COALESCE(SUM(cache_hit_tokens), 0) AS hit,
                           COALESCE(SUM(cache_miss_tokens), 0) AS miss,
                           COALESCE(SUM(completion_tokens), 0) AS completion
                    FROM usage_log WHERE created_at >= ?
                    """,
                    (start_of_day,),
                ).fetchone()
                overall = conn.execute(
                    """
                    SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
                           COALESCE(SUM(total_tokens), 0) AS tokens
                    FROM usage_log
                    """
                ).fetchone()
                by_model = conn.execute(
                    """
                    SELECT model, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost,
                           COALESCE(SUM(total_tokens), 0) AS tokens
                    FROM usage_log WHERE created_at >= ?
                    GROUP BY model ORDER BY cost DESC
                    """,
                    (start_of_day,),
                ).fetchall()
            except sqlite3.OperationalError:
                # 还没建表（比如刚启动就查询）
                return {"ready": False, "today": {}, "total": {}, "by_model": []}
        finally:
            conn.close()

    def _shape(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["cost_usd"] = round(data.get("cost", 0) or 0, 6)
        return data

    today_data = _shape(today)
    total_data = _shape(overall)
    prompt_tokens_today = (today_data.get("hit", 0) or 0) + (today_data.get("miss", 0) or 0)
    hit_rate = (today_data.get("hit", 0) / prompt_tokens_today * 100) if prompt_tokens_today else 0.0

    return {
        "ready": True,
        "today": {
            "calls": today_data["calls"],
            "cost_usd": today_data["cost_usd"],
            "tokens": today_data["tokens"],
            "prompt_tokens": prompt_tokens_today,
            "completion_tokens": today_data.get("completion", 0),
            "cache_hit_tokens": today_data.get("hit", 0),
            "cache_hit_rate": round(hit_rate, 1),
        },
        "total": {
            "calls": total_data["calls"],
            "cost_usd": total_data["cost_usd"],
            "tokens": total_data["tokens"],
        },
        "by_model": [
            {
                "model": row["model"],
                "calls": row["calls"],
                "cost_usd": round(row["cost"] or 0, 6),
                "tokens": row["tokens"],
            }
            for row in by_model
        ],
        "pricing": {
            "model": DEFAULT_MODEL_KEY,
            "offpeak": OFFPEAK_PRICES[DEFAULT_MODEL_KEY],
            "peak_multiplier": PEAK_MULTIPLIER,
            "is_peak_now": is_peak(),
        },
    }


# ------------------------------------------------------------------ 余额

def fetch_balance(api_key: str, base_url: str, timeout: int = 15, use_cache: bool = True) -> dict[str, Any]:
    """查询账户余额（官方 GET /user/balance）。"""
    now = time.time()
    if use_cache and _balance_cache["data"] and now - _balance_cache["at"] < BALANCE_TTL:
        return _balance_cache["data"]

    url = f"{base_url.rstrip('/')}/user/balance"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if exc.code == 401:
            raise RuntimeError("API Key 无效，无法查询余额（401）") from exc
        raise RuntimeError(f"余额查询失败（{exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 DeepSeek：{exc.reason}") from exc

    infos = data.get("balance_infos") or []
    primary = infos[0] if infos else {}
    result = {
        "ok": True,
        "is_available": bool(data.get("is_available")),
        "currency": primary.get("currency") or "",
        "total_balance": primary.get("total_balance") or "0",
        "granted_balance": primary.get("granted_balance") or "0",
        "topped_up_balance": primary.get("topped_up_balance") or "0",
        "all": infos,
        "fetched_at": int(now),
    }
    _balance_cache["at"] = now
    _balance_cache["data"] = result
    return result
