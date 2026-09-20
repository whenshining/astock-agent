"""HTTP 服务：静态前端 + REST API + SSE 流式对话（纯标准库）。"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import VERSION_LABEL, __version__, agent, config
from .engine import screener, store
from .engine.tdx import has_vipdoc
from .memory import store as mem

log = logging.getLogger("http")

STATIC_DIR = Path(__file__).resolve().parent / "web"

# 扫描进度（前端轮询显示）
SCAN_STATE: dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "started_at": None,
    "result": None,
    "error": None,
}
_scan_lock = threading.Lock()


def _db() -> Path:
    return config.db_path()


# ------------------------------------------------------------------ 数据扫描

def start_scan(force: bool = True) -> dict[str, Any]:
    """在后台线程里扫描行情数据。"""
    with _scan_lock:
        if SCAN_STATE["running"]:
            return {"started": False, "reason": "已有扫描任务在进行中"}
        SCAN_STATE.update({
            "running": True, "done": 0, "total": 0,
            "started_at": time.time(), "result": None, "error": None,
        })

    def worker() -> None:
        def progress(done: int, total: int) -> None:
            SCAN_STATE["done"] = done
            SCAN_STATE["total"] = total

        try:
            result = store.scan_market(
                config.get_vipdoc(), _db(), progress=progress,
            )
            SCAN_STATE["result"] = result
            if not result.get("ok"):
                SCAN_STATE["error"] = result.get("error")
        except Exception as exc:
            SCAN_STATE["error"] = f"{exc}"
            traceback.print_exc()
        finally:
            SCAN_STATE["running"] = False
            SCAN_STATE["done"] = SCAN_STATE.get("total") or 0

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True}


def scan_status() -> dict[str, Any]:
    state = dict(SCAN_STATE)
    if state["running"] and state["started_at"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    # 一并回报数据目录是否有效，让前端能把「路径配错」这种问题说清楚
    vipdoc = config.get_vipdoc() or ""
    state["vipdoc_path"] = vipdoc
    state["vipdoc_ok"] = has_vipdoc(vipdoc)
    state["cache"] = store.cache_status(_db())
    return state


def usage_overview(refresh_balance: bool = False) -> dict[str, Any]:
    """用量与余额总览：本地记账 + 官方余额查询。"""
    from . import usage as usage_mod

    cfg = config.load()
    data: dict[str, Any] = {
        "summary": usage_mod.summary(_db()),
        "rate": float(cfg.get("usd_cny_rate") or 7.2),
    }
    key = cfg.get("deepseek_api_key") or ""
    if not key:
        data["balance"] = {"ok": False, "error": "未配置 API Key"}
        return data
    try:
        data["balance"] = usage_mod.fetch_balance(
            key,
            cfg.get("base_url") or "https://api.deepseek.com",
            use_cache=not refresh_balance,
        )
    except Exception as exc:
        data["balance"] = {"ok": False, "error": str(exc)}
    return data


def kb_overview() -> dict[str, Any]:
    """知识库概览：文档列表 + 统计。"""
    from .kb import store as kb_store

    return {
        "stats": kb_store.stats(_db()),
        "documents": kb_store.list_documents(_db()),
    }


def watchlist_overview(with_metrics: bool = True) -> dict[str, Any]:
    """自选股概览：只读通达信的「自选股」。"""
    from .engine import watchlist as wl

    info = wl.load(config.get_vipdoc())
    data: dict[str, Any] = {
        "available": info["available"],
        "count": info["count"],
        "days_ago": info.get("days_ago"),
        "stocks": [],
        "missing": [],
    }
    if not info["available"]:
        data["error"] = info.get("error")
        return data

    if with_metrics:
        for code in info["codes"]:
            detail = screener.stock_detail(_db(), code)
            if detail:
                data["stocks"].append(detail)
            else:
                data["missing"].append(code)
    else:
        data["codes"] = info["codes"]
    return data


def strategy_overview(query: str = "") -> dict[str, Any]:
    """策略列表 + 表单规格（规格由 PARAM_SPEC 生成，前端据此渲染表单）。"""
    from .strategies import BOARD_OPTIONS, PARAM_SPEC, SORT_OPTIONS, list_all, stats

    return {
        "strategies": list_all(_db(), query),
        "stats": stats(_db()),
        "spec": {
            "params": PARAM_SPEC,
            "sort_options": SORT_OPTIONS,
            "board_options": BOARD_OPTIONS,
        },
    }


# ------------------------------------------------------------------ 回测

BACKTEST_STATE: dict[str, Any] = {
    "running": False,
    "stage": "",
    "done": 0,
    "total": 0,
    "started_at": None,
    "result": None,
    "error": None,
    "kind": "",
}
_backtest_lock = threading.Lock()

# 记住每个策略最近一次的样本外验证 / 参数扫描结果。
# 可信度评估要用：没做过样本外验证的结果，最高只能给「中等」。
BACKTEST_MEMORY: dict[str, dict[str, Any]] = {}

RANGE_PRESETS = {"3m": 63, "6m": 122, "1y": 244, "2y": 488, "3y": 732}


def resolve_range(preset: str, start: str = "", end: str = "") -> tuple[int, int]:
    """把「最近 N 个月」这类选择换算成实际的起止交易日。"""
    from .backtest import data as bt_data

    if start and end:
        try:
            return int(start), int(end)
        except ValueError:
            pass
    calendar = bt_data.trading_dates(config.get_vipdoc(), 19900101, 20991231)
    if not calendar:
        return 0, 0
    days = RANGE_PRESETS.get(str(preset or "1y"), 244)
    return calendar[max(0, len(calendar) - days)], calendar[-1]


def start_backtest(payload: dict[str, Any], kind: str = "single") -> dict[str, Any]:
    """在后台线程里跑回测/参数扫描，前端轮询进度。"""
    with _backtest_lock:
        if BACKTEST_STATE["running"]:
            return {"started": False, "reason": "已有回测任务在跑"}
        BACKTEST_STATE.update({
            "running": True, "stage": "准备数据", "done": 0, "total": 0,
            "started_at": time.time(), "result": None, "error": None, "kind": kind,
            "strategy_id": payload.get("strategy_id"),
        })

    def worker() -> None:
        from .backtest import credibility, engine, data as bt_data
        from .strategies import normalize_plan

        try:
            vipdoc = config.get_vipdoc()
            capital = float(payload.get("capital") or 100000)
            start_date, end_date = resolve_range(
                payload.get("range") or "1y",
                str(payload.get("start") or ""),
                str(payload.get("end") or ""),
            )
            if not start_date or not end_date:
                raise RuntimeError("没有读到交易日历，请确认通达信日线数据可用")

            params = dict(payload.get("params") or {})
            plan = normalize_plan(payload.get("plan"))
            # 可信度评估要按策略归集「有没有做过样本外验证」
            strategy_key = str(payload.get("strategy_id") or "")

            # 「只看我的自选股」要在回测里也生效，否则跑的是全市场，
            # 跟界面上显示的条件对不上。注意自选股是"现在"的名单，
            # 拿它交易历史等于事后选股，结果要带警告。
            universe_warning = ""
            if params.get("watchlist"):
                from .engine import watchlist as wl_mod

                params, wl_count = wl_mod.apply_watchlist_filter(params, config.get_vipdoc())
                if wl_count:
                    universe_warning = (
                        f"本次只在你的 {wl_count} 只自选股范围内回测。"
                        "自选股是【现在】的名单，用它去交易过去等于事后选股（look-ahead bias），"
                        "结果偏乐观，只能参考。"
                    )

            def on_progress(done: int, total: int) -> None:
                BACKTEST_STATE["done"] = done
                if total:
                    BACKTEST_STATE["total"] = total
                    BACKTEST_STATE["stage"] = "模拟交易"
                else:
                    BACKTEST_STATE["stage"] = "扫描全市场买入信号"

            if kind == "sweep":
                # 扫描内部会先采集信号（慢），再逐组模拟（快）
                outcome = engine.sweep(
                    vipdoc, params, plan, start_date, end_date,
                    payload.get("grid") or {}, capital=capital, progress=on_progress,
                )
                outcome["start_date"] = start_date
                outcome["end_date"] = end_date
                BACKTEST_STATE["result"] = outcome
                # 记下来：下次回测这条策略时，可信度评估会把它算进去
                _remember(strategy_key, sweep=outcome)
            elif kind == "walkforward":
                # 样本外滚动验证：判断策略是真有效，还是调参调出来的
                outcome = engine.walk_forward(
                    vipdoc, params, plan, start_date, end_date,
                    payload.get("grid") or {}, folds=int(payload.get("folds") or 3),
                    capital=capital, progress=on_progress,
                )
                outcome["start_date"] = start_date
                outcome["end_date"] = end_date
                outcome["benchmark"] = engine.benchmark(vipdoc, start_date, end_date)
                BACKTEST_STATE["result"] = outcome
                # 记下来：下次回测这条策略时，可信度评估会把它算进去
                _remember(strategy_key, validated=outcome)
            else:
                signals, calendar, scanned = engine.collect_signals(
                    vipdoc, params, start_date, end_date, progress=on_progress
                )
                BACKTEST_STATE["stage"] = "模拟交易"
                BACKTEST_STATE["total"] = len(calendar)
                BACKTEST_STATE["done"] = 0
                result = engine.simulate(
                    signals, calendar, bt_data.BarStore(vipdoc, start_date, end_date),
                    engine.TradePlan.from_dict(plan), capital=capital,
                )
                result["scanned"] = scanned
                result["signal_days"] = len(signals)
                result["start_date"] = start_date
                result["end_date"] = end_date
                result["capital"] = capital
                # 基准与分年度：没有对照的收益率没有意义，必须一起给
                result["benchmark"] = engine.benchmark(vipdoc, start_date, end_date)
                result["yearly"] = engine.yearly_breakdown(result["equity_curve"], capital)
                # 一笔都没成交时补上原因（这里直接调的 simulate，别漏了）
                engine.explain_zero_trades(result, signals)
                # 可信度评估：把「这个结果值不值得信」逐条说清楚。
                # 关键是带上这个策略之前做过的样本外验证/参数扫描——
                # 没有它们，再漂亮的结果也只能给「中等」。
                memory = BACKTEST_MEMORY.get(str(payload.get("strategy_id") or ""), {})
                result["credibility"] = credibility.assess(
                    result,
                    validated=memory.get("validated"),
                    sweep=memory.get("sweep"),
                )
                BACKTEST_STATE["result"] = result

            # 把并行状态带出去：跑得慢时能一眼看出是不是退回了单进程
            if isinstance(BACKTEST_STATE.get("result"), dict):
                BACKTEST_STATE["result"]["parallel"] = dict(engine.PARALLEL_STATUS)
                if universe_warning:
                    BACKTEST_STATE["result"]["universe_warning"] = universe_warning
        except Exception as exc:
            log.exception("回测失败")
            BACKTEST_STATE["error"] = str(exc)
        finally:
            BACKTEST_STATE["running"] = False
            BACKTEST_STATE["stage"] = ""

    threading.Thread(target=worker, daemon=True).start()
    return {"started": True}


def backtest_status() -> dict[str, Any]:
    state = dict(BACKTEST_STATE)
    if state["running"] and state["started_at"]:
        state["elapsed"] = round(time.time() - state["started_at"], 1)
    return state


def _remember(strategy_key: str, *, validated: dict | None = None,
              sweep: dict | None = None) -> None:
    """记住某个策略最近一次的样本外验证 / 参数扫描结果。

    可信度评估要用它判断「这条策略到底验没验证过」——
    没做过样本外验证的结果，最高只能给「中等」。
    """
    if not strategy_key:
        return
    slot = BACKTEST_MEMORY.setdefault(strategy_key, {})
    if validated is not None:
        slot["validated"] = validated
    if sweep is not None:
        slot["sweep"] = sweep


# ------------------------------------------------------------------ 请求处理

class Handler(BaseHTTPRequestHandler):
    server_version = f"astock-agent/{__version__}"
    protocol_version = "HTTP/1.1"

    # -------------------------------------------------- 基础工具

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/static") or self.path in ("/", "/index.html"):
            return
        print(f"[http] {self.address_string()} {fmt % args}")

    def _read_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    # -------------------------------------------------- 路由

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            else:
                self._serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            # 记进日志文件：打包后的 exe 没有控制台，print_exc 会丢掉，
            # 接口报 500 时就没法查原因了
            log.exception("处理 %s %s 出错", method, path)
            traceback.print_exc()
            try:
                self._send_error_json(f"服务器内部错误：{exc}", 500)
            except Exception:
                pass

    # -------------------------------------------------- API

    def _handle_api(self, method: str, path: str, query: dict[str, str]) -> None:
        # ---- 配置
        if path == "/api/config" and method == "GET":
            self._send_json({"ok": True, "config": config.public_config()})
            return

        if path == "/api/config" and method == "POST":
            body = self._read_body()
            updates: dict[str, Any] = {}
            for key in ("vipdoc_path", "base_url", "model", "port", "memory_enabled",
                        "temperature", "thinking", "thinking_effort"):
                if key in body:
                    updates[key] = body[key]
            # 空字符串表示"不要改动"，避免前端提交打码值把 Key 覆盖掉
            api_key = body.get("deepseek_api_key")
            if isinstance(api_key, str) and api_key.strip() and "***" not in api_key:
                updates["deepseek_api_key"] = api_key.strip()
            if "port" in updates:
                try:
                    updates["port"] = int(updates["port"])
                except (TypeError, ValueError):
                    updates.pop("port")
            config.save(updates)
            self._send_json({"ok": True, "config": config.public_config()})
            return

        if path == "/api/config/verify" and method == "POST":
            try:
                client = agent.build_client()
                result = client.verify()
                self._send_json({"ok": True, **result})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)})
            return

        # ---- 会话
        if path == "/api/sessions" and method == "GET":
            self._send_json({"ok": True, "sessions": mem.list_sessions(_db())})
            return

        if path == "/api/sessions" and method == "POST":
            body = self._read_body()
            session_id = mem.create_session(_db(), body.get("title") or "新对话")
            self._send_json({"ok": True, "session_id": session_id})
            return

        if path.startswith("/api/sessions/"):
            parts = path.strip("/").split("/")
            # /api/sessions/<id>[...]
            if len(parts) >= 3:
                session_id = parts[2]
                rest = parts[3] if len(parts) > 3 else ""
                if rest == "messages" and method == "GET":
                    messages = mem.get_messages(_db(), session_id)
                    self._send_json({
                        "ok": True,
                        "session": mem.get_session(_db(), session_id),
                        "messages": messages,
                    })
                    return
                if rest == "clear" and method == "POST":
                    mem.clear_messages(_db(), session_id)
                    self._send_json({"ok": True})
                    return
                if not rest and method == "DELETE":
                    mem.delete_session(_db(), session_id)
                    self._send_json({"ok": True})
                    return
                if not rest and method == "POST":
                    body = self._read_body()
                    if body.get("title"):
                        mem.rename_session(_db(), session_id, body["title"])
                    self._send_json({"ok": True})
                    return

        # ---- 聊天（SSE 流式）
        if path == "/api/chat" and method == "POST":
            body = self._read_body()
            session_id = body.get("session_id") or ""
            message = body.get("message") or ""
            if not session_id:
                self._send_error_json("缺少 session_id")
                return
            if not mem.get_session(_db(), session_id):
                self._send_error_json("会话不存在", 404)
                return
            self._stream_chat(session_id, message)
            return

        # ---- 数据缓存
        if path == "/api/cache/status" and method == "GET":
            self._send_json({"ok": True, **scan_status()})
            return

        if path == "/api/usage" and method == "GET":
            refresh = str(query.get("refresh", "")).lower() in ("1", "true", "yes")
            self._send_json({"ok": True, **usage_overview(refresh_balance=refresh)})
            return

        if path == "/api/cache/refresh" and method == "POST":
            body = self._read_body()
            result = start_scan(force=body.get("force", True))
            self._send_json({"ok": True, **result})
            return

        # ---- 直接筛选（不经过大模型，供快捷筛选面板使用）
        if path == "/api/screen" and method == "POST":
            body = self._read_body()
            if not store.cache_status(_db()).get("ready"):
                self._send_error_json("本地行情缓存尚未建立，请先刷新数据。")
                return
            result = screener.screen(_db(), body)
            self._send_json({"ok": True, **result})
            return

        if path == "/api/overview" and method == "GET":
            self._send_json({"ok": True, **screener.stats(_db())})
            return

        if path == "/api/shutdown" and method == "POST":
            self._send_json({"ok": True, "message": "程序即将退出"})
            threading.Thread(target=self._shutdown_later, daemon=True).start()
            return

        if path == "/api/kline" and method == "GET":
            code = (query.get("code") or "").strip()[-6:].zfill(6)
            try:
                days = max(5, min(250, int(query.get("days") or 120)))
            except ValueError:
                days = 120
            market = "sh" if code.startswith(("6", "9")) else "sz"
            from .engine.tdx import read_day

            path_day = Path(config.get_vipdoc()) / market / "lday" / f"{market}{code}.day"
            bars = read_day(path_day, max_records=days)
            self._send_json({
                "ok": True,
                "code": code,
                "bars": [
                    {
                        "date": str(b.date), "open": b.open, "high": b.high,
                        "low": b.low, "close": b.close, "volume": b.volume,
                        "amount": round(b.amount, 2),
                    }
                    for b in bars
                ],
            })
            return

        # ---- 长期记忆
        if path == "/api/memories" and method == "GET":
            self._send_json({"ok": True, "memories": mem.list_memories(_db())})
            return

        if path == "/api/memories" and method == "POST":
            body = self._read_body()
            created = mem.add_memory(
                _db(),
                body.get("content") or "",
                kind=body.get("kind") or "note",
                source="manual",
            )
            self._send_json({"ok": True, "created": created, "memories": mem.list_memories(_db())})
            return

        if path.startswith("/api/memories/") and method == "DELETE":
            try:
                memory_id = int(path.strip("/").split("/")[2])
            except (IndexError, ValueError):
                self._send_error_json("记忆 ID 无效")
                return
            mem.delete_memory(_db(), memory_id)
            self._send_json({"ok": True, "memories": mem.list_memories(_db())})
            return

        # ---- 知识库
        if path == "/api/kb" and method == "GET":
            self._send_json({"ok": True, **kb_overview()})
            return

        if path == "/api/kb/import" and method == "POST":
            self._handle_kb_import()
            return

        if path == "/api/kb/search" and method == "POST":
            from .kb import index as kb_index

            body = self._read_body()
            query = str(body.get("query") or "").strip()
            if not query:
                self._send_error_json("缺少检索关键词")
                return
            hits = kb_index.search(_db(), query, top_k=int(body.get("top_k") or 5))
            self._send_json({"ok": True, "query": query, "count": len(hits), "results": hits})
            return

        if path == "/api/kb/clear" and method == "POST":
            from .kb import store as kb_store

            removed = kb_store.clear_documents(_db())
            self._send_json({"ok": True, "removed": removed, **kb_overview()})
            return

        if path.startswith("/api/kb/") and method == "DELETE":
            from .kb import store as kb_store

            try:
                doc_id = int(path.strip("/").split("/")[2])
            except (IndexError, ValueError):
                self._send_error_json("文档 ID 无效")
                return
            if not kb_store.delete_document(_db(), doc_id):
                self._send_error_json("文档不存在", 404)
                return
            self._send_json({"ok": True, **kb_overview()})
            return

        # ---- 自选股（只读通达信 T0002\blocknew\ZXG.blk）
        if path == "/api/watchlist" and method == "GET":
            with_metrics = str(query.get("metrics", "1")).lower() not in ("0", "false", "no")
            self._send_json({"ok": True, **watchlist_overview(with_metrics)})
            return

        # ---- 自选策略
        if path == "/api/strategies" and method == "GET":
            self._send_json({"ok": True, **strategy_overview(query.get("q", ""))})
            return

        if path == "/api/strategies" and method == "POST":
            self._handle_strategy_save()
            return

        if path.startswith("/api/strategies/"):
            from .strategies import delete as delete_strategy
            from .strategies import find, mark_used

            parts = path.strip("/").split("/")
            if len(parts) >= 3:
                raw_id = parts[2]
                rest = parts[3] if len(parts) > 3 else ""

                if rest == "run" and method == "POST":
                    # 注意：screener 已在模块顶部导入，这里**不能**再写
                    # `from .engine import screener`——那会让 screener 变成
                    # _handle_api 整个函数的局部变量，导致同一函数里的
                    # /api/screen、/api/overview 等分支全部报 UnboundLocalError。
                    # （这个坑踩过两次了，改动这里时务必留意）
                    body = self._read_body()
                    strategy = find(_db(), raw_id)
                    if not strategy:
                        self._send_error_json("策略不存在", 404)
                        return
                    if not store.cache_status(_db()).get("ready"):
                        self._send_error_json("行情缓存尚未建立，请先点「刷新数据」")
                        return
                    params = dict(strategy["params"])
                    if body.get("limit"):
                        try:
                            params["limit"] = max(1, min(200, int(body["limit"])))
                        except (TypeError, ValueError):
                            pass
                    # 策略里可能勾了「只看自选股」：必须在这里也解析一次，
                    # 否则从面板「试跑」会漏掉这个限制（跑成全市场，结果完全不对）
                    if params.get("watchlist"):
                        from .engine import watchlist as wl_mod

                        params, _ = wl_mod.apply_watchlist_filter(params, config.get_vipdoc())
                    result = screener.screen(_db(), params)
                    mark_used(_db(), strategy["id"])
                    self._send_json({
                        "ok": True, "strategy": {"name": strategy["name"],
                                                 "summary": strategy["summary"]},
                        **result,
                    })
                    return

                if not rest and method == "POST":
                    # 更新（走同一套校验）
                    body = self._read_body()
                    try:
                        strategy_id = int(raw_id)
                    except ValueError:
                        self._send_error_json("策略 ID 无效")
                        return
                    self._handle_strategy_save(strategy_id=strategy_id, body=body)
                    return

                if not rest and method == "DELETE":
                    try:
                        strategy_id = int(raw_id)
                    except ValueError:
                        self._send_error_json("策略 ID 无效")
                        return
                    if not delete_strategy(_db(), strategy_id):
                        self._send_error_json("策略不存在", 404)
                        return
                    self._send_json({"ok": True, **strategy_overview()})
                    return

        # ---- 回测
        if path == "/api/backtest/run" and method == "POST":
            body = self._read_body()
            params = body.get("params")
            plan = body.get("plan")
            # 也支持直接指定策略 ID，参数/计划从库里取
            if body.get("strategy_id"):
                from .strategies import find as find_strategy

                found = find_strategy(_db(), body["strategy_id"])
                if not found:
                    self._send_error_json("策略不存在", 404)
                    return
                params = params or found["params"]
                plan = plan or found["plan"]
            if not params:
                self._send_error_json("缺少筛选条件")
                return
            body["params"] = params
            body["plan"] = plan
            self._send_json({"ok": True, **start_backtest(body, kind="single")})
            return

        if path == "/api/backtest/sweep" and method == "POST":
            body = self._read_body()
            if body.get("strategy_id"):
                from .strategies import find as find_strategy

                found = find_strategy(_db(), body["strategy_id"])
                if not found:
                    self._send_error_json("策略不存在", 404)
                    return
                body["params"] = body.get("params") or found["params"]
                body["plan"] = body.get("plan") or found["plan"]
            if not body.get("params"):
                self._send_error_json("缺少筛选条件")
                return
            if not body.get("grid"):
                self._send_error_json("缺少扫描参数网格")
                return
            self._send_json({"ok": True, **start_backtest(body, kind="sweep")})
            return

        if path == "/api/backtest/walkforward" and method == "POST":
            body = self._read_body()
            if body.get("strategy_id"):
                from .strategies import find as find_strategy

                found = find_strategy(_db(), body["strategy_id"])
                if not found:
                    self._send_error_json("策略不存在", 404)
                    return
                body["params"] = body.get("params") or found["params"]
                body["plan"] = body.get("plan") or found["plan"]
            if not body.get("params"):
                self._send_error_json("缺少筛选条件")
                return
            if not body.get("grid"):
                self._send_error_json("缺少扫描参数网格")
                return
            self._send_json({"ok": True, **start_backtest(body, kind="walkforward")})
            return

        if path == "/api/backtest/status" and method == "GET":
            self._send_json({"ok": True, **backtest_status()})
            return

        if path == "/api/backtest/meta" and method == "GET":
            from .backtest import expr as expr_mod
            from .strategies import EXIT_CONDITION_TYPES, PLAN_SPEC

            start_date, end_date = resolve_range("1y")
            self._send_json({
                "ok": True,
                "plan_spec": PLAN_SPEC,
                "exit_condition_types": EXIT_CONDITION_TYPES,
                # 自定义表达式：可用字段、合法名字（前端做即时校验）、示例
                "expr_fields": expr_mod.FIELD_HELP,
                "expr_field_names": sorted(expr_mod.FIELD_ALIASES.keys()),
                "expr_examples": expr_mod.EXAMPLES,
                "ranges": [{"value": k, "label": v} for k, v in (
                    ("3m", "近 3 个月"), ("6m", "近 6 个月"),
                    ("1y", "近 1 年"), ("2y", "近 2 年"), ("3y", "近 3 年"),
                )],
                "default_range": "1y",
                "default_span": {"start_date": start_date, "end_date": end_date},
                "costs": {
                    "commission": "0.025%（单笔最低 5 元）",
                    "stamp_tax": "0.05%（仅卖出）",
                },
            })
            return

        self._send_error_json(f"未知接口：{path}", 404)

    def _handle_strategy_save(self, strategy_id: int | None = None,
                              body: dict[str, Any] | None = None) -> None:
        """新建或更新策略。"""
        from .strategies import save as save_strategy

        body = body if body is not None else self._read_body()
        try:
            saved = save_strategy(
                _db(),
                str(body.get("name") or ""),
                str(body.get("description") or ""),
                body.get("params") or {},
                source="manual",
                strategy_id=strategy_id,
            )
        except ValueError as exc:
            self._send_error_json(str(exc))
            return
        self._send_json({"ok": True, "saved": saved, **strategy_overview()})

    def _handle_kb_import(self) -> None:
        """导入知识库文档。前端把文件读成 base64 发过来（.docx 是二进制）。"""
        import base64
        import binascii

        from .kb import store as kb_store
        from .kb.extract import ExtractError

        body = self._read_body()
        name = str(body.get("name") or "").strip()
        if not name:
            self._send_error_json("缺少文件名")
            return

        payload_b64 = body.get("content_b64")
        text = body.get("text")
        try:
            if payload_b64:
                raw = base64.b64decode(str(payload_b64), validate=False)
            elif text is not None:
                raw = str(text).encode("utf-8")
            else:
                self._send_error_json("缺少文件内容")
                return
        except (binascii.Error, ValueError):
            self._send_error_json("文件内容解码失败")
            return

        if len(raw) > 20 * 1024 * 1024:
            self._send_error_json("文件超过 20MB，请拆分后再导入")
            return

        try:
            imported = kb_store.import_document(_db(), name, raw)
        except ExtractError as exc:
            self._send_error_json(str(exc))
            return
        except Exception as exc:
            log.exception("导入知识库文档失败")
            self._send_error_json(f"导入失败：{exc}")
            return

        self._send_json({"ok": True, "imported": imported, **kb_overview()})

    # -------------------------------------------------- SSE

    def _stream_chat(self, session_id: str, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event: dict[str, Any]) -> None:
            payload = json.dumps(event, ensure_ascii=False, default=str)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            emit({"type": "start", "session_id": session_id})
            agent.run_turn(_db(), session_id, message, emit)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            log.exception("对话流处理出错")
            traceback.print_exc()
            try:
                emit({"type": "error", "message": f"服务器错误：{exc}"})
            except Exception:
                pass
        finally:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass

    # -------------------------------------------------- 关闭程序

    def _shutdown_later(self) -> None:
        """给响应一点时间发出去，然后结束进程。"""
        import os

        time.sleep(0.6)
        os._exit(0)

    # -------------------------------------------------- 静态资源

    def _serve_static(self, path: str) -> None:
        if path in ("/", "", "/index.html"):
            target = STATIC_DIR / "index.html"
        else:
            rel = path.lstrip("/")
            if rel.startswith("static/"):
                rel = rel[len("static/"):]
            target = (STATIC_DIR / rel).resolve()
            # 防目录穿越
            try:
                target.relative_to(STATIC_DIR.resolve())
            except ValueError:
                self._send_error_json("非法路径", 403)
                return

        if not target.is_file():
            self._send_error_json("页面不存在", 404)
            return

        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix in (".html", ".htm"):
            ctype = "text/html; charset=utf-8"
        elif target.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        ctype = ctype or "application/octet-stream"

        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class _AppServer(ThreadingHTTPServer):
    """本地服务。

    ⚠️ 必须关掉 allow_reuse_address：在 Windows 上它等价于 SO_REUSEADDR，
    会允许两个进程绑定同一个端口——后绑的那个能"启动成功"，但连接可能被
    先绑的程序接走。表现就是：程序看起来正常，浏览器打开的却是别人的页面，
    或者接口莫名其妙返回 404。
    """
    allow_reuse_address = False
    daemon_threads = True


def create_server(host: str = "127.0.0.1", port: int = 8760) -> ThreadingHTTPServer:
    return _AppServer((host, port), Handler)
