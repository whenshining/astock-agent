"""回测数据层：历史 K 线按需读取、逐日指标计算、条件判断。

三个关键点：

1. **逐日指标要跟实时筛选完全一致**。实时筛选是 SQL 查指标缓存，回测是
   Python 逐日算——两套实现很容易跑偏。这里刻意复刻 indicators.compute 的
   口径，并且有专门的测试比对「回测在最新交易日的选股结果 == 实时筛选结果」。

2. **滚动计算**。全市场 5000+ 只股票 × 数百个交易日，逐日重算 MA60 会是
   O(n²)，必须用滚动和 + 单调队列把每天压到 O(1)。

3. **热数据按需加载**。回测只需要信号股和被持有股票的 K 线，不必把全市场
   几百 MB 的日线都读进内存。
"""

from __future__ import annotations

import bisect
import struct
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from ..engine.tdx import DEFAULT_MAX_RECORDS, Bar, is_stock_code, read_day

# 与实时筛选保持一致：至少 61 根才能算 MA60
MIN_BARS = 61
# 回测窗口之前额外多读的天数，用于给 MA60 等指标预热（60 根就够，留点余量）
WARMUP_BARS = 70


# ------------------------------------------------------------------ 交易日历

def trading_dates(vipdoc: str | Path, start: int, end: int) -> list[int]:
    """取 [start, end] 区间内的交易日（YYYYMMDD 整数，升序）。

    优先用上证指数文件当日历——它每个交易日都有数据，是最干净的来源；
    读不到就退化成「抽样股票里出现次数过半的日期」。
    """
    root = Path(vipdoc)
    for name in ("sh000001.day", "sz399001.day", "sh000300.day"):
        path = root / ("sh" if name.startswith("sh") else "sz") / "lday" / name
        if path.is_file():
            dates = [b.date for b in read_day(path, max_records=4000)]
            picked = [d for d in dates if start <= d <= end]
            if picked:
                return picked

    # 兜底：统计抽样股票里每个日期出现的次数
    counter: dict[int, int] = {}
    samples = 0
    for market in ("sh", "sz"):
        for path in (root / market / "lday").glob("*.day"):
            if not is_stock_code(path.stem[2:], market):
                continue
            samples += 1
            if samples > 200:
                break
            for bar in read_day(path, max_records=400):
                if start <= bar.date <= end:
                    counter[bar.date] = counter.get(bar.date, 0) + 1
        if samples > 200:
            break
    if not counter:
        return []
    threshold = max(1, samples // 2)
    return sorted(d for d, n in counter.items() if n >= threshold)


# ------------------------------------------------------------------ K 线读取

def day_path(vipdoc: str | Path, code: str) -> Path | None:
    market = "sh" if code.startswith(("6", "9")) else "sz"
    path = Path(vipdoc) / market / "lday" / f"{market}{code}.day"
    return path if path.is_file() else None


def _parse_date(value: int):
    from datetime import date

    try:
        return date(value // 10000, value // 100 % 100, value % 100)
    except ValueError:
        return None


def _file_last_date(path: Path) -> int:
    """读 .day 文件最后一条记录的日期（只看末尾 32 字节，很快）。"""
    try:
        size = path.stat().st_size
        n = size // 32
        if n <= 0:
            return 0
        with open(path, "rb") as handle:
            handle.seek((n - 1) * 32)
            return struct.unpack("<I", handle.read(4))[0]
    except OSError:
        return 0


def _records_needed(path: Path, start_date: int, end_date: int) -> int:
    """估算要从文件末尾往回读多少根，才够覆盖 [start - 预热, end] 这一段。

    关键是先看文件最后日期：回测区间如果结束在很久以前，
    按「窗口长度」去读末尾会读到最新几年的数据，过滤后全空，
    回测会静默地跑出「零交易」。
    """
    last = _file_last_date(path)
    if last <= 0:
        return 10 ** 9

    def natural_days(a: int, b: int) -> int:
        da, db = _parse_date(a), _parse_date(b)
        if not da or not db:
            return 365
        return max(0, (db - da).days)

    # 文件末尾到窗口结束之间的那段：读了也要丢掉
    tail = int(natural_days(end_date, last) * 0.72) if last > end_date else 0
    window = int(natural_days(start_date, end_date) * 0.72)
    return min(10 ** 9, tail + window + WARMUP_BARS + 30)


class BarStore:
    """按代码读取并缓存回测窗口内的 K 线（含指标预热段，只读需要的部分）。"""

    def __init__(self, vipdoc: str | Path, start_date: int, end_date: int) -> None:
        self.vipdoc = Path(vipdoc)
        self.start_date = start_date
        self.end_date = end_date
        self._cache: dict[str, list[Bar]] = {}
        self._need_cache: dict[str, int] = {}

    def _need(self, code: str, path: Path) -> int:
        cached = self._need_cache.get(code)
        if cached is None:
            cached = _records_needed(path, self.start_date, self.end_date)
            self._need_cache[code] = cached
        return cached

    def bars(self, code: str) -> list[Bar]:
        """返回该股票从预热段到窗口结束的 K 线（含窗口之前的若干根）。"""
        cached = self._cache.get(code)
        if cached is not None:
            return cached

        path = day_path(self.vipdoc, code)
        rows: list[Bar] = []
        if path is not None:
            rows = read_day(path, max_records=self._need(code, path))
            rows = [b for b in rows if b.date <= self.end_date]
        self._cache[code] = rows
        return rows

    def window_bars(self, code: str) -> tuple[list[Bar], int]:
        """返回 (全部K线, 窗口起始在其中的下标)。"""
        rows = self.bars(code)
        index = bisect.bisect_left([b.date for b in rows], self.start_date)
        return rows, index


# ------------------------------------------------------------------ 逐日指标

def _sliding_max(values: list[float], window: int) -> list[float | None]:
    """滚动窗口最大值（单调队列，O(n)）。"""
    out: list[float | None] = [None] * len(values)
    dq: deque[int] = deque()
    for i, value in enumerate(values):
        while dq and values[dq[-1]] <= value:
            dq.pop()
        dq.append(i)
        while dq[0] <= i - window:
            dq.popleft()
        if i >= window - 1:
            out[i] = values[dq[0]]
    return out


def compute_series(bars: list[Bar]) -> list[dict[str, Any] | None]:
    """对每根 K 线算出「以该日收盘为准」的指标，口径与实时筛选一致。"""
    n = len(bars)
    if n == 0:
        return []

    closes = [b.close for b in bars]
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    vols = [b.volume for b in bars]
    amounts = [b.amount for b in bars]

    high60 = _sliding_max(closes, 60)
    high20 = _sliding_max(closes, 20)
    amp_series = [0.0] * n
    for i in range(1, n):
        prev = closes[i - 1]
        amp_series[i] = (highs[i] - lows[i]) / prev * 100 if prev > 0 else 0.0

    out: list[dict[str, Any] | None] = [None] * n

    sum5 = sum10 = sum20 = sum60 = 0.0
    sum_vol6 = 0.0
    sum_amp20 = 0.0
    sum_amt5 = 0.0
    consec_up = 0
    consec_down = 0

    for i in range(n):
        close = closes[i]
        vol = vols[i]
        amt = amounts[i]

        # 滚动和：先加后减，保证窗口是 [i-k+1, i]
        sum5 += close
        sum10 += close
        sum20 += close
        sum60 += close
        sum_vol6 += vol
        sum_amp20 += amp_series[i]
        sum_amt5 += amt
        if i >= 5:
            sum5 -= closes[i - 5]
        if i >= 10:
            sum10 -= closes[i - 10]
        if i >= 20:
            sum20 -= closes[i - 20]
        if i >= 60:
            sum60 -= closes[i - 60]
        if i >= 6:
            sum_vol6 -= vols[i - 6]
        if i >= 20:
            sum_amp20 -= amp_series[i - 20]
        if i >= 5:
            sum_amt5 -= amounts[i - 5]

        if i > 0:
            if close > closes[i - 1]:
                consec_up += 1
                consec_down = 0
            elif close < closes[i - 1]:
                consec_down += 1
                consec_up = 0
            else:
                consec_up = 0
                consec_down = 0

        if i < MIN_BARS - 1:
            continue

        prev = closes[i - 1]
        if prev <= 0 or close <= 0:
            continue

        ma5 = sum5 / 5
        ma10 = sum10 / 10
        ma20 = sum20 / 20
        ma60 = sum60 / 60

        # 量比 = 今日量 / 前 5 日均量（前 5 日指不含今日的 [i-5, i-1]）
        base_vol = (sum_vol6 - vol) / 5 if i >= 5 else 0.0
        vol_ratio = (vol / base_vol) if base_vol > 0 else None

        amp20 = sum_amp20 / 20
        # 近 5 日平均成交额（含今日，与实时筛选口径一致）
        amount_avg5 = sum_amt5 / 5

        window60 = high60[i] or close
        out[i] = {
            "date": bars[i].date,
            "open": bars[i].open,
            "high": highs[i],
            "low": lows[i],
            "close": round(close, 2),
            "chg": round((close / prev - 1) * 100, 2),
            "chg5": round((close / closes[i - 5] - 1) * 100, 2) if i >= 5 else 0.0,
            "chg20": round((close / closes[i - 20] - 1) * 100, 2) if i >= 20 else 0.0,
            "amplitude": round(amp_series[i], 2),
            "amp20": round(amp20, 2),
            "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
            "volume": vol,
            "amount": amt,
            "amount_avg5": amount_avg5,
            "ma5": round(ma5, 2),
            "ma10": round(ma10, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            # 必须按取整后的均线比较，和 indicators.compute 保持一致：
            # 均线只差 1e-16 时浮点误差会翻转 > 的判断，导致实时筛选和回测不一致。
            "ma_bull": int(round(ma5, 2) > round(ma10, 2) > round(ma20, 2) and close > round(ma5, 2)),
            "consec_up": consec_up,
            "consec_down": consec_down,
            "new_high60": int(close >= window60),
            "new_high20": int(close >= (high20[i] or close)),
            "drawdown60": round((close / window60 - 1) * 100, 2) if window60 else 0.0,
        }
    return out


# ------------------------------------------------------------------ 条件判断

def passes(params: dict[str, Any], metrics: dict[str, Any], code: str) -> bool:
    """判断某一天的指标是否满足选股条件。

    口径必须与 screener.py 的 SQL 过滤一致，否则回测和实盘会对不上。
    """

    def num(key: str) -> float | None:
        value = params.get(key)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def ge(key: str, field: str) -> bool:
        limit = num(key)
        if limit is None:
            return True
        value = metrics.get(field)
        return value is not None and value >= limit

    def le(key: str, field: str) -> bool:
        limit = num(key)
        if limit is None:
            return True
        value = metrics.get(field)
        return value is not None and value <= limit

    if not ge("min_price", "close"):
        return False
    if not le("max_price", "close"):
        return False
    if not ge("chg_min", "chg"):
        return False
    if not le("chg_max", "chg"):
        return False
    if not ge("chg5_min", "chg5"):
        return False
    if not ge("chg20_min", "chg20"):
        return False
    if not ge("vol_ratio_min", "vol_ratio"):
        return False
    if not le("vol_ratio_max", "vol_ratio"):
        return False
    if not ge("amplitude_min", "amplitude"):
        return False
    if not le("amplitude_max", "amplitude"):
        return False
    if not le("amp20_max", "amp20"):
        return False
    if not ge("consec_up_min", "consec_up"):
        return False
    if not ge("amount_min", "amount"):
        return False
    if not le("amount_max", "amount"):
        return False
    if params.get("new_high60") and not metrics.get("new_high60"):
        return False
    if params.get("new_high20") and not metrics.get("new_high20"):
        return False
    if params.get("ma_bull") and not metrics.get("ma_bull"):
        return False
    limit = num("min_drawdown60")
    if limit is not None:
        value = metrics.get("drawdown60")
        if value is None or value < limit:
            return False

    boards = params.get("boards")
    if boards:
        from ..engine.screener import BOARDS

        prefixes = tuple(p for b in boards for p in BOARDS.get(b, ()))
        if prefixes and not code.startswith(prefixes):
            return False

    codes = params.get("codes")
    if codes is not None and code not in set(codes):
        return False

    return True


def filter_codes(vipdoc: str | Path) -> Iterable[tuple[str, str]]:
    """遍历全市场的 (代码, 市场)。"""
    from ..engine.tdx import iter_stock_files

    for _path, code, market in iter_stock_files(vipdoc):
        yield code, market
