"""自选策略：用户保存的选股条件组合。

设计要点：
  · 策略参数存成结构化 JSON，**执行时由程序读出来直接跑**，
    而不是让模型复述参数——模型复述数字很容易记错。
  · 参数用白名单校验（PARAM_SPEC），脏字段一律丢弃，
    免得存进去一堆跑不通的东西。
  · PARAM_SPEC 同时驱动前端表单，新增条件时只需改这一处。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT DEFAULT '',
    params      TEXT NOT NULL,
    plan        TEXT,
    source      TEXT DEFAULT 'manual',
    uses        INTEGER DEFAULT 0,
    created_at  INTEGER,
    updated_at  INTEGER
);
"""

_lock = threading.RLock()
_initialized: set[str] = set()

MAX_NAME = 40
MAX_DESC = 300

# 参数规格：既用于校验，也用于生成前端表单
# type: number / integer / bool / boards / sort / order / amount
PARAM_SPEC: list[dict[str, Any]] = [
    {"key": "min_price", "label": "股价下限", "unit": "元", "type": "number", "group": "价格"},
    {"key": "max_price", "label": "股价上限", "unit": "元", "type": "number", "group": "价格"},
    {"key": "chg_min", "label": "当日涨幅下限", "unit": "%", "type": "number", "group": "涨跌"},
    {"key": "chg_max", "label": "当日涨幅上限", "unit": "%", "type": "number", "group": "涨跌"},
    {"key": "chg5_min", "label": "5日涨幅下限", "unit": "%", "type": "number", "group": "涨跌"},
    {"key": "chg20_min", "label": "20日涨幅下限", "unit": "%", "type": "number", "group": "涨跌"},
    {"key": "consec_up_min", "label": "连涨天数下限", "unit": "天", "type": "integer", "group": "涨跌"},
    {"key": "vol_ratio_min", "label": "量比下限", "type": "number", "group": "量能"},
    {"key": "vol_ratio_max", "label": "量比上限", "type": "number", "group": "量能"},
    {"key": "amount_min", "label": "成交额下限", "unit": "亿元", "type": "amount",
     "scale": 100000000, "group": "量能",
     "help": "填 5 表示 5 亿元；内部会换算成元保存"},
    {"key": "amplitude_min", "label": "当日振幅下限", "unit": "%", "type": "number", "group": "波动"},
    {"key": "amplitude_max", "label": "当日振幅上限", "unit": "%", "type": "number", "group": "波动"},
    {"key": "amp20_max", "label": "20日均振幅上限", "unit": "%", "type": "number", "group": "波动"},
    {"key": "new_high60", "label": "创 60 日新高", "type": "bool", "group": "形态"},
    {"key": "new_high20", "label": "创 20 日新高", "type": "bool", "group": "形态"},
    {"key": "ma_bull", "label": "均线多头排列", "type": "bool", "group": "形态"},
    {"key": "min_drawdown60", "label": "距 60 日高点回撤不超过", "unit": "%（填正数，如 10）",
     "type": "drawdown", "group": "形态"},
    {"key": "boards", "label": "限定板块", "type": "boards", "group": "范围"},
    {"key": "watchlist", "label": "只看我的自选股", "type": "bool", "group": "范围",
     "help": "勾选后只在你通达信的自选股范围内筛选；每次执行时重新读取"},
    {"key": "sort_by", "label": "排序字段", "type": "sort", "group": "排序"},
    {"key": "sort_order", "label": "排序方向", "type": "order", "group": "排序"},
    {"key": "limit", "label": "返回条数", "type": "integer", "group": "排序"},
]

PARAM_KEYS = {spec["key"] for spec in PARAM_SPEC}

# ---------------------------------------------------------------- 交易计划
# 只有买入条件没法回测——不知道什么时候卖。这里补齐另一半：
# 卖出规则（止盈止损 + 条件卖出）和仓位规则。

