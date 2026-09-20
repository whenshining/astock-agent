"""播入几条示例策略，方便直接体验/测试「自选策略」功能。

用法：
    python tools/seed_strategies.py              # 写入示例策略
    python tools/seed_strategies.py --clear      # 清空全部策略
    python tools/seed_strategies.py --data-dir X # 指定数据目录（默认自动定位）

数据库定位顺序：--data-dir > 环境变量 ASTOCK_DATA_DIR > exe 的 dist\\data > 开发模式目录。
同名策略会被更新，重复运行不会产生重复项。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 示例策略：刻意覆盖不同的参数类型（数值 / 布尔 / 板块 / 排序 / 成交额单位）
SAMPLES: list[dict] = [
    {
        "name": "放量上涨",
        "description": "涨幅和量比同时放大，按量比排序。适合找当日资金明显进场的中小盘。",
        "params": {
            "min_price": 5, "max_price": 30,
            "chg_min": 2, "chg_max": 7,
            "vol_ratio_min": 2,
            "amount_min": 5,              # 单位是亿元，程序内部会换算成元
            "sort_by": "vol_ratio", "limit": 20,
        },
    },
    {
        "name": "均线多头",
        "description": "均线多头排列并且创 60 日新高，趋势最强的一类。",
        "params": {
            "ma_bull": True, "new_high60": True,
            "chg_min": 0, "amount_min": 3,
            "sort_by": "chg", "limit": 20,
        },
    },
    {
        "name": "低吸企稳",
        "description": "当日下跌但近 20 日波动很小，属于回踩不破的标的。",
        "params": {
            "min_price": 5, "max_price": 40,
            "chg_max": 0, "chg_min": -4,
            "amp20_max": 4,
            "amount_min": 2,
            "sort_by": "amount", "limit": 20,
        },
    },
    {
        "name": "连涨强势",
        "description": "连涨 3 天以上且均线多头，只看创业板和深市主板。",
        "params": {
            "consec_up_min": 3, "chg_min": 0,
            "ma_bull": True,
            "boards": ["chinext", "sz_main"],
            "amount_min": 2,
            "sort_by": "consec_up", "limit": 20,
        },
    },
    {
        "name": "创20日新高",
        "description": "创 20 日新高、成交额 3 亿以上，主板为主。",
        "params": {
            "new_high20": True,
            "amount_min": 3, "amplitude_max": 10,
            "boards": ["sh_main", "sz_main"],
            "sort_by": "amount", "limit": 20,
        },
    },
]


def locate_db(override: str | None) -> Path:
    """找出应该写入哪个数据库。"""
    if override:
        candidate = Path(override)
        return (candidate / "astock.db") if candidate.is_dir() else candidate

    import os

    env_dir = os.environ.get("ASTOCK_DATA_DIR")
    if env_dir:
        return Path(env_dir) / "astock.db"

    exe_db = ROOT / "dist" / "data" / "astock.db"
    if exe_db.is_file():
        return exe_db

    from app import config

    return config.db_path()


def main() -> int:
    parser = argparse.ArgumentParser(description="播入示例策略")
    parser.add_argument("--clear", action="store_true", help="清空全部策略而不是写入")
    parser.add_argument("--data-dir", help="指定数据目录")
    args = parser.parse_args()

    db = locate_db(args.data_dir)
    print(f"目标数据库：{db}")
    if not db.is_file():
        print("数据库还不存在——请先启动一次程序（双击 exe 或运行 dev.cmd），再执行本脚本。")
        return 1

    from app import strategies

    if args.clear:
        removed = 0
        for item in strategies.list_all(db):
            if strategies.delete(db, item["id"]):
                removed += 1
        print(f"已清空 {removed} 个策略。")
        return 0

    print()
    for sample in SAMPLES:
        saved = strategies.save(
            db, sample["name"], sample["description"], sample["params"], source="sample"
        )
        verb = "已更新" if saved.get("replaced") else "已创建"
        print(f"  {verb}「{saved['name']}」")
        print(f"      {saved['summary']}")

    stats = strategies.stats(db)
    print(f"\n完成：当前共有 {stats['count']} 个策略。")
    print("打开程序后点左下角「⚡ 自选策略」即可看到，也可以直接在对话里说")
    print("「用我的放量上涨策略选股」。")
    print("\n（不想要了：python tools/seed_strategies.py --clear）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
