"""选股查询：在指标缓存上做条件过滤 + 排序。

所有条件都是可选的；没给的条件不加门槛（避免凭空提高选股标准）。
列名与排序字段走白名单映射，杜绝 SQL 注入。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

from .store import connect, get_meta

# 排序字段白名单：对外名称 -> (SQL 列, 中文标签)
SORT_FIELDS: dict[str, tuple[str, str]] = {
    "chg": ("chg", "涨跌幅"),
    "chg5": ("chg5", "5日涨幅"),
    "chg20": ("chg20", "20日涨幅"),
    "vol_ratio": ("vol_ratio", "量比"),
    "amplitude": ("amplitude", "振幅"),
    "consec_up": ("consec_up", "连涨天数"),
    "close": ("close", "收盘价"),
    "amount": ("amount", "成交额"),
    "drawdown60": ("drawdown60", "距60日高点"),
    "turnover_amount": ("amount_avg5", "5日均额"),
}

SORT_ALIASES = {
    "涨跌幅": "chg", "涨幅": "chg", "量比": "vol_ratio", "振幅": "amplitude",
    "连涨": "consec_up", "连涨天数": "consec_up", "收盘价": "close", "价格": "close",
    "成交额": "amount", "5日涨幅": "chg5", "20日涨幅": "chg20",
    "volratio": "vol_ratio", "price": "chg",
}

# 板块白名单：对外名称 -> 代码前缀
BOARDS: dict[str, tuple[str, ...]] = {
    "sh_main": ("600", "601", "603", "605"),
    "star": ("688", "689"),
    "sz_main": ("000", "001", "002", "003"),
    "chinext": ("300", "301"),
}

BOARD_LABELS = {
    "sh_main": "沪市主板",
    "star": "科创板",
    "sz_main": "深市主板",
    "chinext": "创业板",
}

# 数值型条件：(参数名, SQL 列, 运算符)
_NUMERIC_FILTERS: list[tuple[str, str, str]] = [
    ("min_price", "close", ">="),
    ("max_price", "close", "<="),
    ("chg_min", "chg", ">="),
    ("chg_max", "chg", "<="),
    ("chg5_min", "chg5", ">="),
    ("chg20_min", "chg20", ">="),
    ("vol_ratio_min", "vol_ratio", ">="),
    ("vol_ratio_max", "vol_ratio", "<="),
    ("amplitude_min", "amplitude", ">="),
    ("amplitude_max", "amplitude", "<="),
    ("consec_up_min", "consec_up", ">="),
    ("amount_min", "amount", ">="),
    ("amount_max", "amount", "<="),
    ("amp20_max", "amp20", "<="),
]


def normalize_sort(value: str | None) -> str:
    if not value:
        return "chg"
    key = str(value).strip()
    if key in SORT_FIELDS:
        return key
    if key in SORT_ALIASES:
        return SORT_ALIASES[key]
    return "chg"


def describe_conditions(params: dict[str, Any], trade_date: int | None) -> list[str]:
    """把结构化条件翻译成人话，用于回报命中逻辑。"""
    parts: list[str] = []
    if params.get("min_price") is not None or params.get("max_price") is not None:
        lo = params.get("min_price")
        hi = params.get("max_price")
        if lo is not None and hi is not None:
            parts.append(f"股价 {lo}~{hi} 元")
        elif lo is not None:
            parts.append(f"股价 ≥ {lo} 元")
        else:
            parts.append(f"股价 ≤ {hi} 元")
    if params.get("chg_min") is not None or params.get("chg_max") is not None:
        lo, hi = params.get("chg_min"), params.get("chg_max")
        if lo is not None and hi is not None:
            parts.append(f"当日涨跌幅 {lo}%~{hi}%")
        elif lo is not None:
            parts.append(f"当日涨幅 ≥ {lo}%")
        else:
            parts.append(f"当日涨幅 ≤ {hi}%")
    for key, label in (("chg5_min", "5日涨幅"), ("chg20_min", "20日涨幅")):
        if params.get(key) is not None:
            parts.append(f"{label} ≥ {params[key]}%")
    if params.get("vol_ratio_min") is not None or params.get("vol_ratio_max") is not None:
        lo, hi = params.get("vol_ratio_min"), params.get("vol_ratio_max")
        if lo is not None and hi is not None:
            parts.append(f"量比 {lo}~{hi}")
        elif lo is not None:
            parts.append(f"量比 ≥ {lo}")
        else:
            parts.append(f"量比 ≤ {hi}")
    if params.get("amplitude_min") is not None or params.get("amplitude_max") is not None:
        lo, hi = params.get("amplitude_min"), params.get("amplitude_max")
        if lo is not None and hi is not None:
            parts.append(f"振幅 {lo}%~{hi}%")
        elif lo is not None:
            parts.append(f"振幅 ≥ {lo}%")
        else:
            parts.append(f"振幅 ≤ {hi}%")
    if params.get("consec_up_min") is not None:
        parts.append(f"连涨 ≥ {params['consec_up_min']} 天")
    if params.get("amount_min") is not None:
        # 内部单位是「元」，展示成亿元要除以 1e8（原来写成 /10000 是元->万元，标错了一个量级）
        parts.append(f"成交额 ≥ {params['amount_min'] / 1e8:.2f} 亿元")
    if params.get("amount_max") is not None:
        parts.append(f"成交额 ≤ {params['amount_max'] / 1e8:.2f} 亿元")
    if params.get("amp20_max") is not None:
        parts.append(f"近20日平均振幅 ≤ {params['amp20_max']}%")
    if params.get("new_high60"):
        parts.append("创 60 日新高")
    if params.get("new_high20"):
        parts.append("创 20 日新高")
    if params.get("ma_bull"):
        parts.append("均线多头排列(MA5>MA10>MA20 且 收盘>MA5)")
    if params.get("min_drawdown60") is not None:
        parts.append(f"距 60 日高点 ≥ {params['min_drawdown60']}%")
    boards = params.get("boards")
    if boards:
        labels = [BOARD_LABELS.get(b, b) for b in boards]
        parts.append("板块：" + "/".join(labels))
    if params.get("watchlist"):
        parts.append("仅在我的自选股中筛选")
    if trade_date:
        parts.append(f"数据截至 {trade_date}")
    return parts


def screen(
    db_file: str | Path,
    params: dict[str, Any],
    latest_only: bool = True,
) -> dict[str, Any]:
    """按条件筛选，返回命中列表。"""
    started = time.perf_counter()
    conn = connect(db_file)
    try:
        trade_date = None
        raw_date = get_meta(conn, "trade_date")
        if raw_date:
            trade_date = int(raw_date)

        where: list[str] = []
        values: list[Any] = []

        if latest_only and trade_date:
            where.append("date = ?")
            values.append(trade_date)

        for key, column, op in _NUMERIC_FILTERS:
            value = params.get(key)
            if value is None or value == "":
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            where.append(f"{column} {op} ?")
            values.append(num)

        if params.get("new_high60"):
            where.append("new_high60 = 1")
        if params.get("new_high20"):
            where.append("new_high20 = 1")
        if params.get("ma_bull"):
            where.append("ma_bull = 1")
        if params.get("min_drawdown60") is not None:
            try:
                where.append("drawdown60 >= ?")
                values.append(float(params["min_drawdown60"]))
            except (TypeError, ValueError):
                pass

        boards = params.get("boards")
        if boards:
            prefixes: list[str] = []
            for board in boards:
                prefixes.extend(BOARDS.get(board, ()))
            if prefixes:
                clause = " OR ".join("code LIKE ?" for _ in prefixes)
                where.append(f"({clause})")
                values.extend(f"{p}%" for p in prefixes)

        # 限定在指定代码集合内（自选股/自定义板块用）
        codes = params.get("codes")
        if codes:
            placeholders = ",".join("?" * len(codes))
            where.append(f"code IN ({placeholders})")
            values.extend(codes)

        sql_where = (" WHERE " + " AND ".join(where)) if where else ""

        total = conn.execute(f"SELECT COUNT(*) AS n FROM metrics{sql_where}", values).fetchone()["n"]

        sort_key = normalize_sort(params.get("sort_by"))
        sort_column, sort_label = SORT_FIELDS[sort_key]
        descending = params.get("sort_desc", True)
        if str(params.get("sort_order", "")).lower() in ("asc", "升序"):
            descending = False
        direction = "DESC" if descending else "ASC"

        limit = params.get("limit")
        try:
            limit = max(1, min(500, int(limit))) if limit else 50
        except (TypeError, ValueError):
            limit = 50
        try:
            offset = max(0, int(params.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0

        rows = conn.execute(
            f"SELECT * FROM metrics{sql_where} ORDER BY {sort_column} {direction}, code ASC LIMIT ? OFFSET ?",
            values + [limit, offset],
        ).fetchall()

        stocks = [dict(row) for row in rows]
    finally:
        conn.close()

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    return {
        "total": total,
        "returned": len(stocks),
        "trade_date": trade_date,
        "sort_by": sort_key,
        "sort_label": sort_label,
        "sort_desc": descending,
        "conditions": describe_conditions(params, trade_date),
        "stocks": stocks,
        "elapsed_ms": elapsed_ms,
    }


def stats(db_file: str | Path) -> dict[str, Any]:
    """市场概览：涨跌家数、涨停/跌停、均线多头数量等。"""
    conn = connect(db_file)
    try:
        raw_date = get_meta(conn, "trade_date")
        trade_date = int(raw_date) if raw_date else None
        if not trade_date:
            return {"ready": False}
        row = conn.execute(
            """
            SELECT
                COUNT(*)                                   AS total,
                SUM(CASE WHEN chg > 0 THEN 1 ELSE 0 END)    AS up,
                SUM(CASE WHEN chg < 0 THEN 1 ELSE 0 END)    AS down,
                SUM(CASE WHEN chg = 0 THEN 1 ELSE 0 END)    AS flat,
                SUM(CASE WHEN chg >= 9.8 THEN 1 ELSE 0 END) AS limit_up,
                SUM(CASE WHEN chg <= -9.8 THEN 1 ELSE 0 END) AS limit_down,
                SUM(ma_bull)                                AS ma_bull,
                SUM(new_high60)                             AS new_high60,
                AVG(chg)                                    AS avg_chg
            FROM metrics WHERE date = ?
            """,
            (trade_date,),
        ).fetchone()
        result = dict(row)
        result["ready"] = True
        result["trade_date"] = trade_date
        result["avg_chg"] = round(result["avg_chg"] or 0, 2)
        return result
    finally:
        conn.close()


def stock_detail(db_file: str | Path, code: str) -> dict[str, Any] | None:
    conn = connect(db_file)
    try:
        row = conn.execute("SELECT * FROM metrics WHERE code = ?", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
