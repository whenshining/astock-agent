"""由日线序列计算选股用指标。

全部基于本地日线 OHLCV：不含市盈率/市净率/市值/换手率（那些需要财务或流通股本数据）。
"""

from __future__ import annotations

from typing import Any

from .tdx import Bar

# 需要的最少 K 线数（够算 ma60 + 一点余量）
MIN_BARS = 61


def _ma(closes: list[float], n: int) -> float | None:
    if len(closes) < n:
        # 数据不足时用现有全部（与通达信新上市股票表现接近），少于 n 一半则不计算
        if len(closes) < max(2, n // 2):
            return None
        n = len(closes)
    return sum(closes[-n:]) / n


def compute(bars: list[Bar]) -> dict[str, Any] | None:
    """计算单只股票的指标。bars 最后一条为最新交易日。"""
    if len(bars) < MIN_BARS:
        return None

    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    highs = [b.high for b in bars]
    last = bars[-1]
    close = last.close
    prev = closes[-2]
    if prev <= 0 or close <= 0:
        return None

    chg = (close / prev - 1) * 100
    amplitude = (last.high - last.low) / prev * 100

    # 量比：今日量 / 前 5 日均量
    base = vols[-6:-1]
    avg_base = sum(base) / len(base) if base else 0.0
    vol_ratio = (last.volume / avg_base) if avg_base > 0 else None

    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    # 比较前先按报价精度（0.01 元）取整。
    # 均线数学上相等、但只差 1e-16 时，「重新求和」和「滚动求和」的浮点误差
    # 足以翻转 > 的判断，导致实时筛选和回测对同一只股票给出不同的 ma_bull
    # ——（这条是被口径一致性校验抓出来的）。价格精度本来就是 0.01，
    # 按 2 位小数比较既稳定，也和界面上显示的数字一致。
    r5 = round(ma5, 2) if ma5 is not None else None
    r10 = round(ma10, 2) if ma10 is not None else None
    r20 = round(ma20, 2) if ma20 is not None else None
    ma_bull = bool(r5 is not None and r10 is not None and r20 is not None
                   and r5 > r10 > r20 and close > r5)

    # 连涨天数（自最新往前数）
    consec_up = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            consec_up += 1
        else:
            break

    window60 = closes[-60:]
    window20 = closes[-20:]
    high60 = max(window60)
    high20 = max(window20)
    new_high60 = close >= high60
    new_high20 = close >= high20
    drawdown60 = (close / high60 - 1) * 100 if high60 else 0.0

    chg5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    chg20 = (close / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0

    # 波动率（近 20 日振幅均值）与均量（近 5 日成交额均值，单位万元）
    amp20 = 0.0
    for i in range(len(closes) - 20, len(closes)):
        if closes[i - 1] > 0:
            amp20 += (highs[i] - bars[i].low) / closes[i - 1] * 100
    amp20 /= 20

    amounts = [b.amount for b in bars[-5:]]
    amount_avg5 = sum(amounts) / len(amounts) if amounts else 0.0

    return {
        "date": last.date,
        "close": round(close, 2),
        "chg": round(chg, 2),
        "chg5": round(chg5, 2),
        "chg20": round(chg20, 2),
        "amplitude": round(amplitude, 2),
        "amp20": round(amp20, 2),
        "vol_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "volume": last.volume,
        "amount": round(last.amount, 2),
        "amount_avg5": round(amount_avg5, 2),
        "ma5": round(ma5, 2) if ma5 else None,
        "ma10": round(ma10, 2) if ma10 else None,
        "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "ma_bull": int(ma_bull),
        "consec_up": consec_up,
        "consec_down": _consec_down(closes),
        "new_high60": int(new_high60),
        "new_high20": int(new_high20),
        "drawdown60": round(drawdown60, 2),
    }


def _consec_down(closes: list[float]) -> int:
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            n += 1
        else:
            break
    return n


# 指标列（与数据库表结构保持一致，顺序即插入顺序）
COLUMNS = [
    "code", "market", "date", "close", "chg", "chg5", "chg20",
    "amplitude", "amp20", "vol_ratio", "volume", "amount", "amount_avg5",
    "ma5", "ma10", "ma20", "ma60", "ma_bull",
    "consec_up", "consec_down", "new_high60", "new_high20", "drawdown60",
]
