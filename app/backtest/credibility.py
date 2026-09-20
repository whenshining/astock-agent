"""回测可信度评估。

给一次回测结果打分——但**不是给一个假的精确分数**（"72.3 分"只会给人虚假的确定感），
而是给出等级 + **逐条列出哪一项没过关**。用户需要知道的是"哪里可疑"，
不是"总分多少"。

## 核心原则：单次回测封顶「中」

样本内回测**永远无法证明策略有效**——它只是在同一段历史上调出来的结果。
所以：

    没做样本外验证  →  最高只能到「中」
    样本外验证不通过 →  直接「低」

这条封顶规则是整个评估里最重要的一条。没有它，一个交易 600 次、跑赢基准、
参数稳定的**过拟合**策略会被打成「高可信」——那比不打分更危险。

## 检查项

| 项 | 为什么重要 |
|---|---|
| 交易笔数 | 20 笔的结果没有统计意义，可能全是运气 |
| 回测区间 | 太短的区间覆盖不到不同行情 |
| **样本外验证** | 唯一能区分「真有效」和「调参调出来的」手段 |
| 参数稳定性 | 换个止损就天差地别 = 碰巧凑出来的 |
| **收益集中度** | 利润全靠少数几笔 = 结果不可重复 |
| 相对基准 | 跑输基准说明这策略没有产生任何价值 |
| 成交可执行性 | 大量信号因一字涨停买不进 = 回测虚高 |
| 数据质量 | 区间内有除权但未复权的股票占比 |
"""

from __future__ import annotations

from typing import Any

# 等级
LEVEL_HIGH = "high"
LEVEL_MEDIUM = "medium"
LEVEL_LOW = "low"

_LEVEL_LABEL = {"high": "较高", "medium": "中等", "low": "偏低"}


def _check(name: str, status: str, detail: str, weight: int = 1) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "weight": weight}


