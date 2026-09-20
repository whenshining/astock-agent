"""价格复权：把通达信的不复权日线还原成可用的连续价格。

## 为什么必须做

通达信 .day 存的是**不复权**价格。股票除权除息（送转、分红）后价格会跳变：

    10送10 除权前收盘 100 元，除权后开盘 50 元 → 原始数据里是一根 -50% 的巨阴线

后果是**双重的**：
  · 回测把这根 -50% 当成真实暴跌 → 立刻触发止损 → 记成一笔假巨亏
  · 均线、涨跌幅、创新高这些指标在除权日附近全是错的 → **实时选股也跟着错**

实测：全市场 5251 只里有 **410 只（7.8%）** 存在这类异常跳变，近 300 个交易日共 439 次。

## 怎么还原

通达信的 `gbbq`（股本变迁）文件是**压缩格式**，解析不了（日期字段读出来是垃圾值），
所以这里用**检测**的方式——A 股有涨跌停限制，这就是天然的判据：

    任何超过涨跌停幅度的单日跳变，必然是除权除息，不可能是真实成交

检测到跳变后按**前复权**处理：最新价保持不变，除权日之前的价格按比例缩放，
使序列连续。

## 已知的误判来源，逐个处理

| 情况 | 会不会误判 | 处理 |
|---|---|---|
| 新股上市初期无涨跌幅限制 | 会 | 前 5 根 K 线不参与检测 |
| 长期停牌复牌当天不设涨跌幅 | 会 | 与上一根间隔 > 20 天的跳变不参与检测 |
| ST 股涨跌停是 5% | 不会 | 阈值取「涨跌停 + 2%」，5% 的波动够不着 |
| 小额分红（不足 2%） | 检测不到 | 误差很小，可接受 |
| 数据本身有错 | 会被当成除权 | 概率极低，且缩放方向一致，影响有限 |
"""

from __future__ import annotations

from datetime import date as _date
from typing import Iterable

from .tdx import Bar

# 涨跌停幅度：创业板/科创板 20%，主板 10%
LIMIT_CREATIVE = 0.20
LIMIT_MAIN = 0.10
# 阈值在涨跌停基础上留的余量：避开 ST 的 5% 限制，也避开数据四舍五入
THRESHOLD_MARGIN = 0.01
# A 股最小报价单位（元）
TICK = 0.01
# 新股上市初期（无涨跌幅限制）不动前几根
SKIP_HEAD_BARS = 5
# 与上一根间隔超过这么多自然日，视为长期停牌复牌，跳变属真实波动
MAX_GAP_DAYS = 20


def _limit_for(code: str) -> float:
    return LIMIT_CREATIVE if code.startswith(("30", "68")) else LIMIT_MAIN


def _threshold(code: str, price: float) -> float:
    """当日允许的最大涨跌幅（含报价单位造成的额外空间）。

    **低价股必须单独处理**：A 股最小变动 0.01 元，涨停价四舍五入到分之后，
    实际涨幅会比名义涨跌停大。例如 0.17 元的涨停价是 0.19 元（+11.8%），
    不是 0.187——如果不把这段空间算进去，会把合法的涨停误判成除权。
    """
    limit = _limit_for(code)
    tick_slack = (TICK / 2) / price if price > 0 else 0.0
    return limit + tick_slack + THRESHOLD_MARGIN


def _parse(value: int) -> _date | None:
    try:
        return _date(value // 10000, value // 100 % 100, value % 100)
    except (ValueError, TypeError):
        return None


def _gap_days(prev_date: int, cur_date: int) -> int:
    a, b = _parse(prev_date), _parse(cur_date)
    if not a or not b:
        return 0
    return abs((b - a).days)


def detect_events(bars: list[Bar], code: str, from_start: bool = False) -> list[tuple[int, float]]:
    """找出除权除息日，返回 [(下标, 复权比例)]。

    比例 = 当日收盘 / 前一日收盘。前复权时把该下标**之前**的价格乘以这个比例。

    from_start：这批 K 线是否**从该股上市首日开始**。只有从上市首日读起，
    「前几根」才是真正的上市初期（无涨跌幅限制）；如果是从文件尾部读的一段，
    开头那几根只是普通交易日，必须正常参与检测。

    ⚠️ 这个参数不能省：实时选股读 90 根、回测读 400 根，同一根 K 线在两个窗口里
    位置不同。如果无条件跳过「前 5 根」，两边算出的复权结果就会不一致
    ——（这条是被口径一致性校验抓出来的）。
    """
    if len(bars) <= 1:
        return []

    events: list[tuple[int, float]] = []

    for i in range(1, len(bars)):
        if from_start and i < SKIP_HEAD_BARS:
            continue                          # 新股上市初期无涨跌幅限制
        prev, cur = bars[i - 1], bars[i]
        if prev.close <= 0 or cur.close <= 0:
            continue
        ratio = cur.close / prev.close
        # 先做便宜的比值判断，只有疑似除权才去解析日期——
        # 日期解析每根 K 线都做的话，全市场扫描会明显变慢。
        if abs(ratio - 1) <= _threshold(code, prev.close):
            continue
        if _gap_days(prev.date, cur.date) > MAX_GAP_DAYS:
            continue                          # 长期停牌复牌，属真实波动
        events.append((i, ratio))
    return events


def adjust(bars: list[Bar], code: str, from_start: bool = False) -> tuple[list[Bar], int]:
    """做前复权，返回 (复权后的 K 线, 检测到的除权次数)。

    前复权 = 最新价保持不变，历史价按比例缩放，让序列连续。
    从最近的除权日往前处理，比例自然累积。
    """
    events = detect_events(bars, code, from_start)
    if not events:
        return bars, 0

    out = list(bars)
    for index, ratio in reversed(events):
        for j in range(index):
            bar = out[j]
            out[j] = Bar(
                bar.date,
                bar.open * ratio, bar.high * ratio,
                bar.low * ratio, bar.close * ratio,
                bar.amount, bar.volume,      # 成交额/量不受复权影响
            )
    return out, len(events)


def summarize(bars_list: Iterable[tuple[str, list[Bar]]]) -> dict:
    """批量统计有多少只股票被复权过（用于自检和日志）。"""
    stocks = 0
    events = 0
    for code, bars in bars_list:
        _adjusted, count = adjust(bars, code)
        if count:
            stocks += 1
            events += count
    return {"stocks": stocks, "events": events}
