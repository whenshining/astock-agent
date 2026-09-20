"""卖出条件的自定义表达式。

原来只能从固定的六个预设里挑（跌破均线 / 单日跌幅 / 量比 / 连跌 / 死叉 / 浮盈回落），
组合不了、也改不了。这里给一个小语法，让用户直接写：

    收盘 < MA10
    量比 < 0.8
    收盘 < MA10 且 量比 < 0.8
    跌幅 <= -5 或 浮盈 <= 3

语法刻意做得很小：只支持「字段 比较符 数值」用 且/或 连起来。
**不用 eval**，所以不存在注入问题；解析失败会给出明确的中文提示。
"""

from __future__ import annotations

import re
from typing import Any

# 可用字段：中文名和英文名都认（MA 系列大小写不敏感）
FIELD_ALIASES: dict[str, str] = {
    "收盘": "close", "收盘价": "close", "close": "close",
    "开盘": "open", "开盘价": "open", "open": "open",
    "最高": "high", "最高价": "high", "high": "high",
    "最低": "low", "最低价": "low", "low": "low",
    "涨幅": "chg", "涨跌幅": "chg", "涨跌": "chg", "chg": "chg",
    "跌幅": "chg",                     # 写「跌幅 <= -5」也读得懂
    "量比": "vol_ratio", "vol_ratio": "vol_ratio",
    "成交额": "amount_yi", "成交金额": "amount_yi", "amount": "amount_yi",
    "振幅": "amplitude", "amplitude": "amplitude",
    "连涨": "consec_up", "连涨天数": "consec_up", "consec_up": "consec_up",
    "连跌": "consec_down", "连跌天数": "consec_down", "consec_down": "consec_down",
    "浮盈": "profit", "浮动盈亏": "profit", "盈亏": "profit", "profit": "profit",
    "距高点": "drawdown60", "回撤": "drawdown60", "drawdown60": "drawdown60",
    "ma5": "ma5", "ma10": "ma10", "ma20": "ma20", "ma60": "ma60",
}

# 给界面展示用（顺序有意义，常用的放前面）
FIELD_HELP: list[dict[str, str]] = [
    {"name": "收盘", "desc": "当日收盘价"},
    {"name": "开盘 / 最高 / 最低", "desc": "当日价格"},
    {"name": "涨幅", "desc": "当日涨跌幅 %（负数是跌）"},
    {"name": "量比", "desc": "当日量比"},
    {"name": "成交额", "desc": "当日成交额（亿元）"},
    {"name": "振幅", "desc": "当日振幅 %"},
    {"name": "MA5 / MA10 / MA20 / MA60", "desc": "对应均线值"},
    {"name": "连涨 / 连跌", "desc": "连续上涨/下跌天数"},
    {"name": "浮盈", "desc": "相对买入价的盈亏 %"},
    {"name": "距高点", "desc": "距 60 日最高价的回撤 %（负数）"},
]

EXAMPLES: list[str] = [
    "收盘 < MA10",
    "量比 < 0.8",
    "收盘 < MA10 且 量比 < 0.8",
    "跌幅 <= -5 或 浮盈 <= 3",
]

_OR_SPLIT = re.compile(r"\s*(?:或|或者|or|\|\|)\s*", re.IGNORECASE)
_AND_SPLIT = re.compile(r"\s*(?:且|并且|而且|and|&&)\s*", re.IGNORECASE)
# 「字段 比较符 右边」，右边**既可以是数字，也可以是另一个字段**
# （「收盘 < MA10」是最常见的卖出条件，右边必须是字段才行）
_ATOM = re.compile(
    r"^\s*([A-Za-z0-9\u4e00-\u9fa5_]+)\s*(<=|>=|==|!=|<|>|=)\s*"
    r"([A-Za-z0-9\u4e00-\u9fa5_]+|-?\d+(?:\.\d+)?)\s*$"
)
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")

# 操作数：("num", 1.5) 或 ("field", "ma10")
Operand = tuple[str, Any]
Atom = tuple[str, str, Operand]
# 编译后的结构：外层是「或」，内层是「且」
Compiled = list[list[Atom]]