PLAN_SPEC: list[dict[str, Any]] = [
    {"key": "hold_days", "label": "最多持有", "unit": "个交易日", "type": "integer", "default": 5},
    {"key": "stop_loss", "label": "止损线", "unit": "%（填正数，8 表示跌 8% 止损）",
     "type": "stop", "default": 8},
    {"key": "take_profit", "label": "止盈线", "unit": "%（留空表示不止盈）",
     "type": "number", "default": None},
    {"key": "max_positions", "label": "最多同时持有", "unit": "只", "type": "integer", "default": 5},
    {"key": "position_pct", "label": "单只仓位占比", "unit": "%", "type": "number", "default": 20},
]

PLAN_KEYS = {spec["key"] for spec in PLAN_SPEC}

# 条件卖出：除了固定止盈止损，还能按形态/量能条件卖出
EXIT_CONDITION_TYPES: list[dict[str, Any]] = [
    {"type": "ma_break", "label": "跌破均线", "value_type": "ma",
     "choices": [{"value": "ma5", "label": "MA5"}, {"value": "ma10", "label": "MA10"},
                 {"value": "ma20", "label": "MA20"}, {"value": "ma60", "label": "MA60"}]},
    {"type": "chg_below", "label": "单日跌幅超过", "unit": "%", "value_type": "number",
     "placeholder": "-5"},
    {"type": "vol_ratio_below", "label": "量比低于", "value_type": "number", "placeholder": "1"},
    {"type": "consec_down", "label": "连续下跌达到", "unit": "天", "value_type": "integer",
     "placeholder": "2"},
    {"type": "ma_dead", "label": "MA5 下穿 MA10（死叉）", "value_type": "none"},
    {"type": "profit_below", "label": "浮盈回落到", "unit": "%", "value_type": "number",
     "placeholder": "3"},
]

DEFAULT_PLAN: dict[str, Any] = {
    "hold_days": 5,
    "stop_loss": -8.0,
    "take_profit": None,
    "max_positions": 5,
    "position_pct": 20.0,
    "exit_conditions": [],
}


