"""配置管理 —— 路径、DeepSeek 接入参数。

配置优先级：环境变量 > 数据目录下的 config.json > 内置默认值。
数据目录（会话/记忆/指标缓存）默认放在 %LOCALAPPDATA%\\astock-agent，
打包成 exe 后依然如此，避免写进只读的程序目录。
"""

from __future__ import annotations

import json
import os
import string
import sys
import threading
from pathlib import Path
from typing import Any

from . import VERSION_LABEL, __version__

APP_NAME = "astock-agent"
APP_TITLE = "A股智能选股助手"

# 通达信数据目录：**不写死**。留空表示自动探测，
# 因为每个人的通达信可能装在 C/D/E 盘或任意目录下（见 detect_vipdoc）。
DEFAULT_VIPDOC = ""
DEFAULT_BASE_URL = "https://api.deepseek.com"

# ---------------------------------------------------------------- 模型
# 本段同步自官方 2026-09-10 的更新（api-docs.deepseek.com/updates 与 /quick_start/pricing）
#
#   deepseek-flash    → DeepSeek-V4.1-Flash（2026-09-10 发布）
#                       新架构、原生视觉、1M 上下文；上一代 V4-Flash 已退役
#   deepseek-v4-pro   → DeepSeek-V4-Pro-0813（GA 正式版）
#                       官方已确认 2026-09-14 之后**继续提供**，计费方式不变
#                       （此前公告说要下线，后来改口了，不要再写成"将被路由到 Flash"）
#
# 已退役、服务端仍临时接受的名字（本地也要迁移，否则界面显示新模型、请求发旧名字）：
#   deepseek-chat / deepseek-reasoner                  → 2026-07-24 下线
#   deepseek-v4-flash / deepseek-v4-flash-vision-exp   → 2026-09-10 退役，转发到 V4.1-Flash
DEFAULT_MODEL = "deepseek-flash"
MODEL_CHOICES: list[tuple[str, str]] = [
    ("deepseek-flash", "deepseek-flash · V4.1-Flash（推荐）"),
    ("deepseek-v4-pro", "deepseek-v4-pro · V4-Pro-0813"),
]

# 界面展示用的模型详情。两个模型都是 1M 上下文 / 最大输出 384K，
# 都能开关思考模式；差别在视觉能力、Agent 能力和价格。
MODEL_DETAILS: dict[str, dict] = {
    "deepseek-flash": {
        "version": "DeepSeek-V4.1-Flash",
        "context": "1M",
        "max_output": "384K",
        "vision": True,
        "note": "官方最新架构，快、便宜，原生支持图片理解；日常选股与策略分析足够。",
    },
    "deepseek-v4-pro": {
        "version": "DeepSeek-V4-Pro-0813",
        "context": "1M",
        "max_output": "384K",
        "vision": False,
        "note": "GA 正式版，Agent 与复杂推理更强，价格约为 Flash 的 4 倍；不支持图片。",
    },
}

# 思考强度：low / high / max，官方默认 high。
# 简单问题用 low 更快更省，复杂分析用 max 更稳。
THINKING_EFFORTS: list[tuple[str, str]] = [
    ("low", "低 —— 快、省 token"),
    ("high", "高 —— 默认"),
    ("max", "最高 —— 复杂分析"),
]
DEFAULT_THINKING_EFFORT = "high"

# 已下线/已废弃的模型名 -> 当前替代。
# 老配置里存着这些名字时必须迁移：否则界面显示新模型、实际请求却发旧名字。
RETIRED_MODELS: dict[str, str] = {
    "deepseek-chat": DEFAULT_MODEL,
    "deepseek-reasoner": DEFAULT_MODEL,
    "deepseek-v4-flash": DEFAULT_MODEL,
    "deepseek-v4-flash-vision-exp": DEFAULT_MODEL,
}
DEFAULT_PORT = 8760

# 环境变量覆盖键
_ENV_KEYS = {
    "vipdoc_path": "ASTOCK_VIPDOC",
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "base_url": "ASTOCK_BASE_URL",
    "model": "ASTOCK_MODEL",
    "port": "ASTOCK_PORT",
}

_lock = threading.Lock()


def app_root() -> Path:
    """程序根目录。打包成 exe 后是 exe 所在目录，开发时是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """只读资源根目录（打包后为 PyInstaller 解包目录 _MEIPASS）。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent


