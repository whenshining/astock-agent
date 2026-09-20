"""从通达信本地缓存读取股票名称。

通达信把名称放在 `T0002/hq_cache/{shm,szm,bjm}.tnf`（沪/深/北），
二进制格式为：50 字节文件头 + 定长 314 字节记录，记录内：

    偏移 0..5    代码（6 字节 ASCII）
    偏移 23..30  名称（GBK，最长 4 个汉字，不足补 0）

名称是纯展示用的附加信息，取不到时程序照常工作（表格只显示代码）。
"""

from __future__ import annotations

import re
from pathlib import Path

TNF_HEADER = 50
TNF_RECORD = 314
NAME_OFFSET = 23
NAME_WIDTH = 8

_NAME_FILES = ("shm.tnf", "szm.tnf", "bjm.tnf")

_WS = re.compile(r"\s+")


def name_cache_dir(vipdoc: str | Path) -> Path | None:
    """由 vipdoc 路径推断通达信名称缓存目录。

    vipdoc 通常形如 `<通达信根目录>\\vipdoc`，名称缓存位于同级 `T0002\\hq_cache`。
    """
    root = Path(vipdoc)
    for candidate in (root.parent / "T0002" / "hq_cache", root / "T0002" / "hq_cache"):
        if candidate.is_dir():
            return candidate
    return None


def parse_tnf(path: str | Path) -> dict[str, str]:
    """解析单个 .tnf 文件，返回 {代码: 名称}。"""
    names: dict[str, str] = {}
    try:
        blob = Path(path).read_bytes()
    except OSError:
        return names

    count = (len(blob) - TNF_HEADER) // TNF_RECORD
    for index in range(count):
        start = TNF_HEADER + index * TNF_RECORD
        record = blob[start:start + TNF_RECORD]
        if len(record) < NAME_OFFSET + NAME_WIDTH:
            break
        code = record[0:6].decode("gbk", "ignore").strip("\x00 ")
        if not code.isdigit():
            continue
        raw_name = record[NAME_OFFSET:NAME_OFFSET + NAME_WIDTH]
        name = raw_name.split(b"\x00")[0].decode("gbk", "ignore")
        name = _WS.sub("", name)
        if name:
            names[code] = name
    return names


def load_names(vipdoc: str | Path) -> dict[str, str]:
    """加载沪/深/北全部股票名称。取不到就返回空字典。"""
    cache_dir = name_cache_dir(vipdoc)
    if cache_dir is None:
        return {}

    names: dict[str, str] = {}
    for filename in _NAME_FILES:
        path = cache_dir / filename
        if path.is_file():
            names.update(parse_tnf(path))
    return names
