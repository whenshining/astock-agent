"""全市场指标缓存。

策略：把整市场的日线指标一次性算好存进 SQLite，之后每次选股只是查表（毫秒级）。
日线数据没变（最新交易日相同）时不需要重扫。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

from .indicators import COLUMNS, MIN_BARS, compute
from .names import load_names
from .tdx import DEFAULT_MAX_RECORDS, iter_stock_files, read_day

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    code          TEXT PRIMARY KEY,
    market        TEXT,
    date          INTEGER,
    close         REAL,
    chg           REAL,
    chg5          REAL,
    chg20         REAL,
    amplitude     REAL,
    amp20         REAL,
    vol_ratio     REAL,
    volume        REAL,
    amount        REAL,
    amount_avg5   REAL,
    ma5           REAL,
    ma10          REAL,
    ma20          REAL,
    ma60          REAL,
    ma_bull       INTEGER,
    consec_up     INTEGER,
    consec_down   INTEGER,
    new_high60    INTEGER,
    new_high20    INTEGER,
    drawdown60    REAL,
    name          TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_metrics_date ON metrics(date);
CREATE INDEX IF NOT EXISTS idx_metrics_chg ON metrics(chg);
"""

_INSERT_SQL = (
    f"INSERT OR REPLACE INTO metrics ({', '.join(COLUMNS)}, name) "
    f"VALUES ({', '.join('?' * (len(COLUMNS) + 1))})"
)

_write_lock = threading.Lock()

# 缓存结构版本：改动扫描逻辑或指标字段时递增，
# 程序启动时会发现旧缓存版本不匹配并自动重建，避免用户看到过期/缺字段的数据。
# v3：日线改为前复权（见 engine/adjust.py），旧缓存的指标是在不复权价格上算的。
# v4：ma_bull 改为按 2 位小数比较（消除浮点误差导致的口径不一致）。
SCAN_VERSION = 4


def connect(db_file: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_file), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


# ---------------------------------------------------------------- 扫描

def _scan_one(args: tuple[str, int]) -> tuple[str, str, dict[str, Any]] | None:
    """单个文件的解析 + 指标计算（供进程池调用，必须是模块级函数）。"""
    path, max_records = args
    bars = read_day(path, max_records=max_records)
    if len(bars) < MIN_BARS:
        return None
    metrics = compute(bars)
    if metrics is None:
        return None
    p = Path(path)
    stem = p.stem
    return stem[2:], p.parent.parent.name, metrics


def _collect(files: list[tuple[str, int]], workers: int,
             progress: Callable[[int, int], None] | None) -> tuple[list[tuple[Any, ...]], int]:
    """扫描所有文件并返回 (待写入的行, 有效只数)。

    优先多进程；受限环境（沙箱禁止命名管道、杀软拦截子进程）下自动退回单进程。
    """
    total = len(files)
    if workers > 1:
        try:
            return _collect_parallel(files, workers, progress)
        except (OSError, PermissionError, ImportError, RuntimeError):
            if progress:
                progress(0, total)

    rows: list[tuple[Any, ...]] = []
    scanned = 0
    for done, args in enumerate(files, start=1):
        if progress and done % 500 == 0:
            progress(done, total)
        result = _scan_one(args)
        if result:
            code, market, metrics = result
            rows.append((code, market) + tuple(metrics[c] for c in COLUMNS[2:]) + ("",))
            scanned += 1
    return rows, scanned


def _collect_parallel(files: list[tuple[str, int]], workers: int,
                      progress: Callable[[int, int], None] | None) -> tuple[list[tuple[Any, ...]], int]:
    rows: list[tuple[Any, ...]] = []
    scanned = 0
    total = len(files)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(_scan_one, files, chunksize=64):
            done += 1
            if progress and done % 500 == 0:
                progress(done, total)
            if result:
                code, market, metrics = result
                rows.append((code, market) + tuple(metrics[c] for c in COLUMNS[2:]) + ("",))
                scanned += 1
    return rows, scanned


