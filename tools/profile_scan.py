"""定位扫描瓶颈：遍历 / 读盘 / 计算 分别耗时多少。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.engine.indicators import compute  # noqa: E402
from app.engine.tdx import iter_stock_files, read_day  # noqa: E402


def main() -> None:
    vipdoc = config.get("vipdoc_path")

    t0 = time.perf_counter()
    files = list(iter_stock_files(vipdoc))
    t1 = time.perf_counter()
    print(f"遍历文件        : {len(files)} 个, {t1 - t0:.2f}s")

    t0 = time.perf_counter()
    bars_list = [read_day(p, max_records=160) for p, _, _ in files]
    t1 = time.perf_counter()
    print(f"读盘 (全量)     : {t1 - t0:.2f}s  ({(t1 - t0) / len(files) * 1000:.2f} ms/文件)")

    t0 = time.perf_counter()
    metrics = [compute(b) for b in bars_list]
    t1 = time.perf_counter()
    print(f"计算指标        : {t1 - t0:.2f}s")

    ok = sum(1 for m in metrics if m)
    print(f"有效股票        : {ok}")

    # 对比：只读所需的 61 条
    t0 = time.perf_counter()
    short = [read_day(p, max_records=70) for p, _, _ in files]
    t1 = time.perf_counter()
    print(f"读盘 (70 条)    : {t1 - t0:.2f}s")

    # 单独测一个文件的重复读取
    p = files[0][0]
    t0 = time.perf_counter()
    for _ in range(200):
        read_day(p, max_records=160)
    t1 = time.perf_counter()
    print(f"同一文件 x200   : {(t1 - t0) * 1000:.1f} ms  ({(t1 - t0) / 200 * 1000:.3f} ms/次)")


if __name__ == "__main__":
    main()
