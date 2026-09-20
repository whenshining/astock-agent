"""通达信 vipdoc 日线文件读取（纯本地、无需网络）。

.day 文件格式：每条约 32 字节，小端
    date   u32   YYYYMMDD
    open   u32   价格 * 100
    high   u32   价格 * 100
    low    u32   价格 * 100
    close  u32   价格 * 100
    amount f32   成交额（元）
    volume u32   成交量（手）
    reserved u32
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator, NamedTuple

RECORD_SIZE = 32
_RECORD = struct.Struct("<IIIIIfII")

# 默认读取的 K 线根数：60 日均线/60 日新高需要 60 根，留一点余量即可。
# 读得越少首扫越快（磁盘冷读时尤其明显）。
DEFAULT_MAX_RECORDS = 90


class Bar(NamedTuple):
    date: int
    open: float
    high: float
    low: float
    close: float
    amount: float
    volume: float


def read_day(path: str | Path, max_records: int = 160, adjust: bool = True) -> list[Bar]:
    """读取 .day 文件最后 max_records 根 K 线（越靠后越新）。

    只从文件尾部读取需要的部分，避免把整份历史读进内存。

    **默认做前复权**（见 adjust.py）：通达信存的是不复权价格，除权日会出现
    超过涨跌停的假跳变（实测 7.8% 的股票有），会让回测把除权当成真实暴跌、
    也会让均线/涨跌幅/创新高等指标在除权日附近算错。
    这一步必须在数据读取层做——实时选股和回测都走这里，口径才不会分叉。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            total = size // RECORD_SIZE
            if total <= 0:
                return []
            start = max(0, total - max_records)
            f.seek(start * RECORD_SIZE)
            blob = f.read((total - start) * RECORD_SIZE)
    except OSError:
        return []

    bars: list[Bar] = []
    append = bars.append
    for date, o, h, low, c, amount, volume, _res in _RECORD.iter_unpack(blob):
        append(Bar(date, o / 100.0, h / 100.0, low / 100.0, c / 100.0, amount, float(volume)))

    if adjust and len(bars) > 1:
        # 延迟导入：adjust 依赖 tdx.Bar，模块级互引会成环
        from .adjust import adjust as _adjust

        stem = Path(path).stem            # 例如 sh600519
        code = stem[2:] if len(stem) > 2 else stem
        # start == 0 表示这次是从文件开头读的，也就是真的从上市首日开始，
        # 「前几根」才属于上市初期（无涨跌幅限制）。从尾部读的一段不能跳过开头。
        bars, _events = _adjust(bars, code, from_start=(start == 0))
    return bars


def is_stock_code(code: str, market: str) -> bool:
    """只保留普通 A 股（沪市主板/科创板、深市主板/创业板），剔除指数、基金、债券等。"""
    if market == "sh":
        return code.startswith(("600", "601", "603", "605", "688", "689"))
    if market == "sz":
        return code.startswith(("000", "001", "002", "003", "300", "301"))
    return False


def iter_stock_files(vipdoc: str | Path, markets: tuple[str, ...] = ("sh", "sz")) -> Iterator[tuple[Path, str, str]]:
    """遍历 vipdoc 下的个股日线文件，产出 (path, code, market)。"""
    root = Path(vipdoc)
    for market in markets:
        lday = root / market / "lday"
        if not lday.is_dir():
            continue
        for entry in lday.iterdir():
            name = entry.name
            if not name.endswith(".day"):
                continue
            stem = name[:-4]
            if len(stem) < 8:
                continue
            code = stem[2:]
            if not is_stock_code(code, market):
                continue
            yield entry, code, market


def has_vipdoc(vipdoc: str | Path) -> bool:
    root = Path(vipdoc)
    return (root / "sh" / "lday").is_dir() or (root / "sz" / "lday").is_dir()
