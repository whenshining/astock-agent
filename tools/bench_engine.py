"""引擎性能与正确性实测脚本（开发用，不参与打包）。

用法：python tools/bench_engine.py [--single]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.engine import cache_status, scan_market, screen, stats  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--single", action="store_true", help="单进程扫描（对比多进程）")
    parser.add_argument("--db", default=str(ROOT / ".bench.db"))
    args = parser.parse_args()

    vipdoc = config.get("vipdoc_path")
    print(f"vipdoc    : {vipdoc}")
    print(f"数据库    : {args.db}")
    print(f"并行模式  : {'单进程' if args.single else '多进程'}")

    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.time()
        if now - last[0] > 1.0:
            last[0] = now
            print(f"  扫描中 {done}/{total}", end="\r", flush=True)

    workers = 1 if args.single else None
    t0 = time.time()
    result = scan_market(vipdoc, args.db, progress=progress, workers=workers)
    wall = time.time() - t0
    print(" " * 40, end="\r")
    print(f"\n扫描结果: {result}")
    print(f"墙钟耗时: {wall:.2f}s")

    if not result.get("ok"):
        return 1

    print("\n缓存状态:", cache_status(args.db))
    print("市场概览:", stats(args.db))

    print("\n--- 筛选：涨幅>=0 且 量比>=1.5，按量比排序 ---")
    r = screen(args.db, {"chg_min": 0, "vol_ratio_min": 1.5, "sort_by": "vol_ratio", "limit": 10})
    print(f"命中 {r['total']} 只，耗时 {r['elapsed_ms']}ms，条件：{'；'.join(r['conditions'])}")
    for s in r["stocks"][:10]:
        print(f"  {s['code']} 收{s['close']:>8} 涨{s['chg']:>6}% 量比{s['vol_ratio']} 振幅{s['amplitude']}% 连涨{s['consec_up']}")

    print("\n--- 筛选：均线多头 且 创60日新高 ---")
    r2 = screen(args.db, {"ma_bull": True, "new_high60": True, "sort_by": "chg", "limit": 10})
    print(f"命中 {r2['total']} 只（{r2['elapsed_ms']}ms）")
    for s in r2["stocks"][:10]:
        print(f"  {s['code']} 收{s['close']:>8} 涨{s['chg']:>6}% ma5={s['ma5']} ma20={s['ma20']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
