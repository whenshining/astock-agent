"""顶层入口：供 PyInstaller 打包使用（开发时也可直接 python astock.py）。"""

from __future__ import annotations

import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
