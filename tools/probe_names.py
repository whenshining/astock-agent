"""探测通达信股票名称数据源（TNF / DBF），找出最容易解析的那个。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

HQ = Path(r"D:\new_tdx\T0002\hq_cache")


def probe_dbf(path: Path) -> None:
    print(f"\n===== DBF: {path.name} ({path.stat().st_size} bytes) =====")
    with open(path, "rb") as f:
        head = f.read(32)
        version = head[0]
        n_records = struct.unpack("<I", head[4:8])[0]
        head_len = struct.unpack("<H", head[8:10])[0]
        rec_len = struct.unpack("<H", head[10:12])[0]
        print(f"版本={version:#x} 记录数={n_records} 头长={head_len} 记录长={rec_len}")
        if not (33 <= head_len <= 4096 and 1 <= rec_len <= 4096):
            print("  ！不符合 DBF 结构")
            return
        f.seek(32)
        n_fields = (head_len - 33) // 32
        fields = []
        for _ in range(n_fields):
            fd = f.read(32)
            name = fd[0:11].split(b"\x00")[0].decode("ascii", "ignore")
            ftype = chr(fd[11])
            flen = fd[16]
            fields.append((name, ftype, flen))
        print(f"字段({len(fields)}): {fields}")
        # 读前 3 条记录
        f.seek(head_len)
        for i in range(3):
            rec = f.read(rec_len)
            if len(rec) < rec_len:
                break
            vals = []
            off = 1  # 首字节是删除标记
            for name, ftype, flen in fields:
                raw = rec[off:off + flen]
                off += flen
                try:
                    text = raw.decode("gbk", "ignore").strip()
                except Exception:
                    text = raw.hex()
                vals.append(f"{name}={text}")
            print(f"  记录{i}: {' | '.join(vals)}")


def probe_tnf(path: Path, name_offset: int = 22, rec_size: int = 314, header: int = 50) -> None:
    print(f"\n===== TNF: {path.name} ({path.stat().st_size} bytes) =====")
    with open(path, "rb") as f:
        blob = f.read(200)
    print("前 64 字节:", blob[:64].hex(" "))
    print("前 64 字节(ascii):", "".join(chr(b) if 32 <= b < 127 else "." for b in blob[:64]))
    size = path.stat().st_size
    print(f"猜测: 头={header} 记录长={rec_size} -> 记录数={int((size - header) / rec_size)}")
    with open(path, "rb") as f:
        f.seek(header)
        for i in range(5):
            rec = f.read(rec_size)
            if len(rec) < rec_size:
                break
            code = rec[0:6].decode("gbk", "ignore").strip("\x00 ")
            name = rec[name_offset:name_offset + 8].decode("gbk", "ignore").strip("\x00 ")
            print(f"  code='{code}' name='{name}'  raw[6:24]={rec[6:24].hex(' ')}")
        # 扫描找已知代码
        f.seek(header)
        data = f.read()
    target = b"600519"
    idx = data.find(target)
    print(f"查找 600519: 偏移={idx}, 相对记录起始={idx - header if idx >= 0 else -1}, mod rec_size={(idx - header) % rec_size if idx >= 0 else -1}")
    if idx >= 0:
        rec_start = header + ((idx - header) // rec_size) * rec_size
        rec = data[rec_start:rec_start + rec_size]
        print(f"  该记录前 40 字节: {rec[:40].hex(' ')}")
        for off in range(0, 40):
            chunk = rec[off:off + 8]
            try:
                text = chunk.decode("gbk").strip("\x00 ")
            except Exception:
                continue
            if text and all("\u4e00" <= c <= "\u9fff" for c in text):
                print(f"  -> 中文名称可能位于偏移 {off}: '{text}'")


def main() -> None:
    for name in ("shm.tnf", "szm.tnf"):
        p = HQ / name
        if p.is_file():
            probe_tnf(p)
    for name in ("base.dbf",):
        p = HQ / name
        if p.is_file():
            probe_dbf(p)


if __name__ == "__main__":
    main()