def assess(
    result: dict[str, Any],
    *,
    validated: dict[str, Any] | None = None,
    sweep: dict[str, Any] | None = None,
    adjusted_ratio: float | None = None,
) -> dict[str, Any]:
    """评估一次回测的可信度。

    validated     ：样本外验证结果（做过才传），取自 walk_forward()
    sweep         ：参数扫描结果（做过才传），取自 sweep()
    adjusted_ratio：回测涉及的股票里，区间内发生过除权的比例
    """
    checks: list[dict[str, Any]] = []

    # ---------- 1. 交易笔数 ----------
    trades = int(result.get("trade_count") or 0)
    if trades >= 100:
        checks.append(_check("交易笔数", "pass", f"{trades} 笔，样本量充足", 3))
    elif trades >= 30:
        checks.append(_check("交易笔数", "warn", f"{trades} 笔，偏少，结果有一定偶然性", 3))
    else:
        checks.append(_check("交易笔数", "fail",
                             f"只有 {trades} 笔，样本量太小，结果基本是运气", 3))

    # ---------- 2. 回测区间 ----------
    days = int(result.get("trading_days") or 0)
    if days >= 244:
        checks.append(_check("回测区间", "pass", f"{days} 个交易日（约 1 年以上）", 2))
    elif days >= 122:
        checks.append(_check("回测区间", "warn", f"{days} 个交易日，只覆盖半年，行情类型单一", 2))
    else:
        checks.append(_check("回测区间", "fail",
                             f"只有 {days} 个交易日，太短，说明不了问题", 2))

    # ---------- 3. 样本外验证（最重要的一项）----------
    if validated is None:
        checks.append(_check("样本外验证", "warn",
                             "没做过。单次回测是样本内结果，只能说明「历史上是这样」，"
                             "不能说明「将来也这样」——这是判断策略真假最关键的一步", 5))
    else:
        summary = validated.get("summary") or {}
        level = summary.get("level")
        verdict = summary.get("verdict") or ""
        if level == "good":
            checks.append(_check("样本外验证", "pass", f"已通过：{verdict}", 5))
        elif level == "warn":
            checks.append(_check("样本外验证", "warn", f"存疑：{verdict}", 5))
        else:
            checks.append(_check("样本外验证", "fail", f"未通过：{verdict}", 5))

    # ---------- 4. 参数稳定性 ----------
    if sweep is None:
        # 没做扫描不是「疑点」，只是未知——用 info，不要因此挡住「高」
        checks.append(_check("参数稳定性", "info", "没做过参数扫描，不知道结果对参数是否敏感", 2))
    else:
        returns = [r.get("total_return", 0) for r in (sweep.get("results") or [])]
        if len(returns) >= 2:
            spread = max(returns) - min(returns)
            if spread < 15:
                checks.append(_check("参数稳定性", "pass",
                                     f"不同参数下收益跨度 {spread:.1f} 个百分点，比较稳定", 2))
            elif spread < 30:
                checks.append(_check("参数稳定性", "warn",
                                     f"收益跨度 {spread:.1f} 个百分点，对参数有一定依赖", 2))
            else:
                checks.append(_check("参数稳定性", "fail",
                                     f"收益跨度高达 {spread:.1f} 个百分点——"
                                     "换个参数结果就天差地别，多半是参数碰巧凑出来的", 2))

    # ---------- 5. 收益集中度 ----------
    trade_list = result.get("trades") or []
    profits = [t.get("pnl", 0) for t in trade_list if t.get("pnl", 0) > 0]
    total_profit = sum(profits)
    if profits and total_profit > 0:
        top = max(profits)
        share = top / total_profit * 100
        if share < 30:
            checks.append(_check("收益集中度", "pass",
                                 f"最大单笔盈利占总盈利 {share:.0f}%，比较分散", 2))
        elif share < 50:
            checks.append(_check("收益集中度", "warn",
                                 f"最大单笔盈利占总盈利 {share:.0f}%，有点集中", 2))
        else:
            checks.append(_check("收益集中度", "fail",
                                 f"最大单笔盈利就占了总盈利的 {share:.0f}%——"
                                 "去掉那一笔结果就崩了，不可重复", 2))
    else:
        checks.append(_check("收益集中度", "warn", "没有盈利交易，无从判断", 2))

    # ---------- 6. 相对基准 ----------
    benchmark = result.get("benchmark") or {}
    if benchmark:
        excess = float(result.get("total_return", 0)) - float(benchmark.get("return_pct", 0))
        name = benchmark.get("name", "基准")
        if excess > 0:
            checks.append(_check("相对基准", "pass",
                                 f"跑赢{name} {excess:.1f} 个百分点", 3))
        else:
            checks.append(_check("相对基准", "fail",
                                 f"跑输{name} {abs(excess):.1f} 个百分点——"
                                 "同期直接买指数比它强，这个策略没有产生超额价值", 3))
    else:
        checks.append(_check("相对基准", "warn", "没有基准数据可比", 3))

    # ---------- 7. 成交可执行性 ----------
    skipped = result.get("skipped") or {}
    limit_up = int(skipped.get("limit_up") or 0)
    signal_days = int(result.get("signal_days") or 0)
    # 用「买不进次数 / (成交笔数 + 买不进次数)」估算受阻比例
    attempts = trades + limit_up
    if attempts > 0:
        blocked = limit_up / attempts * 100
        if blocked < 10:
            checks.append(_check("成交可执行性", "pass",
                                 f"因一字涨停没买进 {limit_up} 次（占 {blocked:.0f}%），可执行性好", 2))
        elif blocked < 30:
            checks.append(_check("成交可执行性", "warn",
                                 f"因一字涨停没买进 {limit_up} 次（占 {blocked:.0f}%），"
                                 "实际交易中可能更难成交", 2))
        else:
            checks.append(_check("成交可执行性", "fail",
                                 f"有 {blocked:.0f}% 的买入信号因一字涨停买不进——"
                                 "这个策略现实中很难落地", 2))

    # ---------- 8. 数据质量 ----------
    if adjusted_ratio is not None:
        pct = adjusted_ratio * 100
        if pct < 5:
            checks.append(_check("数据质量", "pass",
                                 f"回测涉及的股票里 {pct:.1f}% 区间内发生过除权（已复权处理）", 1))
        elif pct < 20:
            checks.append(_check("数据质量", "warn",
                                 f"有 {pct:.0f}% 的股票区间内发生过除权（已复权处理）", 1))
        else:
            checks.append(_check("数据质量", "warn",
                                 f"有 {pct:.0f}% 的股票区间内发生过除权，"
                                 "复权是检测式的，可能有个别误差", 1))

    # ---------- 汇总 ----------
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    infos = [c for c in checks if c["status"] == "info"]
    weighted_total = sum(c["weight"] for c in checks)
    weighted_bad = sum(c["weight"] for c in fails) + sum(c["weight"] * 0.4 for c in warns)
    score = max(0, min(100, round((1 - weighted_bad / max(1, weighted_total)) * 100)))

    if fails:
        level = LEVEL_LOW
    elif warns:
        level = LEVEL_MEDIUM
    else:
        level = LEVEL_HIGH

    # ★ 核心封顶：没有样本外验证就绝不给「高」
    capped = False
    if level == LEVEL_HIGH and validated is None:
        level = LEVEL_MEDIUM
        capped = True

    # 样本外不通过时，哪怕其他项都过也不能高
    if validated is not None and (validated.get("summary") or {}).get("level") == "bad":
        level = LEVEL_LOW

    # 分值必须和等级一致：不能让「中等」旁边写着 96 分，那比不给分更误导
    score = min(score, {LEVEL_LOW: 49, LEVEL_MEDIUM: 79, LEVEL_HIGH: 100}[level])

    return {
        "level": level,
        "label": _LEVEL_LABEL[level],
        "score": score,
        "capped": capped,
        "checks": checks,
        "fail_count": len(fails),
        "warn_count": len(warns),
        "info_count": len(infos),
        "summary": _summarize(level, fails, warns, capped, validated),
    }


def _summarize(level: str, fails: list, warns: list, capped: bool,
               validated: dict | None) -> str:
    if level == LEVEL_LOW:
        first = fails[0]["name"] if fails else "样本外验证"
        return f"可信度偏低：{first}这一项没过关，结论不能作为依据。"
    if level == LEVEL_MEDIUM:
        if capped:
            return ("最高只能给到「中等」——因为没做样本外验证。"
                    "单次回测是样本内结果，无法证明策略在没见过的数据上也有效。")
        return f"可信度中等：有 {len(warns)} 项存在疑点，建议先看下面的清单。"
    return "各主要维度都过关，且通过了样本外验证。"