_data_dir_cache: Path | None = None
_data_dir_cache_lock = threading.Lock()


def _resolve_data_dir() -> Path:
    candidates: list[Path] = []
    override = os.environ.get("ASTOCK_DATA_DIR")
    if override:
        candidates.append(Path(override))
    else:
        candidates.append(app_root() / "data")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / APP_NAME)

    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            # 探测文件名带进程号：并发线程各用各的，避免互相删掉对方的探测文件
            probe = path / f".write-test-{os.getpid()}"
            probe.write_text("ok", encoding="utf-8")
            try:
                probe.unlink()
            except OSError:
                pass
            return path
        except OSError:
            continue

    import tempfile

    fallback = Path(tempfile.gettempdir()) / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def data_dir() -> Path:
    """可写数据目录（配置、数据库、缓存）。

    优先放在程序旁边（exe 同级 `data\\`，开发时是项目根 `data\\`），
    这样程序是「绿色便携」的：连文件夹一起拷走，对话历史和长期记忆都会跟着走；
    想重置就删掉这个 data 目录。

    依次尝试：环境变量指定 -> 程序目录\\data -> %LOCALAPPDATA%\\astock-agent -> 系统临时目录。
    后两级是兜底，保证程序被放在只读位置（如 Program Files）时依然能正常工作。

    ⚠️ 结果在进程内缓存，只解析一次。这个函数会被每个 HTTP 请求调用，
    而早期版本每次都要写一个探测文件来测可写性——并发请求下多个线程会互相
    删掉对方的探测文件，把「文件已被别人删掉」误判成「目录不可写」，
    导致同一个进程里 data_dir() 返回不同的目录（表现为接口 500、
    或数据莫名其妙写到了别的盘）。
    """
    global _data_dir_cache
    if _data_dir_cache is None:
        with _data_dir_cache_lock:
            if _data_dir_cache is None:
                _data_dir_cache = _resolve_data_dir()
    return _data_dir_cache


def reset_data_dir_cache() -> None:
    """清空缓存（仅测试用，例如切换 ASTOCK_DATA_DIR 之后）。"""
    global _data_dir_cache
    with _data_dir_cache_lock:
        _data_dir_cache = None


def db_path() -> Path:
    return data_dir() / "astock.db"


# ------------------------------------------------------------------ 通达信目录探测

# 通达信安装目录常见叫法（数据在其下的 vipdoc 子目录里）
_COMMON_TDX_DIRS = (
    "new_tdx", "tdx", "tdx64", "zd_zsone", "zszq", "htzq",
    "通达信", "财富通", "国泰君安", "招商证券",
)


def _looks_like_vipdoc(path: str | Path) -> bool:
    """判断某个目录是不是通达信的数据根（含 sh/sz 的日线目录）。"""
    root = Path(str(path))
    for market in ("sh", "sz"):
        if (root / market / "lday").is_dir():
            return True
    return False


def _fixed_drives() -> list[str]:
    """列出本机固定磁盘（跳过网络盘和光驱，避免枚举时卡住）。"""
    drives: list[str] = []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mask = kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if not (mask >> index) & 1:
                continue
            root = f"{letter}:\\"
            # DRIVE_FIXED == 3
            if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:
                drives.append(root)
    except Exception:
        drives = [f"{c}:\\" for c in string.ascii_uppercase if os.path.isdir(f"{c}:\\")]
    return drives


