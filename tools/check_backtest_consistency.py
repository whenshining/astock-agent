"""一致性校验：回测的条件判断 vs 实时筛选。

回测和实盘是两套实现（一个 Python 逐日算、一个 SQL 查缓存），
只要口径有一点偏差，回测出来的策略拿到实盘就是另一回事。

本脚本用最新交易日做全市场比对：同样的条件，两边选出的股票集合必须完全相同。
用法：python tools/check_backtest_consistency.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.backtest import data as bt_data  # noqa: E402
from app.engine import screener, store  # noqa: E402

# 覆盖各类条件的测试用例
CASES: list[tuple[str, dict]] = [
    ("涨幅+量比", {"chg_min": 2, "vol_ratio_min": 2}),
    ("均线多头+创新高", {"ma_bull": True, "new_high60": True}),
    ("价格区间", {"min_price": 5, "max_price": 30}),
    ("成交额", {"amount_min": 500000000}),
    ("连涨", {"consec_up_min": 3}),
    ("5日涨幅", {"chg5_min": 5}),
    ("20日涨幅", {"chg20_min": 10}),
    ("振幅区间", {"amplitude_min": 3, "amplitude_max": 8}),
    ("20日均振幅上限", {"amp20_max": 4}),
    ("距60日高点", {"min_drawdown60": -5}),
    ("板块限定", {"boards": ["chinext"], "chg_min": 0}),
    ("组合条件", {"chg_min": 1, "chg_max": 7, "vol_ratio_min": 1.5,
                  "amount_min": 200000000, "ma_bull": True, "sort_by": "vol_ratio"}),
    ("无量比数据也参与", {"chg_min": -1, "chg_max": 1}),
    ("创20日新高", {"new_high20": True}),
]


def main() -> int:
    vipdoc = config.get_vipdoc()
    db = config.db_path()

    print(f"vipdoc = {vipdoc}")
    print(f"数据库 = {db}")

    # 必须用 ensure_cache：它会在缓存版本过旧时自动重建。
    # 用 cache_status 只看"有没有缓存"，缓存是旧版本代码建的也会被当成可用，
    # 结果就是拿旧缓存比对新逻辑，跑出一堆假的不一致（这个坑踩过一次）。
    store.ensure_cache(vipdoc, db)
    status = store.cache_status(db)
    trade_date = status["trade_date"]
    print(f"最新交易日 = {trade_date}   （缓存版本 v{status['scan_version']}，"
          f"当前代码 v{store.SCAN_VERSION}）\n")

    # ---- 回测口径：全市场逐日算指标，取最新交易日 ----
    t0 = time.time()
    store_bt = bt_data.BarStore(vipdoc, trade_date, trade_date)
    market: dict[str, dict] = {}
    for code, _market in bt_data.filter_codes(vipdoc):
        series = bt_data.compute_series(store_bt.bars(code))
        last = series[-1] if series else None
        if last and last["date"] == trade_date:
            market[code] = last
    elapsed = time.time() - t0
    print(f"回测口径：全市场 {len(market)} 只在 {trade_date} 有指标（耗时 {elapsed:.1f}s）\n")

    print(f"{'用例':<20}{'实时筛选':>8}{'回测口径':>8}   结果")
    print("-" * 62)
    failures = 0
    for label, params in CASES:
        # screener 单次最多返回 500 条，这里翻页取全量再比对
        live_codes: set[str] = set()
        offset = 0
        total = None
        while True:
            page = screener.screen(db, dict(params, limit=500, offset=offset))
            if total is None:
                total = page["total"]
            live_codes.update(s["code"] for s in page["stocks"])
            if len(live_codes) >= total or not page["stocks"]:
                break
            offset += 500

        bt_codes = {c for c, m in market.items() if bt_data.passes(params, m, c)}

        same = live_codes == bt_codes
        if not same:
            failures += 1
        detail = ""
        if not same:
            only_live = sorted(live_codes - bt_codes)[:4]
            only_bt = sorted(bt_codes - live_codes)[:4]
            detail = f"仅实时={only_live} 仅回测={only_bt}"
        print(f"{label:<20}{len(live_codes):>8}{len(bt_codes):>8}   "
              f"{'✓ 完全一致' if same else '✗ 不一致 ' + detail}")

    print()
    if failures:
        print(f"⚠ {failures}/{len(CASES)} 个用例口径不一致——回测结果不可信，必须先修")
        return 1
    print(f"✓ {len(CASES)} 个用例全部一致：回测口径与实时筛选完全对齐")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
