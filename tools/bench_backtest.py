"""回测信号采集的性能基准与并行验证。

必须写成文件跑（不能用 python - 从 stdin 跑）：
Windows 下多进程用 spawn 启动子进程，需要能重新导入 __main__，
stdin 脚本没有可导入的主模块，并行会静默退化成单进程。

用法：python tools/bench_backtest.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.backtest import data as bt  # noqa: E402
from app.backtest import engine  # noqa: E402

PARAMS = {"chg_min": 2, "chg_max": 7, "vol_ratio_min": 2, "amount_min": 5e8}


def main() -> int:
    vip = config.get_vipdoc()
    calendar = bt.trading_dates(vip, 20200101, 20991231)
    end, start = calendar[-1], calendar[-244]
    print(f"vipdoc={vip}")
    print(f"区间 {start}~{end}（{len([d for d in calendar if start <= d <= end])} 个交易日）")
    print(f"CPU {os.cpu_count()} 核，默认并行度 {engine.signal_workers()}")
    print()

    # 先确认并行路径本身能不能跑通（不再静默吞异常）
    codes = [code for code, _ in bt.filter_codes(vip)]
    tasks = [(str(vip), c, start, end, PARAMS) for c in codes]
    print(f"全市场 {len(tasks)} 只")
    print()

    timings = {}
    for label, workers in (("单进程", 1), ("并行", None)):
        t0 = time.time()
        signals, cal, scanned = engine.collect_signals(vip, PARAMS, start, end, workers=workers)
        elapsed = time.time() - t0
        timings[label] = elapsed
        total = sum(len(v) for v in signals.values())
        print(f"  {label:6} {elapsed:6.1f}s   扫描 {scanned} 只，信号 {total} 个")

    # 校验并行与串行结果完全一致——并行最容易出的错就是结果对不上
    s1, _, _ = engine.collect_signals(vip, PARAMS, start, end, workers=1)
    s2, _, _ = engine.collect_signals(vip, PARAMS, start, end, workers=None)
    a = {d: sorted(x["code"] for x in v) for d, v in s1.items()}
    b = {d: sorted(x["code"] for x in v) for d, v in s2.items()}
    print()
    print(f"  并行结果与单进程完全一致：{a == b}")
    if timings.get("单进程"):
        speedup = timings["单进程"] / max(0.01, timings.get("并行", 1))
        print(f"  加速比：{speedup:.1f}x")

    # ---- 端到端：优化后各功能实际要多久 ----
    print()
    print("=" * 62)
    print("端到端耗时")
    print("=" * 62)
    base = {"hold_days": 5, "stop_loss": 8, "take_profit": 15,
            "max_positions": 5, "position_pct": 20}
    grid = {"hold_days": [3, 5, 10], "stop_loss": [5, 8, 10]}

    for label, days in (("近 1 年", 244), ("近 2 年", 488), ("近 3 年", 732)):
        lo = calendar[-days]
        t0 = time.time()
        result = engine.run_backtest(vip, PARAMS, base, lo, end, capital=100000)
        elapsed = time.time() - t0
        status = engine.PARALLEL_STATUS
        tag = f"并行 {status['workers']} 进程" if status["parallel"] else "单进程"
        print(f"  回测 {label:5} {elapsed:5.1f}s  {tag:12} "
              f"收益 {result['total_return']:>7.2f}%  {result['trade_count']:>4} 笔")

    two_year = calendar[-488]
    t0 = time.time()
    swept = engine.sweep(vip, PARAMS, base, two_year, end, grid, capital=100000)
    print(f"  参数扫描 9 组      {time.time()-t0:5.1f}s   最佳 {swept['best']['total_return']:>7.2f}%")

    t0 = time.time()
    walked = engine.walk_forward(vip, PARAMS, base, two_year, end, grid, folds=3, capital=100000)
    print(f"  样本外验证 3 折    {time.time()-t0:5.1f}s   "
          f"{walked['summary']['level']:5} 样本外 {walked['summary']['out_sample_total']:+.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