def detect_vipdoc() -> str:
    """在常见位置自动寻找通达信数据目录，找不到返回空字符串。

    先试常见的目录名，再扫一遍各磁盘的顶层目录（只下一层，很快）。
    目的是让程序换台机器也能直接用，不必手工配置路径。
    """
    drives = _fixed_drives()

    for drive in drives:
        for name in _COMMON_TDX_DIRS:
            candidate = Path(drive) / name / "vipdoc"
            if _looks_like_vipdoc(candidate):
                return str(candidate)

    for drive in drives:
        try:
            children = list(Path(drive).iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            candidate = child / "vipdoc"
            if _looks_like_vipdoc(candidate):
                return str(candidate)
    return ""


_vipdoc_cache: str | None = None


def get_vipdoc() -> str:
    """实际生效的通达信目录：配置优先，配置无效或为空时自动探测。"""
    global _vipdoc_cache
    if _vipdoc_cache is not None:
        return _vipdoc_cache

    configured = (load().get("vipdoc_path") or "").strip()
    if configured and _looks_like_vipdoc(configured):
        _vipdoc_cache = configured
    else:
        _vipdoc_cache = detect_vipdoc() or configured
    return _vipdoc_cache


def reset_vipdoc_cache() -> None:
    global _vipdoc_cache
    _vipdoc_cache = None


def config_path() -> Path:
    return data_dir() / "config.json"


DEFAULTS: dict[str, Any] = {
    "vipdoc_path": DEFAULT_VIPDOC,
    "deepseek_api_key": "",
    "base_url": DEFAULT_BASE_URL,
    "model": DEFAULT_MODEL,
    "port": DEFAULT_PORT,
    "temperature": 0.3,
    # 思考模式：官方默认开启。开启时按官方要求回传 reasoning_content，
    # 且 temperature 无效（此时不会发送该参数）。
    "thinking": True,
    # 思考强度 low / high / max（官方默认 high）。只在开启思考模式时发送。
    "thinking_effort": DEFAULT_THINKING_EFFORT,
    "max_tool_rounds": 6,
    "memory_enabled": True,
    # 仅用于把美元计价的费用折算成人民币展示（官方余额本身就是人民币）
    "usd_cny_rate": 7.2,
}


def load() -> dict[str, Any]:
    """读取完整配置（默认值 + config.json + 环境变量）。"""
    cfg = dict(DEFAULTS)
    path = config_path()
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
    for key, env in _ENV_KEYS.items():
        value = os.environ.get(env)
        if value:
            if key == "port":
                try:
                    cfg[key] = int(value)
                except ValueError:
                    pass
            else:
                cfg[key] = value
    # 已下线的模型名一律换掉（内存中生效，落盘由 migrate_settings 负责）
    if cfg.get("model") in RETIRED_MODELS:
        cfg["model"] = RETIRED_MODELS[cfg["model"]]
    return cfg


def migrate_settings() -> list[str]:
    """把配置文件里已下线的模型名换成当前模型并落盘。返回迁移说明。"""
    path = config_path()
    if not path.is_file():
        return []
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(saved, dict):
        return []

    old = saved.get("model")
    if old not in RETIRED_MODELS:
        return []
    new = RETIRED_MODELS[old]
    saved["model"] = new
    with _lock:
        try:
            path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return [f"模型 {old} 已下线，已自动切换为 {new}"]


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """合并写入 config.json，返回写入后的完整配置。"""
    with _lock:
        current = {}
        path = config_path()
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(current, dict):
                    current = {}
            except (OSError, ValueError):
                current = {}
        for key, value in updates.items():
            if key in DEFAULTS:
                current[key] = value
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
        # 路径可能改了，让下次重新解析
        reset_vipdoc_cache()
        return load()


def get(key: str) -> Any:
    return load().get(key, DEFAULTS.get(key))


def public_config() -> dict[str, Any]:
    """给前端的配置（API key 打码，只暴露是否已设置）。"""
    cfg = load()
    key = cfg.get("deepseek_api_key") or ""
    masked = ""
    if key:
        masked = key[:6] + "*" * max(0, len(key) - 10) + key[-4:] if len(key) > 12 else "*" * len(key)
    return {
        "version": __version__,
        "version_label": VERSION_LABEL,
        # 展示实际生效的路径（可能是自动探测出来的），而不是配置里那个空值
        "vipdoc_path": get_vipdoc(),
        "vipdoc_configured": (cfg.get("vipdoc_path") or "").strip(),
        "vipdoc_autodetected": not (cfg.get("vipdoc_path") or "").strip()
        and bool(get_vipdoc()),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "port": cfg.get("port"),
        "thinking": bool(cfg.get("thinking", True)),
        "thinking_effort": cfg.get("thinking_effort") or DEFAULT_THINKING_EFFORT,
        "memory_enabled": cfg.get("memory_enabled", True),
        "api_key_set": bool(key),
        "api_key_masked": masked,
        "data_dir": str(data_dir()),
        "model_choices": [{"value": v, "label": t} for v, t in MODEL_CHOICES],
        # 每个模型的版本 / 上下文 / 能力，供设置界面展示
        "model_details": {
            value: dict(detail, label=label)
            for value, label in MODEL_CHOICES
            for detail in [MODEL_DETAILS.get(value, {})]
        },
        "thinking_efforts": [{"value": v, "label": t} for v, t in THINKING_EFFORTS],
    }
