# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把整个程序打成单个 exe。

用法：python -m PyInstaller astock-agent.spec --noconfirm
产物：dist/astock-agent.exe
"""

import sys
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "astock.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 前端静态资源必须打进包里
        (str(ROOT / "app" / "web"), "app/web"),
    ],
    hiddenimports=[
        "app.engine.store",
        "app.engine.screener",
        "app.llm.tools",
        "app.memory.store",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只排除体型大且确定用不到的第三方库。
    # 注意：不要排除标准库（尤其 email —— urllib.request / http.client 依赖它），
    # 否则打包能过但运行时才崩。
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy", "PIL", "cv2", "onnxruntime",
        "comtypes", "uiautomation", "rapidocr_onnxruntime", "shapely", "pyclipper",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="astock-agent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 无控制台窗口，双击即用
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon=str(ROOT / "assets" / "icon.ico"),   # 有图标时取消注释
)
