"""程序入口：启动本地服务、建立数据缓存、打开浏览器。

打包成 exe 后双击即运行（无控制台窗口），日志写在数据目录的 astock.log。
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from . import VERSION_LABEL, config, server
from .engine import store
from .memory import store as mem

HOST = "127.0.0.1"


def setup_logging() -> None:
    log_file = config.data_dir() / "astock.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")],
    )


def find_free_port(start: int, attempts: int = 20) -> int:
    """找一个真正空闲的端口。

    ⚠️ 不要设 SO_REUSEADDR：在 Windows 上它允许绑定已被占用的端口，
    会让这个检测形同虚设——结果两个程序抢同一端口，浏览器打开的可能
    是别人在做的东西。
    """
    for offset in range(attempts):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"端口 {start}~{start + attempts} 都被占用，无法启动。")


def verify_server(port: int, attempts: int = 12) -> bool:
    """自检：确认该端口上响应的确实是我们自己的服务。"""
    for _ in range(attempts):
        if already_running(port):
            return True
        time.sleep(0.25)
    return False


def already_running(port: int) -> bool:
    """检测该端口上是否已经跑着本程序。"""
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://{HOST}:{port}/api/config", timeout=1.5
        ) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and "config" in data
    except (urllib.error.URLError, ValueError, OSError):
        return False


def notify_error(message: str) -> None:
    """尽量用系统弹窗告知用户（打包后没有控制台）。"""
    print(f"[错误] {message}", file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, message, config.APP_TITLE, 0x10)
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    # PyInstaller 多进程（扫描时用进程池）必需
    multiprocessing.freeze_support()

    argv = list(sys.argv[1:] if argv is None else argv)
    no_browser = "--no-browser" in argv
    force_port = None
    for i, arg in enumerate(argv):
        if arg == "--port" and i + 1 < len(argv):
            try:
                force_port = int(argv[i + 1])
            except ValueError:
                pass

    setup_logging()
    log = logging.getLogger("main")

    # 老配置里的已下线模型名自动迁移（否则界面显示新模型、实际请求发旧名字）
    for note in config.migrate_settings():
        log.info("配置迁移：%s", note)
        print(f"  [配置迁移] {note}")

    cfg = config.load()

    # 通达信数据目录：没配或配的路径已失效时，自动找一遍
    vipdoc = config.get_vipdoc()
    if vipdoc:
        if not (cfg.get("vipdoc_path") or "").strip():
            log.info("自动探测到通达信数据目录：%s", vipdoc)
            print(f"  [数据目录] 自动探测到：{vipdoc}")
    else:
        log.warning("未找到通达信数据目录，需要用户在设置里指定")
        print("  [数据目录] 未找到通达信数据目录，请在「设置」里指定")
    port = force_port or int(cfg.get("port") or config.DEFAULT_PORT)

    # 已经有实例在跑就直接打开它，避免重复启动
    if already_running(port) and not force_port:
        url = f"http://{HOST}:{port}/"
        log.info("检测到已有实例，直接打开 %s", url)
        if not no_browser:
            webbrowser.open(url)
        return 0

    try:
        port = find_free_port(port)
    except RuntimeError as exc:
        notify_error(str(exc))
        return 1

    # 初始化数据库
    mem.init(config.db_path())
    conn = store.connect(config.db_path())
    try:
        store.init_db(conn)
    finally:
        conn.close()

    httpd = server.create_server(HOST, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    # 自检：确认这个端口上响应的确实是我们自己，而不是别的程序
    if not verify_server(port):
        log.error("端口 %s 上的响应不属于本程序，可能已被其他程序占用", port)
        notify_error(
            f"端口 {port} 似乎已被其他程序占用，界面可能无法正常打开。\n\n"
            f"请关闭占用该端口的程序后重试，或用 --port 指定其它端口。"
        )
        try:
            httpd.shutdown()
        except Exception:
            pass
        return 1

    url = f"http://{HOST}:{port}/"
    log.info("服务已启动：%s", url)
    print(f"\n  {config.APP_TITLE} {VERSION_LABEL} 已启动")
    print(f"  访问地址：{url}")
    print(f"  数据目录：{config.data_dir()}")
    print("  关闭本窗口即可退出程序\n")

    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    # 首次运行、以及缓存由旧版本程序建立时，自动重建行情缓存
    status = store.cache_status(config.db_path())
    if not status.get("ready"):
        log.info("未发现行情缓存，开始后台扫描…")
        server.start_scan(force=True)
    elif status.get("stale"):
        log.info("缓存版本过旧（v%s -> v%s），自动重建…", status.get("scan_version"), store.SCAN_VERSION)
        server.start_scan(force=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