def scan_market(
    vipdoc: str,
    db_file: str | Path,
    max_records: int = DEFAULT_MAX_RECORDS,
    progress: Callable[[int, int], None] | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    """全量扫描 vipdoc，把指标写入缓存表。返回统计信息。"""
    started = time.time()
    files = [(str(path), max_records) for path, _code, _market in iter_stock_files(vipdoc)]
    total = len(files)
    if total == 0:
        return {
            "ok": False,
            "error": f"未在 {vipdoc} 下找到日线文件，请检查 vipdoc 路径是否正确",
            "scanned": 0,
            "elapsed": 0.0,
        }

    if workers is None:
        if getattr(sys, "frozen", False):
            # 打包成 exe 后默认单进程：最稳，且避免 onefile 下子进程拖慢启动。
            # 想要更快的首扫可以设环境变量 ASTOCK_WORKERS=4。
            try:
                workers = max(1, int(os.environ.get("ASTOCK_WORKERS", "1")))
            except ValueError:
                workers = 1
        else:
            workers = min(8, max(1, (os.cpu_count() or 4) - 1))

    rows, scanned = _collect(files, workers, progress)

    # 回填股票名称（纯展示用，取不到就留空）
    names = load_names(vipdoc)
    if names:
        rows = [row[:-1] + (names.get(row[0], ""),) for row in rows]

    newest = max((r[2] for r in rows), default=0)

    with _write_lock:
        conn = connect(db_file)
        try:
            init_db(conn)
            conn.execute("DELETE FROM metrics")
            conn.executemany(_INSERT_SQL, rows)
            set_meta(conn, "scan_at", int(time.time()))
            set_meta(conn, "trade_date", newest)
            set_meta(conn, "vipdoc_path", vipdoc)
            set_meta(conn, "count", scanned)
            set_meta(conn, "scan_version", SCAN_VERSION)
            conn.commit()
        finally:
            conn.close()

    return {
        "ok": True,
        "scanned": scanned,
        "files": total,
        "trade_date": newest,
        "elapsed": round(time.time() - started, 2),
    }


def ensure_cache(
    vipdoc: str,
    db_file: str | Path,
    max_records: int = DEFAULT_MAX_RECORDS,
    progress: Callable[[int, int], None] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """按需建立缓存：缓存存在且数据源未变时直接复用。"""
    if force:
        return scan_market(vipdoc, db_file, max_records, progress)

    with _write_lock:
        conn = connect(db_file)
        try:
            init_db(conn)
            cached_vipdoc = get_meta(conn, "vipdoc_path")
            cached_date = get_meta(conn, "trade_date")
            cached_version = get_meta(conn, "scan_version")
            count = conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"]
        finally:
            conn.close()

    version_ok = (int(cached_version) if cached_version else 0) == SCAN_VERSION
    if count and cached_vipdoc == vipdoc and version_ok:
        latest = latest_day_on_disk(vipdoc)
        if latest is None or (cached_date and int(cached_date) >= latest):
            return {
                "ok": True,
                "cached": True,
                "scanned": count,
                "trade_date": int(cached_date) if cached_date else None,
                "elapsed": 0.0,
            }

    result = scan_market(vipdoc, db_file, max_records, progress)
    result["cached"] = False
    return result


def latest_day_on_disk(vipdoc: str, sample: int = 300) -> int | None:
    """抽样探测磁盘上最新的交易日（用于判断缓存是否过期）。"""
    newest = 0
    checked = 0
    for path, _code, _market in iter_stock_files(vipdoc):
        bars = read_day(path, max_records=1)
        if bars:
            newest = max(newest, bars[-1].date)
        checked += 1
        if checked >= sample:
            break
    return newest or None


def cache_status(db_file: str | Path) -> dict[str, Any]:
    with _write_lock:
        conn = connect(db_file)
        try:
            init_db(conn)
            count = conn.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"]
            scan_at = get_meta(conn, "scan_at")
            trade_date = get_meta(conn, "trade_date")
            vipdoc = get_meta(conn, "vipdoc_path")
            version = get_meta(conn, "scan_version")
        finally:
            conn.close()
    version_num = int(version) if version else 0
    return {
        "count": count,
        "scan_at": int(scan_at) if scan_at else None,
        "trade_date": int(trade_date) if trade_date else None,
        "vipdoc_path": vipdoc,
        "ready": count > 0,
        "scan_version": version_num,
        # 缓存是旧版本代码建的（或字段新增过），需要重建
        "stale": count > 0 and version_num != SCAN_VERSION,
    }


def load_all(db_file: str | Path) -> Iterable[sqlite3.Row]:
    conn = connect(db_file)
    try:
        for row in conn.execute("SELECT * FROM metrics"):
            yield row
    finally:
        conn.close()
