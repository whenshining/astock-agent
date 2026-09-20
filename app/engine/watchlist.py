"""读取通达信的自选股。

只关心**「自选股」**（通达信内置的那个，文件是 `T0002\\blocknew\\ZXG.blk`）。
用户的其他自定义板块（主选/次选/强势股票…）一律忽略——
实测那些多半是随模板带进来的残留，常年不更新，展示出来只是噪音。

.blk 格式（GBK 文本）：
    每行 7 个字符，第 1 位是市场标记（1=沪市，0=深市），后 6 位是证券代码。
    例如 `1600519` = 沪市 600519，`0000858` = 深市 000858。
    空行是分组分隔（通达信里一个自选股可以分几组），解析时忽略。
    里面也混有指数（如 1999999 上证指数），按个股规则过滤掉。

只读，不修改任何通达信文件。
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from .tdx import is_stock_code

BLOCK_DIR_PARTS = ("T0002", "blocknew")
WATCHLIST_FILE = "ZXG.blk"
WATCHLIST_LABEL = "自选股"

_ENTRY_RE = re.compile(r"^([01])(\d{6})$")


def block_dir(vipdoc: str | Path) -> Path | None:
    """由 vipdoc 推断 blocknew 目录。"""
    root = Path(vipdoc)
    for base in (root.parent, root):
        candidate = base.joinpath(*BLOCK_DIR_PARTS)
        if candidate.is_dir():
            return candidate
    return None


def watchlist_path(vipdoc: str | Path) -> Path | None:
    """自选股文件路径，找不到返回 None。"""
    directory = block_dir(vipdoc)
    if directory is None:
        return None
    path = directory / WATCHLIST_FILE
    return path if path.is_file() else None


def parse_blk(path: str | Path) -> list[str]:
    """解析 .blk，返回其中的 A 股代码（去重、保持原顺序）。"""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return []

    codes: list[str] = []
    seen: set[str] = set()
    for line in raw.decode("gbk", "ignore").splitlines():
        entry = line.strip()
        if not entry:
            continue                      # 空行是分组分隔
        match = _ENTRY_RE.match(entry)
        if match:
            market = "sh" if match.group(1) == "1" else "sz"
            code = match.group(2)
        elif len(entry) == 6 and entry.isdigit():
            # 少数版本直接写 6 位代码、没有市场标记
            code = entry
            market = "sh" if code.startswith(("6", "9")) else "sz"
        else:
            continue
        if is_stock_code(code, market) and code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def load(vipdoc: str | Path) -> dict[str, Any]:
    """读取自选股。

    返回 {"available", "codes", "count", "days_ago", "updated_at"}。
    days_ago 是文件最后一次更新的天数，用来提示用户数据新鲜度。
    """
    path = watchlist_path(vipdoc)
    if path is None:
        return {
            "available": False, "codes": [], "count": 0,
            "days_ago": None, "updated_at": None,
            "error": "没找到通达信的自选股文件（T0002\\blocknew\\ZXG.blk）。"
                     "请确认通达信已安装，且你至少添加过一只自选股。",
        }

    codes = parse_blk(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    days_ago = int((time.time() - mtime) / 86400) if mtime else None

    if not codes:
        return {
            "available": False, "codes": [], "count": 0,
            "days_ago": days_ago, "updated_at": int(mtime) if mtime else None,
            "error": "自选股文件是空的，里面还没有个股（指数不算）。",
        }

    return {
        "available": True,
        "codes": codes,
        "count": len(codes),
        "days_ago": days_ago,
        "updated_at": int(mtime) if mtime else None,
    }


def resolve_codes(vipdoc: str | Path) -> list[str]:
    """取自选股代码列表。"""
    return list(load(vipdoc)["codes"])


def apply_watchlist_filter(params: dict[str, Any], vipdoc: str | Path) -> tuple[dict[str, Any], int]:
    """如果 params 里要求「只看自选股」，就把代码集合塞进去。

    每次执行时都重新读文件，所以用户在通达信里调整了自选股，策略会自动跟上。
    返回 (新的 params, 自选股只数)。
    """
    if not params.get("watchlist"):
        return params, 0
    info = load(vipdoc)
    if not info["available"]:
        updated = dict(params)
        updated["codes"] = []            # 空集合 → 命不中任何股票，而不是跑成全市场
        return updated, 0
    updated = dict(params)
    updated["codes"] = info["codes"]
    return updated, info["count"]


def summary(vipdoc: str | Path) -> dict[str, Any]:
    """概览，供工具和系统提示词使用。"""
    info = load(vipdoc)
    return {
        "available": info["available"],
        "count": info["count"],
        "days_ago": info.get("days_ago"),
        "error": info.get("error"),
    }