def normalize_plan(raw: Any) -> dict[str, Any]:
    """校验并规范化交易计划。止损对外填正数（8 表示跌 8%），内部统一存负数。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}

    plan = dict(DEFAULT_PLAN)

    def _num(key: str, low: float, high: float, default):
        value = raw.get(key, default)
        if value is None or value == "":
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, num))

    hold = _num("hold_days", 1, 120, 5)
    plan["hold_days"] = int(hold) if hold else 5

    # 止损：用户填的是正数（8 表示跌 8% 止损），要在正数空间里钳制再取负，
    # 否则会被 min/max 直接压到边界值上去。
    stop_raw = raw.get("stop_loss", 8)
    if stop_raw is None or stop_raw == "":
        plan["stop_loss"] = None
    else:
        try:
            stop_value = abs(float(stop_raw))
        except (TypeError, ValueError):
            stop_value = 8.0
        plan["stop_loss"] = -max(0.1, min(100.0, stop_value))

    take = _num("take_profit", 0.1, 1000, None)
    plan["take_profit"] = abs(take) if take is not None else None

    positions = _num("max_positions", 1, 50, 5)
    plan["max_positions"] = int(positions) if positions else 5

    pct = _num("position_pct", 1, 100, 20)
    plan["position_pct"] = pct if pct else 20.0

    conditions: list[dict[str, Any]] = []
    valid_types = {c["type"] for c in EXIT_CONDITION_TYPES}
    for item in (raw.get("exit_conditions") or [])[:6]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip()
        if kind == "expr":
            # 自定义表达式：存之前先解析一遍，别让写错的表达式进了库
            text = str(item.get("expr") or "").strip()
            if not text:
                continue
            try:
                from .backtest import expr as expr_mod

                expr_mod.compile_expression(text)
            except ValueError:
                continue
            conditions.append({"type": "expr", "expr": text[:120]})
            continue
        if kind not in valid_types:
            continue
        entry: dict[str, Any] = {"type": kind}
        if kind == "ma_break":
            ma = str(item.get("ma") or "ma10").strip().lower()
            entry["ma"] = ma if ma in ("ma5", "ma10", "ma20", "ma60") else "ma10"
        elif kind != "ma_dead":
            try:
                entry["value"] = float(item.get("value"))
            except (TypeError, ValueError):
                continue                    # 没填数值的条件直接丢掉
        conditions.append(entry)
    plan["exit_conditions"] = conditions
    return plan


def describe_plan(plan: dict[str, Any] | None) -> str:
    """把交易计划翻成人话。"""
    plan = normalize_plan(plan)
    parts = [f"最多持有 {plan['hold_days']} 个交易日"]
    if plan.get("stop_loss") is not None:
        parts.append(f"止损 {abs(plan['stop_loss']):.0f}%")
    if plan.get("take_profit") is not None:
        parts.append(f"止盈 {plan['take_profit']:.0f}%")

    specs = {c["type"]: c for c in EXIT_CONDITION_TYPES}
    for rule in plan.get("exit_conditions") or []:
        spec = specs.get(rule["type"], {})
        if rule["type"] == "expr":
            parts.append(f"满足「{rule.get('expr', '')}」时卖出")
            continue
        if rule["type"] == "ma_break":
            label = {"ma5": "MA5", "ma10": "MA10", "ma20": "MA20",
                     "ma60": "MA60"}.get(rule.get("ma", "ma10"), "MA10")
            parts.append(f"跌破{label}卖出")
        elif rule["type"] == "ma_dead":
            parts.append("MA5 死叉卖出")
        else:
            value = rule.get("value")
            text = f"{value:g}" if isinstance(value, float) else str(value)
            if rule["type"] == "profit_below":
                parts.append(f"浮盈回落到 {text}% 卖出")
            else:
                parts.append(f"{spec.get('label', rule['type'])} {text}{spec.get('unit', '')} 卖出")

    parts.append(f"最多同时持有 {plan['max_positions']} 只，单只 {plan['position_pct']:.0f}% 仓位")
    return "；".join(parts)

SORT_OPTIONS = [
    {"value": "chg", "label": "涨跌幅"},
    {"value": "chg5", "label": "5日涨幅"},
    {"value": "chg20", "label": "20日涨幅"},
    {"value": "vol_ratio", "label": "量比"},
    {"value": "amplitude", "label": "振幅"},
    {"value": "consec_up", "label": "连涨天数"},
    {"value": "close", "label": "收盘价"},
    {"value": "amount", "label": "成交额"},
    {"value": "drawdown60", "label": "距60日高点"},
]

BOARD_OPTIONS = [
    {"value": "sh_main", "label": "沪市主板"},
    {"value": "star", "label": "科创板"},
    {"value": "sz_main", "label": "深市主板"},
    {"value": "chinext", "label": "创业板"},
]


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
            # 轻量迁移：给老库补上后加的列
            for statement in ("ALTER TABLE strategies ADD COLUMN plan TEXT",):
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError:
                    pass
            conn.commit()
        finally:
            conn.close()


def ensure_init(db_file: str | Path) -> None:
    key = str(db_file)
    if key not in _initialized:
        init(db_file)
        _initialized.add(key)


# ------------------------------------------------------------------ 参数校验

def normalize_params(raw: Any) -> dict[str, Any]:
    """只保留白名单内的键，并转成正确类型；空值一律丢弃。"""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    if not isinstance(raw, dict):
        return {}

    spec_by_key = {spec["key"]: spec for spec in PARAM_SPEC}
    clean: dict[str, Any] = {}

    for key, value in raw.items():
        spec = spec_by_key.get(key)
        if not spec or value is None or value == "":
            continue
        kind = spec["type"]
        try:
            if kind in ("number", "amount", "drawdown"):
                num = float(value)
                if kind == "drawdown":
                    # 用户习惯填正数表示"回撤不超过 N%"，内部是负的百分比
                    num = -abs(num)
                elif kind == "amount":
                    # 对外单位是亿元（模型和界面都按亿元填），内部统一存成「元」，
                    # 这样执行时可以直接交给 screener，不用再换算
                    num = num * float(spec.get("scale") or 1)
                clean[key] = num
            elif kind == "integer":
                clean[key] = int(float(value))
            elif kind == "bool":
                if isinstance(value, str):
                    # 宽容一点：除了明确的否定值，其他非空字符串都当作 true
                    # （老版本可能存的是板块名这种字符串）
                    clean[key] = value.strip().lower() not in ("", "0", "false", "no", "否", "off")
                else:
                    clean[key] = bool(value)
                if not clean[key]:
                    clean.pop(key)          # false 等同于不设置
            elif kind == "boards":
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                valid = {b["value"] for b in BOARD_OPTIONS}
                picked = [v for v in value if v in valid]
                if picked:
                    clean[key] = picked
            elif kind == "sort":
                valid = {s["value"] for s in SORT_OPTIONS}
                text = str(value).strip()
                if text in valid:
                    clean[key] = text
            elif kind == "order":
                text = str(value).strip().lower()
                if text in ("asc", "desc"):
                    clean[key] = text
            elif kind in ("watchlist", "text"):
                # 自选板块名等文本型条件：原样保留，执行时再解析
                text = str(value).strip()
                if text:
                    clean[key] = text[:40]
        except (TypeError, ValueError):
            continue

    if "limit" in clean:
        clean["limit"] = max(1, min(500, int(clean["limit"])))
    return clean


def describe(params: dict[str, Any]) -> str:
    """把参数翻成人话，用于展示和注入提示词。"""
    from .engine import screener

    parts = screener.describe_conditions(params, None)
    parts = [p for p in parts if not p.startswith("数据截至")]
    sort_key = params.get("sort_by")
    if sort_key:
        label = screener.SORT_FIELDS.get(sort_key, (None, sort_key))[1]
        direction = "升序" if str(params.get("sort_order", "")).lower() == "asc" else "降序"
        parts.append(f"按{label}{direction}")
    if not parts:
        return "无筛选条件（全市场）"
    return "；".join(parts)


# ------------------------------------------------------------------ CRUD

def _row_to_dict(row: sqlite3.Row, with_params: bool = True) -> dict[str, Any]:
    data = dict(row)
    plan = normalize_plan(data.get("plan"))
    data["plan"] = plan
    data["plan_summary"] = describe_plan(plan)
    if with_params:
        try:
            data["params"] = json.loads(data.get("params") or "{}")
        except ValueError:
            data["params"] = {}
        data["summary"] = describe(data["params"])
    else:
        data.pop("params", None)
    return data


def save(
    db_file: str | Path,
    name: str,
    description: str = "",
    params: Any = None,
    source: str = "manual",
    strategy_id: int | None = None,
    plan: Any = None,
) -> dict[str, Any]:
    """新建或更新策略。同名（不分大小写）视为更新。"""
    ensure_init(db_file)
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("策略名称不能为空")
    if len(clean_name) > MAX_NAME:
        raise ValueError(f"策略名称不能超过 {MAX_NAME} 个字")

    clean_params = normalize_params(params)
    if not clean_params:
        raise ValueError("策略至少要包含一个筛选条件")
    clean_plan = normalize_plan(plan)

    now = int(time.time())
    with _lock:
        conn = connect(db_file)
        try:
            target = None
            if strategy_id:
                target = conn.execute("SELECT * FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            if target is None:
                target = conn.execute(
                    "SELECT * FROM strategies WHERE name = ? COLLATE NOCASE", (clean_name,)
                ).fetchone()

            if target:
                conn.execute(
                    "UPDATE strategies SET name = ?, description = ?, params = ?, plan = ?, "
                    "updated_at = ? WHERE id = ?",
                    (clean_name, (description or "").strip()[:MAX_DESC],
                     json.dumps(clean_params, ensure_ascii=False),
                     json.dumps(clean_plan, ensure_ascii=False), now, target["id"]),
                )
                new_id = target["id"]
                replaced = True
            else:
                cursor = conn.execute(
                    "INSERT INTO strategies (name, description, params, plan, source, uses, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (clean_name, (description or "").strip()[:MAX_DESC],
                     json.dumps(clean_params, ensure_ascii=False),
                     json.dumps(clean_plan, ensure_ascii=False), source, now, now),
                )
                new_id = int(cursor.lastrowid or 0)
                replaced = False
            conn.commit()
            row = conn.execute("SELECT * FROM strategies WHERE id = ?", (new_id,)).fetchone()
            result = _row_to_dict(row)
            result["replaced"] = replaced
            return result
        finally:
            conn.close()


def list_all(db_file: str | Path, query: str = "") -> list[dict[str, Any]]:
    """列出策略；query 非空时按名称/描述/条件文字搜索。"""
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            rows = conn.execute(
                "SELECT * FROM strategies ORDER BY updated_at DESC"
            ).fetchall()
        finally:
            conn.close()

    items = [_row_to_dict(row) for row in rows]
    keyword = (query or "").strip().lower()
    if keyword:
        items = [
            item for item in items
            if keyword in (item["name"] or "").lower()
            or keyword in (item.get("description") or "").lower()
            or keyword in (item.get("summary") or "").lower()
        ]
    return items


def find(db_file: str | Path, name_or_id: str | int) -> dict[str, Any] | None:
    """按 ID、名称（精确优先，其次模糊包含）查找策略。"""
    ensure_init(db_file)
    text = str(name_or_id).strip()
    if not text:
        return None

    with _lock:
        conn = connect(db_file)
        try:
            row = None
            if text.isdigit():
                row = conn.execute("SELECT * FROM strategies WHERE id = ?", (int(text),)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM strategies WHERE name = ? COLLATE NOCASE", (text,)
                ).fetchone()
            if row is None:
                # 模糊匹配：用户常说"用那个放量的策略"
                row = conn.execute(
                    "SELECT * FROM strategies WHERE name LIKE ? COLLATE NOCASE "
                    "ORDER BY length(name) ASC LIMIT 1",
                    (f"%{text}%",),
                ).fetchone()
            return _row_to_dict(row) if row else None
        finally:
            conn.close()


def update(db_file: str | Path, strategy_id: int, **fields: Any) -> dict[str, Any] | None:
    ensure_init(db_file)
    current = find(db_file, strategy_id)
    if not current:
        return None
    return save(
        db_file,
        name=fields.get("name") or current["name"],
        description=fields.get("description", current.get("description") or ""),
        params=fields.get("params", current["params"]),
        strategy_id=strategy_id,
    )


def delete(db_file: str | Path, strategy_id: int) -> bool:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            row = conn.execute("SELECT id FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
            conn.commit()
            return True
        finally:
            conn.close()


def mark_used(db_file: str | Path, strategy_id: int) -> None:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            conn.execute("UPDATE strategies SET uses = uses + 1 WHERE id = ?", (strategy_id,))
            conn.commit()
        finally:
            conn.close()


def stats(db_file: str | Path) -> dict[str, int]:
    ensure_init(db_file)
    with _lock:
        conn = connect(db_file)
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM strategies").fetchone()["n"]
            uses = conn.execute("SELECT COALESCE(SUM(uses), 0) AS n FROM strategies").fetchone()["n"]
            return {"count": int(count), "uses": int(uses)}
        finally:
            conn.close()


def prompt_summary(db_file: str | Path, limit: int = 20) -> str:
    """给系统提示词用的策略清单（只列名称和条件，节省 token）。"""
    items = list_all(db_file)[:limit]
    if not items:
        return ""
    lines = []
    for item in items:
        desc = (item.get("description") or "").strip()
        line = f"- 「{item['name']}」：{item['summary']}"
        if desc:
            line += f"（{desc}）"
        lines.append(line)
    return "\n".join(lines)
