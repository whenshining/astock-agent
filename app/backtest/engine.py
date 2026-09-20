"""回测引擎：用历史数据模拟一个策略的实际交易过程。

设计原则：**宁可把结果算难看，也不能算好看**。回测里最容易自欺欺人的
几个地方，这里全部按最保守的方式处理：

  1. 信号在 T 日收盘产生 → **T+1 开盘买入**，绝不用当日收盘价成交（那等于偷看未来）
  2. **一字涨停买不进** → 跳过这笔信号（A股最常见的回测虚高来源）
  3. **一字跌停卖不出** → 顺延到下一交易日
  4. **T+1 制度** → 当天买入当天不能卖
  5. 停牌（当天没有K线）→ 不交易
  6. 手续费：买入佣金 0.025%（最低 5 元）；卖出佣金 + 印花税 0.05%
  7. 资金约束：现金不足就买不了，不做杠杆

止损/止盈同一天都触发时，按**先止损**处理（更保守）。
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable

from . import data as bt_data
from . import expr as expr_mod
from ..engine.tdx import Bar, read_day

# 交易成本（可按需调整）
COMMISSION_RATE = 0.00025      # 佣金 0.025%（双边）
COMMISSION_MIN = 5.0           # 单笔最低佣金
STAMP_TAX_RATE = 0.0005        # 印花税 0.05%（仅卖出）
MIN_LOT = 100                  # 一手 100 股
TRADING_DAYS_PER_YEAR = 244

# 卖出原因
REASON_HOLD = "持有到期"
REASON_STOP = "止损"
REASON_TAKE = "止盈"
REASON_COND = "条件卖出"
REASON_END = "回测结束平仓"


@dataclass
class TradePlan:
    """交易计划：决定什么时候卖、买多少。"""
    hold_days: int = 5
    stop_loss: float | None = -8.0          # 百分比，负数
    take_profit: float | None = None        # 百分比，正数
    max_positions: int = 5
    position_pct: float = 20.0              # 单只占初始资金的比例
    exit_conditions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "TradePlan":
        raw = raw or {}

        def _num(key: str, default):
            value = raw.get(key, default)
            if value is None or value == "":
                return None if default is None else default
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        hold = raw.get("hold_days", 5)
        try:
            hold = max(1, min(120, int(hold)))
        except (TypeError, ValueError):
            hold = 5
        try:
            max_pos = max(1, min(50, int(raw.get("max_positions", 5))))
        except (TypeError, ValueError):
            max_pos = 5

        conditions = raw.get("exit_conditions") or []
        if not isinstance(conditions, list):
            conditions = []

        return cls(
            hold_days=hold,
            stop_loss=_num("stop_loss", -8.0),
            take_profit=_num("take_profit", None),
            max_positions=max_pos,
            position_pct=max(1.0, min(100.0, _num("position_pct", 20.0) or 20.0)),
            exit_conditions=[c for c in conditions if isinstance(c, dict)],
        )


@dataclass
class _Position:
    code: str
    shares: int
    buy_date: int
    buy_price: float
    buy_index: int
    buy_day: int                    # 买入日在交易日历中的下标
    cost: float                     # 含手续费的买入总成本


def _limit_ratio(code: str) -> float:
    """涨跌停幅度：创业板/科创板 20%，其他 10%。"""
    return 0.20 if code.startswith(("30", "68")) else 0.10


def _buy_fee(amount: float) -> float:
    return max(COMMISSION_MIN, amount * COMMISSION_RATE)


def _sell_fee(amount: float) -> float:
    return max(COMMISSION_MIN, amount * COMMISSION_RATE) + amount * STAMP_TAX_RATE


def _is_limit_up_open(bar: Bar, prev_close: float, code: str) -> bool:
    """开盘就一字涨停 → 买不进。"""
    if prev_close <= 0:
        return False
    limit = prev_close * (1 + _limit_ratio(code))
    return bar.open >= limit * 0.999 and bar.high <= bar.open * 1.0001


def _is_limit_down(bar: Bar, prev_close: float, code: str) -> bool:
    """一字跌停 → 卖不出。"""
    if prev_close <= 0:
        return False
    limit = prev_close * (1 - _limit_ratio(code))
    return bar.low <= limit * 1.001 and bar.high <= bar.low * 1.0001


def evaluate_exit_conditions(
    conditions: Iterable[dict[str, Any]],
    metrics: dict[str, Any],
    prev_metrics: dict[str, Any] | None,
    buy_price: float,
) -> str | None:
    """检查条件卖出，返回触发的那条规则的说明，没触发返回 None。"""
    for rule in conditions:
        kind = str(rule.get("type") or "").strip()
        try:
            value = float(rule.get("value")) if rule.get("value") not in (None, "") else None
        except (TypeError, ValueError):
            value = None
        ma_key = str(rule.get("ma") or "ma10").strip()

        if kind == "ma_break":
            # 收盘跌破指定均线
            ma_value = metrics.get(ma_key)
            if ma_value and metrics.get("close", 0) < ma_value:
                label = {"ma5": "MA5", "ma10": "MA10", "ma20": "MA20", "ma60": "MA60"}.get(ma_key, ma_key)
                return f"跌破{label}"

        elif kind == "chg_below":
            if value is not None and metrics.get("chg") is not None and metrics["chg"] <= value:
                return f"单日跌幅超过 {abs(value):.1f}%"

        elif kind == "vol_ratio_below":
            ratio = metrics.get("vol_ratio")
            if value is not None and ratio is not None and ratio < value:
                return f"量比低于 {value}"

        elif kind == "consec_down":
            if value is not None and (metrics.get("consec_down") or 0) >= value:
                return f"连跌 {int(value)} 天"

        elif kind == "ma_dead":
            # MA5 下穿 MA10（死叉）
            if prev_metrics and metrics.get("ma5") and prev_metrics.get("ma5"):
                if (prev_metrics["ma5"] >= prev_metrics.get("ma10", 0)
                        and metrics["ma5"] < metrics.get("ma10", 0)):
                    return "MA5 下穿 MA10"

        elif kind == "profit_below":
            # 浮盈回撤到某个水平（保护利润）
            if value is not None and buy_price > 0:
                gain = (metrics.get("close", 0) / buy_price - 1) * 100
                if gain <= value:
                    return f"浮盈回落至 {value:.1f}%"

        elif kind == "expr":
            # 用户自己写的表达式，例如「收盘 < MA10 且 量比 < 0.8」
            text = str(rule.get("expr") or "").strip()
            if text:
                try:
                    compiled = _compiled_expr(text)
                except ValueError:
                    continue          # 无效表达式跳过（保存时已经拦过一道）
                if expr_mod.evaluate(compiled, metrics, buy_price):
                    return text

    return None


@lru_cache(maxsize=256)
def _compiled_expr(text: str):
    """表达式编译结果缓存——逐日逐仓都要判断，每次重新解析会明显拖慢回测。"""
    return expr_mod.compile_expression(text)


# ------------------------------------------------------------------ 信号采集
#
# 回测 99% 的时间花在「扫全市场历史数据」这一步：5000+ 只股票 × 数百个交易日，
# 每只都要读文件 + 逐日算指标。20 核的机器只跑单线程是最大浪费，所以这里并行。

def signal_workers() -> int:
    """并行度。开发环境用满核；打包成 exe 后保守一些（onefile 下子进程开销更大）。"""
    if getattr(sys, "frozen", False):
        try:
            return max(1, min(8, int(os.environ.get("ASTOCK_WORKERS", "4"))))
        except ValueError:
            return 4
    return min(8, max(1, (os.cpu_count() or 4) - 1))


def _signal_worker(task: tuple) -> tuple[str, list[tuple[int, float, float | None]]]:
    """子进程任务：算出单只股票在回测窗口内的全部买入信号。

    必须是模块级函数——ProcessPoolExecutor 需要能 pickle 它。
    只回传 (日期, 收盘价, 排序值) 这样的紧凑元组，避免进程间搬大量数据。
    """
    vipdoc, code, start_date, end_date, params, sort_field = task
    path = bt_data.day_path(vipdoc, code)
    if path is None:
        return code, []

    bars = read_day(path, max_records=bt_data._records_needed(path, start_date, end_date))
    bars = [b for b in bars if b.date <= end_date]
    if len(bars) < bt_data.MIN_BARS:
        return code, []

    found: list[tuple[int, float, float | None]] = []
    for metrics in bt_data.compute_series(bars):
        if not metrics:
            continue
        date = metrics["date"]
        if date < start_date or date > end_date:
            continue
        if bt_data.passes(params, metrics, code):
            value = metrics.get(sort_field) if sort_field else None
            found.append((date, metrics["close"],
                          float(value) if isinstance(value, (int, float)) else None))
    return code, found


def _serial_signals(tasks: list[tuple], progress) -> list[tuple[str, list]]:
    out = []
    for done, task in enumerate(tasks, start=1):
        if progress and done % 300 == 0:
            progress(done, 0)
        out.append(_signal_worker(task))
    return out


# 并行采集的状态。之前这里 except 之后静默退回单进程，
# 结果「并行没生效」被藏了起来，白白多花 15 倍时间还找不出原因——
# 所以现在把失败原因记下来，并在结果里带出去。
PARALLEL_STATUS: dict[str, Any] = {"workers": 1, "parallel": False, "fallback_reason": ""}


def _parallel_signals(tasks: list[tuple], workers: int, progress) -> list[tuple[str, list]]:
    from concurrent.futures import ProcessPoolExecutor

    total = len(tasks)
    out: list[tuple[str, list]] = []
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(_signal_worker, tasks, chunksize=24):
            out.append(item)
            done += 1
            if progress and done % 300 == 0:
                progress(done, total)
    return out


def collect_signals(
    vipdoc: str,
    params: dict[str, Any],
    start_date: int,
    end_date: int,
    progress: Callable[[int, int], None] | None = None,
    bar_store: bt_data.BarStore | None = None,
    workers: int | None = None,
) -> tuple[dict[int, list[dict[str, Any]]], list[int], int]:
    """扫描全市场，把「哪天有哪些股票满足买入条件」收集起来。

    返回 (按日期归集的信号, 交易日历, 扫描股票数)。
    信号只跟买入条件有关、跟交易计划无关，所以参数扫描/样本外验证只需跑一次。
    """
    calendar = bt_data.trading_dates(vipdoc, start_date, end_date)
    if not calendar:
        return {}, [], 0

    # 用和「实时筛选」同一套字段映射，保证两边排序口径一致
    from ..engine.screener import normalize_sort

    sort_field = normalize_sort(params.get("sort_by"))
    ascending = str(params.get("sort_order", "")).lower() in ("asc", "升序")

    # 指定了股票池（单只股票回测 / 只看自选股）时只扫这些，
    # 没必要为了 1 只股票遍历全市场 5000+ 个文件。
    wanted = params.get("codes")
    if wanted is not None:
        codes = [c for c in dict.fromkeys(wanted) if bt_data.day_path(vipdoc, c) is not None]
    else:
        codes = [code for code, _market in bt_data.filter_codes(vipdoc)]
    total = len(codes)
    if not total:
        return {}, calendar, 0

    tasks = [(str(vipdoc), code, start_date, end_date, params, sort_field) for code in codes]
    if workers is None:
        workers = signal_workers()

    results: list[tuple[str, list]] | None = None
    PARALLEL_STATUS.update({"workers": workers, "parallel": False, "fallback_reason": ""})
    if workers > 1 and total > 200:
        try:
            results = _parallel_signals(tasks, workers, progress)
            PARALLEL_STATUS["parallel"] = True
        except Exception as exc:
            # 受限环境（沙箱禁止命名管道、杀软拦子进程、onefile 下 spawn 失败）退回单进程。
            # 一定要记下原因——静默退回会让「慢 15 倍」变成一个查不出来的谜。
            PARALLEL_STATUS["fallback_reason"] = f"{type(exc).__name__}: {exc}"
            print(f"[backtest] 多进程不可用，退回单进程：{exc}")
            results = None
    if results is None:
        results = _serial_signals(tasks, progress)

    signals: dict[int, list[dict[str, Any]]] = {}
    for code, found in results:
        for date, close, sort_value in found:
            signals.setdefault(date, []).append(
                {"code": code, "date": date, "close": close, "sort_value": sort_value})

    # 单只股票回测常用模式：不管买入条件，第一个交易日就买，之后只看卖出规则。
    # 同一天的信号数往往远多于可用仓位（平均 30+ 个信号抢 5 个仓位），
    # 这时「先挑谁」直接决定回测结果。必须按策略里设的排序字段来，
    # 否则挑出来的是目录顺序靠前的股票，跟「实时选股」看到的根本不是一批。
    # 取不到排序值的排在最后（升序降序都是）。
    sentinel = float("inf") if ascending else float("-inf")
    for items in signals.values():
        items.sort(
            key=lambda s: (sentinel if s.get("sort_value") is None else float(s["sort_value"])),
            reverse=not ascending,
        )

    if progress:
        progress(total, total)
    return signals, calendar, total


# ------------------------------------------------------------------ 组合模拟

def simulate(
    signals: dict[int, list[dict[str, Any]]],
    calendar: list[int],
    bar_store: bt_data.BarStore,
    plan: TradePlan,
    capital: float = 100000.0,
    start_date: int | None = None,
    end_date: int | None = None,
) -> dict[str, Any]:
    """按时间顺序模拟组合交易。

    start_date / end_date 用于只跑区间的一部分——样本外验证要把同一批信号
    切成「样本内段」和「样本外段」分别模拟。
    """
    if start_date or end_date:
        calendar = [
            d for d in calendar
            if (start_date is None or d >= start_date) and (end_date is None or d <= end_date)
        ]
    if not calendar:
        return _empty_result(capital)

    # 预载 K 线与逐日指标（只针对有信号的股票）
    cache: dict[str, tuple[list[Bar], list[dict[str, Any] | None], dict[int, int]]] = {}
    # 没有「条件卖出」时根本不需要逐日指标，而算指标是回测里最贵的一步。
    # 默认交易计划只用到止盈止损（看 OHLC 就够），所以这里能省掉一大截。
    # ⚠️ 买信号采集那一步也必须算指标，那是省不掉的。
    need_series = bool(plan.exit_conditions)

    def load(code: str):
        entry = cache.get(code)
        if entry is None:
            bars = bar_store.bars(code)
            if need_series:
                series = bt_data.compute_series(bars)
                index = {m["date"]: i for i, m in enumerate(series) if m}
            else:
                series = []
                index = {b.date: i for i, b in enumerate(bars)}
            entry = (bars, series, index)
            cache[code] = entry
        return entry

    cash = float(capital)
    positions: dict[str, _Position] = {}
    trades: list[dict[str, Any]] = []
    equity_curve: list[dict[str, Any]] = []
    skipped_limit_up = 0
    skipped_no_cash = 0

    for day_index, today in enumerate(calendar):
        # ---------- 1. 用今天的开盘价执行「昨天收盘产生的信号」 ----------
        if day_index > 0:
            pending = signals.get(calendar[day_index - 1], [])
            for signal in pending:
                code = signal["code"]
                if code in positions or len(positions) >= plan.max_positions:
                    continue
                bars, series, index_map = load(code)
                i = index_map.get(today)
                if i is None:
                    continue                       # 停牌
                bar = bars[i]
                prev_close = bars[i - 1].close if i > 0 else bar.open
                if _is_limit_up_open(bar, prev_close, code):
                    skipped_limit_up += 1
                    continue

                budget = min(capital * plan.position_pct / 100.0, cash)
                shares = int(budget / bar.open / MIN_LOT) * MIN_LOT
                if shares < MIN_LOT:
                    skipped_no_cash += 1
                    continue
                amount = shares * bar.open
                fee = _buy_fee(amount)
                if amount + fee > cash:
                    shares -= MIN_LOT
                    if shares < MIN_LOT:
                        skipped_no_cash += 1
                        continue
                    amount = shares * bar.open
                    fee = _buy_fee(amount)
                cash -= amount + fee
                positions[code] = _Position(
                    code=code, shares=shares, buy_date=today, buy_price=bar.open,
                    buy_index=i, buy_day=day_index, cost=amount + fee,
                )

        # ---------- 2. 处理持仓的卖出 ----------
        for code in list(positions.keys()):
            position = positions[code]
            bars, series, index_map = load(code)
            i = index_map.get(today)
            if i is None:
                continue                            # 停牌，继续持有
            if today <= position.buy_date:
                continue                            # T+1：当天买不能当天卖

            bar = bars[i]
            metrics = (series[i] if series else None) or {}
            prev_metrics = series[i - 1] if (series and i > 0) else None
            prev_close = bars[i - 1].close if i > 0 else bar.open
            held = day_index - position.buy_day

            price: float | None = None
            reason = ""

            # 一字跌停卖不出，只能顺延
            if _is_limit_down(bar, prev_close, code):
                continue

            # 止损优先于止盈（同一天都触发时按更坏的算）
            if plan.stop_loss is not None:
                stop_price = position.buy_price * (1 + plan.stop_loss / 100.0)
                if bar.low <= stop_price:
                    price = min(bar.open, stop_price) if bar.open < stop_price else stop_price
                    reason = REASON_STOP

            if price is None and plan.take_profit is not None:
                target = position.buy_price * (1 + plan.take_profit / 100.0)
                if bar.high >= target:
                    price = max(bar.open, target) if bar.open > target else target
                    reason = REASON_TAKE

            if price is None and plan.exit_conditions:
                hit = evaluate_exit_conditions(
                    plan.exit_conditions, metrics, prev_metrics, position.buy_price
                )
                if hit:
                    price = bar.close
                    reason = f"{REASON_COND}：{hit}"

            if price is None and held >= plan.hold_days:
                price = bar.close
                reason = REASON_HOLD

            if price is None:
                continue

            amount = position.shares * price
            fee = _sell_fee(amount)
            cash += amount - fee
            pnl = amount - fee - position.cost
            trades.append({
                "code": code,
                "name": "",
                "buy_date": position.buy_date,
                "buy_price": round(position.buy_price, 3),
                "sell_date": today,
                "sell_price": round(price, 3),
                "shares": position.shares,
                "hold_days": held,
                "pnl": round(pnl, 2),
                "pnl_pct": round((price / position.buy_price - 1) * 100, 2),
                "cost": round(position.cost, 2),
                "reason": reason,
            })
            del positions[code]

        # ---------- 3. 记录当日净值 ----------
        market_value = 0.0
        for code, position in positions.items():
            bars, series, index_map = load(code)
            i = index_map.get(today)
            price = bars[i].close if i is not None else position.buy_price
            market_value += position.shares * price
        equity_curve.append({
            "date": today,
            "equity": round(cash + market_value, 2),
            "cash": round(cash, 2),
            "position_count": len(positions),
        })

    # ---------- 收尾：回测结束时按最后一天收盘价平仓 ----------
    if positions:
        last_day = calendar[-1]
        for code, position in list(positions.items()):
            bars, series, index_map = load(code)
            i = index_map.get(last_day)
            price = bars[i].close if i is not None else position.buy_price
            amount = position.shares * price
            fee = _sell_fee(amount)
            cash += amount - fee
            trades.append({
                "code": code,
                "name": "",
                "buy_date": position.buy_date,
                "buy_price": round(position.buy_price, 3),
                "sell_date": last_day,
                "sell_price": round(price, 3),
                "shares": position.shares,
                "hold_days": _index_of(calendar, last_day) - _index_of(calendar, position.buy_date),
                "pnl": round(amount - fee - position.cost, 2),
                "pnl_pct": round((price / position.buy_price - 1) * 100, 2),
                "cost": round(position.cost, 2),
                "reason": REASON_END,
            })
            del positions[code]
        equity_curve[-1]["equity"] = round(cash, 2)
        equity_curve[-1]["cash"] = round(cash, 2)
        equity_curve[-1]["position_count"] = 0

    result = summarise(trades, equity_curve, capital, calendar)
    result["skipped"] = {
        "limit_up": skipped_limit_up,       # 因一字涨停没买进
        "no_cash": skipped_no_cash,         # 因资金不足没买进
    }
    return result


def _index_of(calendar: list[int], date: int) -> int:
    """用二分查日期在日历中的位置。"""
    import bisect

    pos = bisect.bisect_left(calendar, date)
    return pos if pos < len(calendar) else len(calendar) - 1


def _empty_result(capital: float) -> dict[str, Any]:
    return {
        "initial_capital": capital,
        "final_equity": capital,
        "total_return": 0.0,
        "annual_return": 0.0,
        "max_drawdown": 0.0,
        "trade_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "win_rate": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "avg_hold_days": 0.0,
        "trades": [],
        "equity_curve": [],
        "skipped": {"limit_up": 0, "no_cash": 0},
    }


# ------------------------------------------------------------------ 绩效统计

def summarise(
    trades: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
    capital: float,
    calendar: list[int],
) -> dict[str, Any]:
    """把交易记录和净值曲线汇总成绩效指标。"""
    if not equity_curve:
        return _empty_result(capital)

    final_equity = equity_curve[-1]["equity"]
    total_return = (final_equity / capital - 1) * 100

    days = len(calendar)
    annual_return = 0.0
    if days > 0 and final_equity > 0:
        years = days / TRADING_DAYS_PER_YEAR
        if years > 0:
            annual_return = ((final_equity / capital) ** (1 / years) - 1) * 100

    # 最大回撤
    peak = equity_curve[0]["equity"]
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point["equity"])
        if peak > 0:
            drawdown = (point["equity"] / peak - 1) * 100
            max_drawdown = min(max_drawdown, drawdown)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    return {
        "initial_capital": capital,
        "final_equity": round(final_equity, 2),
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        "max_drawdown": round(max_drawdown, 2),
        "trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (
            round(gross_win, 2) if gross_win > 0 else 0.0
        ),
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / len(trades), 1) if trades else 0.0,
        "trading_days": days,
        "trades": trades,
        "equity_curve": equity_curve,
        "skipped": {"limit_up": 0, "no_cash": 0},
    }


def explain_zero_trades(result: dict[str, Any], signals: dict[int, list[dict[str, Any]]]) -> None:
    """一笔都没成交时，把原因写进 result['zero_trade_reason']。

    必须由所有回测入口调用——服务端是直接调 simulate() 的，
    如果只在 run_backtest() 里写这段，界面就走不到，用户只看到一个 0 不知道为什么。
    """
    if result.get("trade_count"):
        return
    skipped = result.get("skipped") or {}
    skipped_cash = skipped.get("no_cash", 0)
    skipped_limit = skipped.get("limit_up", 0)
    signal_days = len(signals)

    if skipped_cash and not skipped_limit:
        result["zero_trade_reason"] = (
            "出现了买入信号，但初始资金买不起一手（100 股），所以一笔都没成交。"
            "这只股票单价较高时很常见——调高初始资金再试。"
        )
    elif skipped_cash and skipped_limit:
        result["zero_trade_reason"] = (
            f"买入信号触发了，但都没能成交：{skipped_limit} 次一字涨停买不进、"
            f"{skipped_cash} 次资金不够买一手。"
        )
    elif skipped_limit:
        result["zero_trade_reason"] = (
            f"买入信号触发了，但都撞上一字涨停买不进（{skipped_limit} 次）。"
        )
    elif signal_days == 0:
        result["zero_trade_reason"] = (
            "这段区间内买入条件一次都没触发。可以换个区间，"
            "或者换成「指定股票 + 只测卖出规则」的模式。"
        )
    else:
        result["zero_trade_reason"] = "有买入信号，但受涨停/停牌/资金限制没能成交。"


def run_backtest(
    vipdoc: str,
    params: dict[str, Any],
    plan: TradePlan | dict[str, Any],
    start_date: int,
    end_date: int,
    capital: float = 100000.0,
    signals: dict[int, list[dict[str, Any]]] | None = None,
    calendar: list[int] | None = None,
) -> dict[str, Any]:
    """跑一次完整回测（已有信号时可复用，用于参数扫描）。"""
    if isinstance(plan, dict):
        plan = TradePlan.from_dict(plan)

    if signals is None or calendar is None:
        signals, calendar, _ = collect_signals(vipdoc, params, start_date, end_date)

    bar_store = bt_data.BarStore(vipdoc, start_date, end_date)
    result = simulate(signals, calendar, bar_store, plan, capital)
    result["benchmark"] = benchmark(vipdoc, start_date, end_date)
    result["yearly"] = yearly_breakdown(result["equity_curve"], capital)
    explain_zero_trades(result, signals)
    return result


# ------------------------------------------------------------------ 基准与稳定性

# 基准指数，按优先级找（沪深300 最能代表大盘）
BENCHMARK_CANDIDATES = (
    ("sh000300", "沪深300"),
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
)


def benchmark(vipdoc: str, start_date: int, end_date: int) -> dict[str, Any] | None:
    """同期指数「买入并持有」的收益，作为对照基准。

    没有基准的收益率没有意义——大盘涨 40% 的时候赚 38%，其实是跑输了。
    """
    from pathlib import Path as _Path

    from ..engine.tdx import read_day as _read_day

    for filename, name in BENCHMARK_CANDIDATES:
        market, code = filename[:2], filename[2:]
        path = _Path(vipdoc) / market / "lday" / f"{filename}.day"
        if not path.is_file():
            continue
        bars = [b for b in _read_day(path, max_records=4000) if start_date <= b.date <= end_date]
        if len(bars) < 2:
            continue
        return {
            "code": code,
            "name": name,
            "return_pct": round((bars[-1].close / bars[0].close - 1) * 100, 2),
            "start_close": bars[0].close,
            "end_close": bars[-1].close,
            "curve": [{"date": b.date, "close": b.close} for b in bars],
        }
    return None


def yearly_breakdown(equity_curve: list[dict[str, Any]], capital: float) -> list[dict[str, Any]]:
    """按自然年拆分收益：看策略是不是只在某一段行情里有效。"""
    if not equity_curve:
        return []

    buckets: dict[int, list[dict[str, Any]]] = {}
    for point in equity_curve:
        buckets.setdefault(point["date"] // 10000, []).append(point)

    out: list[dict[str, Any]] = []
    base = capital
    for year in sorted(buckets):
        points = buckets[year]
        start_equity = base
        end_equity = points[-1]["equity"]
        peak = start_equity
        drawdown = 0.0
        for point in points:
            peak = max(peak, point["equity"])
            if peak > 0:
                drawdown = min(drawdown, (point["equity"] / peak - 1) * 100)
        out.append({
            "year": year,
            "return_pct": round((end_equity / start_equity - 1) * 100, 2) if start_equity else 0.0,
            "max_drawdown": round(drawdown, 2),
            "trading_days": len(points),
            "end_equity": round(end_equity, 2),
        })
        base = end_equity
    return out


def _segment_bounds(calendar: list[int], folds: int) -> list[tuple[int, int]]:
    """把交易日历切成 folds+1 段，返回每段的 (起, 止)。"""
    total = len(calendar)
    size = total // (folds + 1)
    if size <= 0:
        return []
    bounds = []
    for i in range(folds + 1):
        lo = i * size
        hi = (i + 1) * size if i < folds else total
        bounds.append((calendar[lo], calendar[hi - 1]))
    return bounds


def walk_forward(
    vipdoc: str,
    params: dict[str, Any],
    base_plan: dict[str, Any] | TradePlan,
    start_date: int,
    end_date: int,
    grid: dict[str, list[Any]],
    folds: int = 3,
    capital: float = 100000.0,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """样本外滚动验证 —— 判断策略是真有效，还是调参调出来的。

    把区间切成 folds+1 段。每一折：
      1. 在前一段（样本内）用参数网格挑出表现最好的一组
      2. 拿这组参数到**下一段没见过的数据**（样本外）上跑
    滚动推进，汇总所有样本外片段的表现。

    样本内漂亮、样本外崩掉，就是过拟合——这是区分「真策略」和「碰巧」的关键。
    """
    if isinstance(base_plan, dict):
        base_plan = TradePlan.from_dict(base_plan)

    signals, calendar, scanned = collect_signals(
        vipdoc, params, start_date, end_date, progress=progress
    )
    if not calendar:
        return {"ok": False, "error": "回测区间内没有交易日数据"}

    bounds = _segment_bounds(calendar, max(1, min(6, folds)))
    if len(bounds) < 2:
        return {"ok": False, "error": "区间太短，切不出样本内/样本外两段"}

    # 展开参数网格（与 sweep 一致，上限 60 组）
    keys = [k for k in grid if grid[k]]
    combos: list[dict[str, Any]] = [{}]
    for key in keys:
        values = grid[key][:10]
        combos = [dict(c, **{key: v}) for c in combos for v in values]
        if len(combos) > 60:
            combos = combos[:60]
            break

    def build_plan(combo: dict[str, Any]) -> TradePlan:
        return TradePlan(
            hold_days=int(combo.get("hold_days", base_plan.hold_days)),
            stop_loss=(None if combo.get("stop_loss") in (None, "")
                       else -abs(float(combo.get("stop_loss", base_plan.stop_loss or 8)))),
            take_profit=(base_plan.take_profit if combo.get("take_profit") in (None, "")
                         else abs(float(combo["take_profit"]))),
            max_positions=int(combo.get("max_positions", base_plan.max_positions)),
            position_pct=float(combo.get("position_pct", base_plan.position_pct)),
            exit_conditions=base_plan.exit_conditions,
        )

    bar_store = bt_data.BarStore(vipdoc, start_date, end_date)
    fold_results: list[dict[str, Any]] = []
    equity = capital                       # 样本外资金曲线（逐折复利滚动）

    for index in range(len(bounds) - 1):
        in_lo, in_hi = bounds[index]
        out_lo, out_hi = bounds[index + 1]

        # ---- 样本内：从参数网格里挑最优 ----
        best_combo: dict[str, Any] = {}
        best_return: float | None = None
        for combo in combos:
            outcome = simulate(signals, calendar, bar_store, build_plan(combo),
                               capital, start_date=in_lo, end_date=in_hi)
            if best_return is None or outcome["total_return"] > best_return:
                best_return, best_combo = outcome["total_return"], combo
        if progress:
            progress(index + 1, len(bounds) - 1)

        # ---- 样本外：用挑出来的参数跑没见过的数据 ----
        out_plan = build_plan(best_combo)
        out = simulate(signals, calendar, bar_store, out_plan, equity,
                       start_date=out_lo, end_date=out_hi)
        equity = out["final_equity"]

        fold_results.append({
            "fold": index + 1,
            "in_sample": {"start": in_lo, "end": in_hi, "return_pct": round(best_return or 0, 2)},
            "out_sample": {
                "start": out_lo, "end": out_hi,
                "return_pct": out["total_return"],
                "max_drawdown": out["max_drawdown"],
                "trade_count": out["trade_count"],
                "win_rate": out["win_rate"],
            },
            "chosen_plan": {
                "hold_days": out_plan.hold_days,
                "stop_loss": out_plan.stop_loss,
                "take_profit": out_plan.take_profit,
            },
        })

    compounded = 1.0
    for fold in fold_results:
        compounded *= (1 + fold["out_sample"]["return_pct"] / 100)
    oos_total = (compounded - 1) * 100
    is_avg = sum(f["in_sample"]["return_pct"] for f in fold_results) / len(fold_results)
    oos_avg = sum(f["out_sample"]["return_pct"] for f in fold_results) / len(fold_results)
    distinct = {(f["chosen_plan"]["hold_days"], f["chosen_plan"]["stop_loss"]) for f in fold_results}

    # 结论判定：这是整个功能的落点
    if is_avg <= 0:
        verdict = "样本内本身就赚不到钱 —— 买入条件无效，调卖点救不回来"
        level = "bad"
    elif oos_total < 0:
        verdict = "样本内赚钱、样本外亏钱 —— 典型的过拟合，这组参数不能上实盘"
        level = "bad"
    elif oos_total < is_avg * 0.5:
        verdict = "样本外明显弱于样本内 —— 有过度调参的迹象，参数不稳"
        level = "warn"
    else:
        verdict = "样本外表现与样本内接近 —— 策略对参数不敏感，相对可信"
        level = "good"

    if len(distinct) > 1 and level == "good":
        verdict += "（但各折选出的最优参数不一致，说明最优区间偏平，别把某一组当标准答案）"

    return {
        "ok": True,
        "scanned": scanned,
        "folds": fold_results,
        "summary": {
            "in_sample_avg": round(is_avg, 2),
            "out_sample_avg": round(oos_avg, 2),
            "out_sample_total": round(oos_total, 2),
            "final_equity": round(equity, 2),
            "initial_capital": capital,
            "distinct_plans": len(distinct),
            "verdict": verdict,
            "level": level,
        },
    }


# ------------------------------------------------------------------ 参数扫描

def sweep(
    vipdoc: str,
    params: dict[str, Any],
    base_plan: dict[str, Any] | TradePlan,
    start_date: int,
    end_date: int,
    grid: dict[str, list[Any]],
    capital: float = 100000.0,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """参数扫描：同一批买入信号，换不同卖出参数各跑一遍，看哪组更稳。

    信号只算一次（那是最慢的一步），所以扫描比跑多次独立回测快得多。
    组合数上限 60，避免一次扫出上千次模拟。
    """
    if isinstance(base_plan, dict):
        base_plan = TradePlan.from_dict(base_plan)

    # 展开参数网格
    keys = [k for k in grid if grid[k]]
    combos: list[dict[str, Any]] = [{}]
    for key in keys:
        values = grid[key][:10]                 # 每个维度最多 10 个候选
        combos = [dict(c, **{key: v}) for c in combos for v in values]
        if len(combos) > 60:
            combos = combos[:60]
            break

    signals, calendar, scanned = collect_signals(
        vipdoc, params, start_date, end_date, progress=progress
    )
    if not calendar:
        return {"ok": False, "error": "回测区间内没有交易日数据", "results": []}

    bar_store = bt_data.BarStore(vipdoc, start_date, end_date)
    results: list[dict[str, Any]] = []
    for index, combo in enumerate(combos):
        plan = TradePlan(
            hold_days=int(combo.get("hold_days", base_plan.hold_days)),
            stop_loss=(None if combo.get("stop_loss") in (None, "")
                       else -abs(float(combo.get("stop_loss", base_plan.stop_loss or 8)))),
            take_profit=(base_plan.take_profit if combo.get("take_profit") in (None, "")
                         else abs(float(combo["take_profit"]))),
            max_positions=int(combo.get("max_positions", base_plan.max_positions)),
            position_pct=float(combo.get("position_pct", base_plan.position_pct)),
            exit_conditions=base_plan.exit_conditions,
        )
        outcome = simulate(signals, calendar, bar_store, plan, capital)
        results.append({
            "plan": {
                "hold_days": plan.hold_days,
                "stop_loss": plan.stop_loss,
                "take_profit": plan.take_profit,
            },
            "total_return": outcome["total_return"],
            "annual_return": outcome["annual_return"],
            "max_drawdown": outcome["max_drawdown"],
            "trade_count": outcome["trade_count"],
            "win_rate": outcome["win_rate"],
            "profit_factor": outcome["profit_factor"],
            "avg_hold_days": outcome["avg_hold_days"],
        })
        if progress:
            progress(index + 1, len(combos))

    # 按收益排序，但把最大回撤一起带出来——只有收益高、回撤也可控的才算好
    results.sort(key=lambda r: -r["total_return"])
    best = results[0] if results else None
    return {
        "ok": True,
        "scanned": scanned,
        "signal_days": len(signals),
        "calendar_days": len(calendar),
        "combos": len(combos),
        "results": results,
        "best": best,
    }