def compile_expression(text: str) -> Compiled:
    """把表达式编译成可求值的结构。语法有问题时抛 ValueError（消息是给用户看的）。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("表达式不能为空")

    groups: Compiled = []
    for or_part in _OR_SPLIT.split(raw):
        or_part = or_part.strip()
        if not or_part:
            raise ValueError(f"「或」两边都要有条件：{raw}")
        and_group: list[Atom] = []
        for atom_text in _AND_SPLIT.split(or_part):
            atom_text = atom_text.strip()
            if not atom_text:
                raise ValueError(f"「且」两边都要有条件：{raw}")
            match = _ATOM.match(atom_text)
            if not match:
                raise ValueError(
                    f"看不懂「{atom_text}」。写法是「字段 比较符 数值或字段」，"
                    f"例如：收盘 < MA10、量比 < 0.8"
                )
            name, op, right_text = match.group(1), match.group(2), match.group(3)
            key = resolve_field(name)
            if key is None:
                raise ValueError(f"没有「{name}」这个字段。可用字段见下方说明。")
            if op == "=":
                op = "=="

            if _NUMBER.match(right_text):
                operand: Operand = ("num", float(right_text))
            else:
                right_key = resolve_field(right_text)
                if right_key is None:
                    raise ValueError(
                        f"「{right_text}」既不是数字，也不是可用字段。"
                        f"如果这里想写数值，请填纯数字（如 -5、0.8）"
                    )
                operand = ("field", right_key)

            and_group.append((key, op, operand))
        groups.append(and_group)

    if not groups:
        raise ValueError("表达式不能为空")
    return groups


def resolve_field(name: str) -> str | None:
    """把用户写的字段名解析成内部键；认不出来返回 None。"""
    text = (name or "").strip()
    lowered = text.lower()
    if lowered in FIELD_ALIASES:
        return FIELD_ALIASES[lowered]
    if text in FIELD_ALIASES:
        return FIELD_ALIASES[text]
    return None


def field_value(key: str, metrics: dict[str, Any], buy_price: float) -> float | None:
    """取字段在当日的值；取不到返回 None（该条件视为不成立）。"""
    if key == "amount_yi":
        amount = metrics.get("amount")
        return (amount / 1e8) if amount is not None else None
    if key == "profit":
        close = metrics.get("close")
        if close is None or not buy_price:
            return None
        return (close / buy_price - 1) * 100
    value = metrics.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _compare(left: float, op: str, right: float) -> bool:
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


def evaluate(groups: Compiled, metrics: dict[str, Any], buy_price: float) -> bool:
    """求值：外层或、内层且。任何取不到的字段按「条件不成立」处理——宁可不卖。"""
    for and_group in groups:
        ok = True
        for key, op, operand in and_group:
            left = field_value(key, metrics, buy_price)
            if left is None:
                ok = False
                break
            if operand[0] == "num":
                right = operand[1]
            else:
                right = field_value(operand[1], metrics, buy_price)
                if right is None:
                    ok = False
                    break
            if not _compare(left, op, right):
                ok = False
                break
        if ok:
            return True
    return False


def describe(text: str) -> str:
    """把表达式整理成统一的展示写法（解析不了就原样返回）。"""
    try:
        groups = compile_expression(text)
    except ValueError:
        return (text or "").strip()
    op_label = {"<": "<", "<=": "≤", ">": ">", ">=": "≥", "==": "=", "!=": "≠"}
    parts = []
    for and_group in groups:
        atoms = []
        for key, op, operand in and_group:
            right = f"{operand[1]:g}" if operand[0] == "num" else str(operand[1])
            atoms.append(f"{key} {op_label.get(op, op)} {right}")
        parts.append(" 且 ".join(atoms))
    return " 或 ".join(parts)


def validate(text: str) -> str | None:
    """返回错误消息；没问题返回 None。"""
    try:
        compile_expression(text)
        return None
    except ValueError as exc:
        return str(exc)
