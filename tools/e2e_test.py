"""端到端测试：用本地 Mock LLM 验证完整「对话选股」链路（无需真实 API Key）。

验证内容：
  1. 前端调用的 SSE 接口能否逐事件正确流出
  2. Agent 是否把自然语言转成工具调用并真正执行了选股
  3. 工具结果能否回填给模型、模型能否据此作答（多轮循环）
  4. 会话与消息是否落库
  5. 长期记忆能否自动提取并持久化

用法：python tools/e2e_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 环境必须在导入 app 之前设好（config 读环境变量） ----
TEST_DIR = ROOT / "_e2e"


def _free_port(start: int, tries: int = 30) -> int:
    """找一个真正空闲的端口。

    用固定端口的话，端口被别的进程（例如另一个实例）占用时，
    请求会打到别人身上，测试结果会变成一堆莫名其妙的失败。

    注意：**不能设 SO_REUSEADDR** —— 在 Windows 上它允许绑定已被占用的端口，
    会让这个检测失效（本测试就因此误选过别的项目正在用的 8800 端口）。
    """
    import socket

    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"找不到空闲端口（{start}~{start + tries}）")


MOCK_PORT = _free_port(8799)
APP_PORT = _free_port(MOCK_PORT + 1)

os.environ["ASTOCK_DATA_DIR"] = str(TEST_DIR)
os.environ["ASTOCK_WORKERS"] = "1"
os.environ["DEEPSEEK_API_KEY"] = "mock-key-for-e2e"
os.environ["ASTOCK_MODEL"] = "mock-model"
os.environ["ASTOCK_BASE_URL"] = f"http://127.0.0.1:{MOCK_PORT}"

from app import config, server  # noqa: E402
from app.engine import store as estore  # noqa: E402
from app.memory import store as mem  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


# ================================================================ Mock LLM

SCREEN_ARGS = {"chg_min": 2, "vol_ratio_min": 2, "sort_by": "vol_ratio", "limit": 5}


class MockLLM(BaseHTTPRequestHandler):
    """一个最小的 OpenAI 兼容服务，按脚本走：先要工具，再总结。"""

    protocol_version = "HTTP/1.1"
    calls: list[dict] = []

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        messages = payload.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        MockLLM.calls.append({
            "stream": bool(payload.get("stream")),
            "has_tools": bool(payload.get("tools")),
            "tool_names": [t.get("function", {}).get("name") for t in (payload.get("tools") or [])],
            "roles": [m.get("role") for m in messages],
            "system_head": system[:40],
            "system": system,
            "messages": messages,
            "model": payload.get("model"),
            "thinking": payload.get("thinking"),
            "has_temperature": "temperature" in payload,
        })

        if not payload.get("stream"):
            # 非流式：要么是 Key 验证，要么是记忆提取
            if "长期记忆" in system:
                body = {"memories": [{"kind": "preference", "content": "用户偏好量比大于 2 的放量股票"}]}
                return self._json({"choices": [{"message": {"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}}]})
            return self._json({"choices": [{"message": {"role": "assistant", "content": "pong"}}]})

        # 只看「最后一条用户消息之后」有没有工具结果——
        # 否则多轮对话里历史遗留的工具消息会让判断失真
        last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
        has_tool_result = any(m.get("role") == "tool" for m in messages[last_user + 1:])
        if has_tool_result:
            return self._stream_text(messages)

        # 按最后一条用户消息决定调哪个工具
        last_text = (messages[last_user].get("content") or "") if last_user >= 0 else ""
        # 注意：知识库那条**故意不检查工具是否在列表里** —— 真实场景中历史消息里
        # 留着工具名时模型可能照旧调用，正好用来验证服务端会明确拒绝。
        if "查知识库" in last_text:
            return self._stream_kb_call()
        if "存成策略" in last_text:
            return self._stream_strategy_call("save_strategy", {
                "name": "放量上涨",
                "description": "涨幅和量比同时放大，按量比排序",
                "params": {"chg_min": 2, "vol_ratio_min": 2, "ma_bull": True, "sort_by": "vol_ratio"},
            }, "call_save_1")
        if "用策略选股" in last_text:
            return self._stream_strategy_call("run_strategy", {"name": "放量上涨"}, "call_run_1")
        if "有哪些策略" in last_text:
            return self._stream_strategy_call("list_strategies", {}, "call_list_1")
        return self._stream_tool_call()

    def _stream_strategy_call(self, tool_name: str, args: dict, call_id: str) -> None:
        """模拟模型调用策略工具。"""
        raw = json.dumps(args, ensure_ascii=False)
        half = len(raw) // 2
        self._sse_start()
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": call_id, "type": "function",
             "function": {"name": tool_name, "arguments": raw[:half]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[half:]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._sse_write({"choices": [], "usage": {
            "prompt_tokens": 1400, "completion_tokens": 70, "total_tokens": 1470,
            "prompt_cache_hit_tokens": 1100, "prompt_cache_miss_tokens": 300}})
        self._sse_end()

    # ---------------- SSE 辅助

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_write(self, chunk: dict) -> None:
        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _sse_end(self) -> None:
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- 两种回复脚本

    def _stream_tool_call(self) -> None:
        raw = json.dumps(SCREEN_ARGS, ensure_ascii=False)
        half = len(raw) // 2
        self._sse_start()
        # 先发思维链（思考模式下模型会先输出 reasoning_content）
        self._sse_write({"choices": [{"index": 0, "delta": {
            "reasoning_content": "用户在找放量上涨的股票。需要调用 screen_stocks，参数设为涨幅≥2%、量比≥2，按量比排序。"},
            "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "好的，我先筛一遍。"}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_e2e_1", "type": "function", "function": {"name": "screen_stocks", "arguments": ""}}
        ]}, "finish_reason": None}]})
        # 参数故意分两片发，验证流式拼接是否正确
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[:half]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[half:]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        # 最后一个 chunk 带整次请求的 token 用量（与官方行为一致，且 choices 为空）
        self._sse_write({"choices": [], "usage": {
            "prompt_tokens": 1200, "completion_tokens": 80, "total_tokens": 1280,
            "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 300}})
        self._sse_end()

    def _stream_kb_call(self) -> None:
        """模拟模型先检索知识库。"""
        raw = json.dumps({"query": "量比 阈值 放量"}, ensure_ascii=False)
        self._sse_start()
        self._sse_write({"choices": [{"index": 0, "delta": {
            "reasoning_content": "这个问题涉及判读标准，必须先查知识库。"}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_kb_1", "type": "function",
             "function": {"name": "search_knowledge_base", "arguments": raw}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self._sse_write({"choices": [], "usage": {
            "prompt_tokens": 1500, "completion_tokens": 60, "total_tokens": 1560,
            "prompt_cache_hit_tokens": 1200, "prompt_cache_miss_tokens": 300}})
        self._sse_end()

    def _emit_text(self, text: str, prompt_tokens: int = 1600, completion_tokens: int = 100) -> None:
        """把一段文本按小块流式发出去（模拟真实分片）。"""
        self._sse_start()
        for i in range(0, len(text), 24):
            self._sse_write({"choices": [{"index": 0, "delta": {"content": text[i:i + 24]},
                                          "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse_write({"choices": [], "usage": {
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_cache_hit_tokens": int(prompt_tokens * 0.8),
            "prompt_cache_miss_tokens": prompt_tokens - int(prompt_tokens * 0.8)}})
        self._sse_end()

    def _stream_text(self, messages: list[dict]) -> None:
        tool_msg = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        info = {}
        try:
            info = json.loads((tool_msg or {}).get("content") or "{}")
        except ValueError:
            pass

        # 策略工具结果
        if "saved" in info:
            saved = info["saved"]
            verb = "已更新" if saved.get("replaced") else "已保存"
            return self._emit_text(
                f"{verb}策略「{saved.get('name')}」：{saved.get('summary', '')}")
        if "strategies" in info:
            items = info.get("strategies") or []
            if not items:
                return self._emit_text("你还没有保存任何策略。")
            return self._emit_text(
                f"你保存了 {len(items)} 个策略：" + "、".join(s["name"] for s in items))
        if "strategy" in info and "hit_count" in info:
            strategy = info.get("strategy") or {}
            stocks = info.get("stocks") or []
            first = stocks[0] if stocks else {}
            return self._emit_text(
                f"按你的策略「{strategy.get('name')}」（{strategy.get('summary', '')}）执行，"
                f"共命中 {info.get('hit_count')} 只。其中 {first.get('code', '—')} "
                f"收盘 {first.get('close', '—')} 元，量比 {first.get('vol_ratio', '—')}。")

        # 知识库检索结果：回复里必须带上出处
        if "results" in info:
            results = info.get("results") or []
            if results:
                lines = [f"知识库里查到 {len(results)} 段相关内容："]
                for item in results[:3]:
                    snippet = (item.get("text") or "")[:36].replace("\n", " ")
                    lines.append(f"- {snippet}…（来源：{item.get('citation')}）")
                text = "\n".join(lines)
            else:
                text = "知识库里没有找到相关记载，我不给建议。"
            self._sse_start()
            for i in range(0, len(text), 24):
                self._sse_write({"choices": [{"index": 0, "delta": {"content": text[i:i + 24]}, "finish_reason": None}]})
            self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            self._sse_write({"choices": [], "usage": {
                "prompt_tokens": 1800, "completion_tokens": 120, "total_tokens": 1920,
                "prompt_cache_hit_tokens": 1500, "prompt_cache_miss_tokens": 300}})
            self._sse_end()
            return

        stocks = info.get("stocks") or []
        first = stocks[0] if stocks else {}
        text = (
            f"筛选完成：命中 {info.get('hit_count', '?')} 只，按{info.get('sorted_by', '量比')}排序。\n"
            f"其中 {first.get('code', '—')} 收盘 {first.get('close', '—')} 元，"
            f"涨跌幅 {first.get('chg', '—')}%，量比 {first.get('vol_ratio', '—')}。\n"
            f"数据截至 {info.get('trade_date', '—')}。以上仅为技术面筛选结果，不构成投资建议。"
        )
        self._sse_start()
        step = 24
        for i in range(0, len(text), step):
            self._sse_write({"choices": [{"index": 0, "delta": {"content": text[i:i + step]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self._sse_write({"choices": [], "usage": {
            "prompt_tokens": 2600, "completion_tokens": 220, "total_tokens": 2820,
            "prompt_cache_hit_tokens": 2000, "prompt_cache_miss_tokens": 600}})
        self._sse_end()


# ================================================================ 测试主流程

def call(path: str, payload=None, method: str = "GET", timeout: int = 60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}{path}", data=data,
        headers={"Content-Type": "application/json; charset=utf-8"}, method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("error") or body
        except ValueError:
            detail = f"HTTP {exc.code}: {body[:200]}"
        raise RuntimeError(detail) from exc


def stream_chat(session_id: str, message: str) -> list[dict]:
    """读取 /api/chat 的 SSE 流，返回事件列表。"""
    body = json.dumps({"session_id": session_id, "message": message}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}/api/chat", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events: list[dict] = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                events.append(json.loads(payload))
            except ValueError:
                pass
    return events


def main() -> int:
    print("=" * 68)
    print("端到端测试：对话选股全链路（Mock LLM）")
    print("=" * 68)

    if TEST_DIR.exists():
        shutil.rmtree(TEST_DIR, ignore_errors=True)
    TEST_DIR.mkdir(parents=True, exist_ok=True)

    # ---------- 1. Mock LLM
    mock = ThreadingHTTPServer(("127.0.0.1", MOCK_PORT), MockLLM)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    print(f"\n[1] Mock LLM 已启动 :{MOCK_PORT}")

    # ---------- 2. 数据初始化 + 建缓存
    print("\n[2] 建立行情缓存")
    mem.init(config.db_path())
    conn = estore.connect(config.db_path())
    estore.init_db(conn)
    conn.close()
    result = estore.scan_market(config.get_vipdoc(), config.db_path(), workers=1)
    print(f"    扫描结果: {result}")
    check("行情缓存建立成功", bool(result.get("ok")), f"{result.get('scanned')} 只，截至 {result.get('trade_date')}")

    # ---------- 3. 启动应用服务
    httpd = server.create_server("127.0.0.1", APP_PORT)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"\n[3] 应用服务已启动 :{APP_PORT}")
    time.sleep(0.5)

    cfg = call("/api/config")["config"]
    check("服务可访问且配置正确", cfg["vipdoc_path"] == config.get_vipdoc(), f"model={cfg['model']}")

    # 验证 Key 通道
    verify = call("/api/config/verify", {}, "POST")
    check("API Key 校验通道可用", verify.get("ok") is True, verify.get("reply", ""))

    # ---------- 4. 走一轮完整对话
    print("\n[4] 发起一轮对话（应触发 screen_stocks 工具）")
    session_id = call("/api/sessions", {"title": "端到端测试"}, "POST")["session_id"]
    events = stream_chat(session_id, "帮我找今天放量上涨的股票，量比大于2，涨幅超过2%")

    kinds = [e["type"] for e in events]
    print(f"    收到 {len(events)} 个事件：{kinds}")

    check("收到 start 事件", "start" in kinds)
    check("收到文本增量（流式）", kinds.count("text") > 0, f"{kinds.count('text')} 个 delta")
    check("收到工具调用开始", "tool_start" in kinds)
    check("收到工具执行结果", "tool_result" in kinds)
    check("收到 done 事件", "done" in kinds)
    check("没有 error 事件", "error" not in kinds,
          str([e for e in events if e["type"] == "error"]))

    tool_start = next((e for e in events if e["type"] == "tool_start"), {})
    check("工具名正确", tool_start.get("name") == "screen_stocks", tool_start.get("name", ""))
    check("工具参数完整（流式分片拼接正确）",
          tool_start.get("args") == SCREEN_ARGS,
          f"{tool_start.get('args')} vs {SCREEN_ARGS}")

    tool_result = next((e for e in events if e["type"] == "tool_result"), {})
    check("工具执行成功", tool_result.get("ok") is True, tool_result.get("error") or "")
    check("结果含股票列表", bool(tool_result.get("stocks")), f"{len(tool_result.get('stocks') or [])} 只")
    check("命中数量符合预期", (tool_result.get("total") or 0) > 0, f"共命中 {tool_result.get('total')} 只")
    check("带回了筛选条件说明", bool(tool_result.get("conditions")),
          "；".join(tool_result.get("conditions") or []))
    first_stock = (tool_result.get("stocks") or [{}])[0]
    check("结果带回了股票名称（通达信 TNF 解析）",
          bool(first_stock.get("name")),
          f"{first_stock.get('code')} = {first_stock.get('name')}")

    # 文本内容应包含工具返回的真实数据（证明回填闭环）
    full_text = "".join(e.get("delta", "") for e in events if e["type"] == "text")
    first_code = (tool_result.get("stocks") or [{}])[0].get("code", "")
    check("模型回复引用了工具返回的真实数据",
          bool(first_code) and first_code in full_text,
          f"回复中出现 {first_code}")
    print("\n    ---- 助手回复 ----")
    for line in full_text.splitlines():
        print(f"    {line}")

    # ---------- 5. 落库检查
    print("\n[5] 检查会话与消息持久化")
    stored = call(f"/api/sessions/{session_id}/messages")
    roles = [m["role"] for m in stored["messages"]]
    print(f"    落库消息角色序列：{roles}")
    check("用户消息已落库", roles.count("user") == 1)
    check("助手消息已落库", roles.count("assistant") >= 1)
    check("工具消息已落库", roles.count("tool") == 1)
    tool_msg = next((m for m in stored["messages"] if m["role"] == "tool"), {})
    check("工具消息保留了结果摘要（前端可回显）",
          bool((tool_msg.get("meta") or {}).get("stocks")),
          (tool_msg.get("meta") or {}).get("summary", ""))
    check("思考内容已落库（供后续轮次回传）",
          any((m.get("reasoning") or "").strip() for m in stored["messages"] if m["role"] == "assistant"))

    sessions = call("/api/sessions")["sessions"]
    check("会话标题自动生成", any(s["id"] == session_id and s["title"] != "新对话" for s in sessions),
          next((s["title"] for s in sessions if s["id"] == session_id), ""))

    # ---------- 6. 长期记忆
    print("\n[6] 检查长期记忆自动提取")
    memories = []
    for _ in range(20):
        time.sleep(0.3)
        memories = call("/api/memories")["memories"]
        if memories:
            break
    check("记忆已自动提取并持久化", bool(memories),
          f"{len(memories)} 条：" + "；".join(m["content"] for m in memories))

    # 记忆是否注入了系统提示
    mem_calls = [c for c in MockLLM.calls if c["has_tools"]]
    check("Mock 收到的请求带 system 提示", all(c["system_head"] for c in mem_calls))
    check("工具调用共发生两轮（询问 + 总结）", len(mem_calls) == 2, f"{len(mem_calls)} 轮")

    # ---- 思考模式相关的官方要求 ----
    second_round = mem_calls[-1]["messages"]
    assistant_msgs = [m for m in second_round if m.get("role") == "assistant"]
    check("历史轮次的 reasoning_content 已回传（官方硬性要求）",
          any((m.get("reasoning_content") or "").strip() for m in assistant_msgs),
          f"第二轮带 {len(assistant_msgs)} 条 assistant 消息")
    check("使用当前在售模型（不是已下线的 deepseek-chat）",
          mem_calls[0]["model"] not in (None, "", "deepseek-chat", "deepseek-reasoner"),
          f"model={mem_calls[0]['model']}")
    check("已发送 thinking 开关字段",
          isinstance(mem_calls[0]["thinking"], dict),
          f"thinking={mem_calls[0]['thinking']}")
    check("思考模式下不发送无效的 temperature",
          mem_calls[0]["has_temperature"] is False)

    # ---------- 7. 用量与费用记账
    print("\n[7] 检查用量与费用记账")
    usage_events = [e for e in events if e["type"] == "usage"]
    check("对话过程中收到用量事件", len(usage_events) >= 1, f"{len(usage_events)} 次")
    turn_cost = sum((e["usage"].get("cost_usd") or 0) for e in usage_events)
    check("已按官方价目算出费用", turn_cost > 0, f"本轮 ${turn_cost:.6f}")
    check("token 数确实来自接口返回的 usage",
          any(e["usage"].get("cache_hit_tokens") == 900 for e in usage_events),
          f"各轮 (命中, 未命中) = {[(e['usage'].get('cache_hit_tokens'), e['usage'].get('cache_miss_tokens')) for e in usage_events]}")
    usage_api = call("/api/usage")
    check("用量已落库（今日汇总非空）", usage_api["summary"]["today"]["calls"] > 0,
          f"今日 {usage_api['summary']['today']['calls']} 次调用，${usage_api['summary']['today']['cost_usd']}")
    check("缓存命中率已统计", usage_api["summary"]["today"]["cache_hit_rate"] > 0,
          f"{usage_api['summary']['today']['cache_hit_rate']}%")
    check("按模型拆分可用", len(usage_api["summary"]["by_model"]) >= 1,
          str([m["model"] for m in usage_api["summary"]["by_model"]]))

    # ---------- 8. 知识库（RAG + 引用溯源）
    print("\n[8] 检查知识库")
    tool_calls = [c for c in MockLLM.calls if c["has_tools"]]

    # 8.1 知识库为空时，模型根本拿不到检索工具 —— 「没文档就不给建议」的硬保证
    check("知识库为空时不向模型提供检索工具",
          "search_knowledge_base" not in tool_calls[-1]["tool_names"],
          f"工具={tool_calls[-1]['tool_names']}")
    check("空知识库时系统提示明确禁止给建议",
          "没有导入任何文档" in tool_calls[-1]["system"] and "禁止" in tool_calls[-1]["system"])

    # 8.2 导入文档
    kb_doc = """# 量比使用说明

## 阈值参考
量比 1.5 到 2.5 之间属于温和放量，通常是健康的换手。
量比超过 5 一般是突发消息或资金异动，追高风险较大，需要谨慎。

## 风险提示
量比要结合股价位置判断，高位放量需要格外警惕出货。
"""
    imported = call("/api/kb/import", {"name": "量比规则.md", "text": kb_doc}, "POST")
    check("可以导入知识库文档",
          imported.get("ok") and imported["imported"]["chunks"] > 0,
          f"{imported['imported']['name']} → {imported['imported']['chunks']} 段")
    check("文档统计正确", imported["stats"]["documents"] == 1,
          f"{imported['stats']['documents']} 篇 / {imported['stats']['chunks']} 段")

    # 8.3 导入后，工具出现，且检索被真正执行
    kb_session = call("/api/sessions", {"title": "知识库测试"}, "POST")["session_id"]
    kb_events = stream_chat(kb_session, "查知识库：量比多少算温和放量？")
    kb_round = [c for c in MockLLM.calls if c["has_tools"]][-1]
    check("导入后把检索工具提供给模型",
          "search_knowledge_base" in kb_round["tool_names"],
          str(kb_round["tool_names"]))
    check("系统提示切换为「有知识库」规则",
          "用户导入了" in kb_round["system"] and "必须" in kb_round["system"])

    kb_tool = next((e for e in kb_events if e["type"] == "tool_result"), None)
    check("知识库检索被真正执行",
          kb_tool is not None and kb_tool.get("name") == "search_knowledge_base",
          (kb_tool or {}).get("name", ""))
    if kb_tool:
        check("检索命中内容", (kb_tool.get("total") or 0) > 0, f"{kb_tool.get('total')} 段")
        first = (kb_tool.get("knowledge") or [{}])[0]
        citation = first.get("citation") or ""
        check("命中结果带出处（文件名 · 章节 · 段号）",
              "量比规则.md" in citation and "第" in citation, citation)
        check("命中结果带原文供核对", bool(first.get("text")),
              (first.get("text") or "")[:36])

    kb_text = "".join(e.get("delta", "") for e in kb_events if e["type"] == "text")
    check("回复中标注了知识库来源", "来源：" in kb_text and "量比规则.md" in kb_text)

    # 8.4 无关问题不应命中（宁可不给，也不硬凑）
    miss = call("/api/kb/search", {"query": "如何用 Python 写爬虫抓数据"}, "POST")
    check("无关问题检索结果为空", miss["count"] == 0, f"命中 {miss['count']} 段")

    # 8.5 不支持的格式要有明确提示
    try:
        call("/api/kb/import", {"name": "研报.pdf", "text": "x"}, "POST")
        check("不支持的格式被拒绝", False, "竟然导入成功了")
    except RuntimeError as exc:
        check("不支持的格式被拒绝且说明原因", "不支持" in str(exc), str(exc)[:70])

    # 8.6 删除文档后，检索能力要一并收回
    doc_id = imported["imported"]["id"]
    call(f"/api/kb/{doc_id}", None, "DELETE")
    check("删除文档后知识库为空", call("/api/kb")["stats"]["documents"] == 0)
    stream_chat(kb_session, "再问一次：量比阈值是多少")
    check("删除后重新收回检索工具（能力不残留）",
          "search_knowledge_base" not in [c for c in MockLLM.calls if c["has_tools"]][-1]["tool_names"])

    # 8.7 历史消息里残留工具名、模型照旧调用时，服务端要明确拒绝
    stale_events = stream_chat(kb_session, "查知识库：量比阈值是多少")
    stale = next((e for e in stale_events
                  if e["type"] == "tool_result" and e.get("name") == "search_knowledge_base"), None)
    check("工具箱里没有的工具被明确拒绝（避免模型前后矛盾）",
          stale is not None and stale.get("ok") is False and "不可用" in (stale.get("error") or ""),
          (stale or {}).get("error", "")[:60])

    # ---------- 9. 自选策略
    print("\n[9] 检查自选策略")
    from app.llm import tools as tool_mod

    initial = call("/api/strategies")
    check("初始没有策略", initial["stats"]["count"] == 0)
    check("表单规格由后端下发（前端不写死字段）",
          len(initial["spec"]["params"]) >= 15,
          f"{len(initial['spec']['params'])} 个可配置条件")

    created = call("/api/strategies", {
        "name": "放量上涨",
        "description": "涨幅和量比同时放大",
        "params": {"chg_min": 2, "vol_ratio_min": 2, "ma_bull": True,
                   "amount_min": 5, "sort_by": "vol_ratio", "limit": 20},
    }, "POST")
    saved = created["saved"]
    check("可以新建策略", created["ok"] and saved["name"] == "放量上涨")
    check("条件被翻译成人话", "涨幅" in saved["summary"] and "量比" in saved["summary"],
          saved["summary"])
    check("成交额按亿元输入、内部换算成元",
          abs(saved["params"]["amount_min"] - 5e8) < 1,
          f"amount_min={saved['params']['amount_min']}（5 亿应为 500000000）")
    check("成交额展示正确（不是 50000 亿）", "5.00 亿元" in saved["summary"], saved["summary"])

    try:
        call("/api/strategies", {"name": "", "params": {"chg_min": 1}}, "POST")
        check("空名称被拒绝", False, "竟然通过了")
    except RuntimeError as exc:
        check("空名称被拒绝", "名称" in str(exc), str(exc)[:40])
    try:
        call("/api/strategies", {"name": "空条件", "params": {}}, "POST")
        check("空条件被拒绝", False, "竟然通过了")
    except RuntimeError as exc:
        check("空条件被拒绝", "条件" in str(exc), str(exc)[:40])

    garbage = call("/api/strategies", {
        "name": "脏数据测试", "params": {"chg_min": 1, "不存在的字段": 99, "max_price": ""},
    }, "POST")
    check("非法字段被丢弃",
          "不存在的字段" not in garbage["saved"]["params"] and "max_price" not in garbage["saved"]["params"],
          str(garbage["saved"]["params"]))

    call("/api/strategies", {"name": "低吸企稳", "params": {"chg_max": 0, "amp20_max": 4}}, "POST")
    # 查询参数里的中文必须 URL 编码（前端用 encodeURIComponent，这里同理）
    found = call("/api/strategies?q=" + urllib.parse.quote("低吸"))
    check("按名称搜索", [s["name"] for s in found["strategies"]] == ["低吸企稳"],
          str([s["name"] for s in found["strategies"]]))
    found2 = call("/api/strategies?q=" + urllib.parse.quote("量比"))
    check("按条件文字也能搜到", any(s["name"] == "放量上涨" for s in found2["strategies"]),
          str([s["name"] for s in found2["strategies"]]))

    target = next(s for s in call("/api/strategies")["strategies"] if s["name"] == "低吸企稳")
    edited = call(f"/api/strategies/{target['id']}", {
        "name": "低吸企稳", "description": "改过的描述",
        "params": {"chg_max": -1, "amp20_max": 3.5},
    }, "POST")
    check("可以编辑策略", edited["saved"]["description"] == "改过的描述", edited["saved"]["description"])
    check("编辑后条件已更新", abs(edited["saved"]["params"]["amp20_max"] - 3.5) < 1e-6)

    # 让助手保存策略
    strat_session = call("/api/sessions", {"title": "策略测试"}, "POST")["session_id"]
    save_events = stream_chat(strat_session, "把涨幅大于2%、量比大于2、均线多头 存成策略")
    save_tool = next((e for e in save_events if e["type"] == "tool_result"
                      and e.get("name") == "save_strategy"), None)
    check("助手能直接保存策略", save_tool is not None and save_tool.get("ok") is True,
          (save_tool or {}).get("error", ""))
    if save_tool:
        check("保存回执带回策略名与条件",
              save_tool["strategy"]["name"] == "放量上涨" and bool(save_tool["strategy"]["summary"]),
              save_tool["strategy"]["summary"])
    strat_round = [c for c in MockLLM.calls if c["has_tools"]][-1]
    check("三个策略工具都已提供给模型",
          {"list_strategies", "run_strategy", "save_strategy"} <= set(strat_round["tool_names"]),
          str(strat_round["tool_names"]))
    check("已保存的策略被注入系统提示", "放量上涨" in strat_round["system"])

    # 让助手执行策略
    run_events = stream_chat(strat_session, "用策略选股")
    run_tool = next((e for e in run_events if e["type"] == "tool_result"
                     and e.get("name") == "run_strategy"), None)
    check("助手能按名字执行策略", run_tool is not None and run_tool.get("ok") is True,
          (run_tool or {}).get("error", ""))
    if run_tool:
        check("执行的是保存下来的参数",
              run_tool["strategy"]["name"] == "放量上涨" and (run_tool.get("total") or 0) > 0,
              f"命中 {run_tool.get('total')} 只")
        check("执行结果带回股票列表", bool(run_tool.get("stocks")))

    list_events = stream_chat(strat_session, "我有哪些策略")
    list_tool = next((e for e in list_events if e["type"] == "tool_result"
                      and e.get("name") == "list_strategies"), None)
    check("助手能列出全部策略",
          list_tool is not None and len(list_tool.get("strategies") or []) == 3,
          f"{len(list_tool.get('strategies') or []) if list_tool else 0} 个")

    missing = tool_mod.execute("run_strategy", {"name": "根本不存在的策略"}, config.db_path())
    check("执行不存在的策略有明确提示",
          missing.get("ok") is False and "没有找到" in (missing.get("error") or ""),
          (missing.get("error") or "")[:50])

    del_target = next(s for s in call("/api/strategies")["strategies"] if s["name"] == "脏数据测试")
    deleted = call(f"/api/strategies/{del_target['id']}", None, "DELETE")
    check("可以删除策略", deleted["stats"]["count"] == 2, f"剩余 {deleted['stats']['count']} 个")

    # ---------- 10. 通达信自选股
    print("\n[10] 检查通达信自选股")
    from app.engine import watchlist as wl_mod

    overview = call("/api/watchlist?metrics=0")
    if not overview.get("available"):
        print(f"  （本机没读到自选股，跳过相关断言）{overview.get('error', '')}")
    else:
        check("能读到自选股", overview["count"] >= 1, f"{overview['count']} 只")
        check("附带数据新鲜度", overview.get("days_ago") is not None,
              f"文件 {overview.get('days_ago')} 天前更新")
        check("只返回自选股、不再有多板块概念",
              "groups" not in overview, "接口已简化为单一列表")

        detail = call("/api/watchlist")
        check("返回的股票带当前指标",
              bool(detail["stocks"]) and "close" in detail["stocks"][0],
              f"{len(detail['stocks'])} 只")
        check("板块内股票数量一致",
              detail["count"] == len(detail["stocks"]) + len(detail.get("missing") or []),
              f"count={detail['count']} 有效={len(detail['stocks'])} 缺数据={len(detail.get('missing') or [])}")

        in_watchlist = {s["code"] for s in detail["stocks"]}

        limited = tool_mod.execute(
            "screen_stocks",
            {"watchlist": True, "chg_min": -100, "limit": 100},
            config.db_path(),
        )
        got = {s["code"] for s in limited.get("stocks") or []}
        check("watchlist=true 时只返回自选股内的股票",
              bool(got) and got <= in_watchlist,
              f"返回 {len(got)} 只，越界={got - in_watchlist}")
        check("条件描述说明了自选股限制",
              any("自选股" in c for c in limited.get("conditions") or []),
              str(limited.get("conditions")))

        # 策略里勾选「只看我的自选股」
        strat = call("/api/strategies", {
            "name": "自选股放量", "description": "只在自己自选股里找放量的",
            "params": {"watchlist": True, "vol_ratio_min": 1, "sort_by": "vol_ratio"},
        }, "POST")
        check("策略可以勾选「只看自选股」",
              strat["saved"]["params"].get("watchlist") is True,
              strat["saved"]["summary"])
        run = call(f"/api/strategies/{strat['saved']['id']}/run", {}, "POST")
        run_codes = {s["code"] for s in run.get("stocks") or []}
        check("执行该策略时自选股限制同样生效",
              not run_codes or run_codes <= in_watchlist,
              f"命中 {run.get('total')} 只，越界={run_codes - in_watchlist}")

        tool_result = tool_mod.execute("get_watchlist", {"with_metrics": False}, config.db_path())
        check("get_watchlist 工具可用", tool_result.get("ok") is True,
              f"{tool_result.get('count')} 只")

        # 解析器只认自选股文件
        check("只读 ZXG.blk，忽略其他自定义板块",
              wl_mod.watchlist_path(config.get_vipdoc()).name == "ZXG.blk",
              str(wl_mod.watchlist_path(config.get_vipdoc())))

    # ---------- 11. HTTP 接口逐个探活
    # 这段用来防止「某个接口只在某个分支里被引用、从没被测过」导致的隐藏崩溃。
    # 曾经真实发生过：策略试跑分支里的局部 import 把 screener 变成函数局部变量，
    # 结果 /api/screen 和 /api/overview 全部 500，而测试没覆盖到，
    # 直到打包成 exe 手工验证时才发现。
    print("\n[11] HTTP 接口探活")
    endpoints = (
        ("/api/config", "GET", None),
        ("/api/sessions", "GET", None),
        ("/api/cache/status", "GET", None),
        ("/api/overview", "GET", None),
        ("/api/memories", "GET", None),
        ("/api/usage", "GET", None),
        ("/api/kb", "GET", None),
        ("/api/watchlist?metrics=0", "GET", None),
        ("/api/strategies", "GET", None),
        ("/api/screen", "POST", {"limit": 3, "chg_min": 2, "vol_ratio_min": 2}),
        ("/api/kb/search", "POST", {"query": "量比"}),
    )
    broken = []
    for path, method, payload in endpoints:
        try:
            result = call(path, payload, method, timeout=40)
            if not (isinstance(result, dict) and result.get("ok") is True):
                broken.append(f"{method} {path}: {str(result)[:60]}")
        except Exception as exc:
            broken.append(f"{method} {path}: {type(exc).__name__} {exc}")
    check(f"{len(endpoints)} 个 HTTP 接口全部正常", not broken, "；".join(broken[:3]))

    overview = call("/api/overview")
    check("市场概览返回涨跌家数",
          bool(overview.get("ready")) and overview.get("up") is not None,
          f"涨{overview.get('up')} 跌{overview.get('down')} 涨停{overview.get('limit_up')}")
    manual = call("/api/screen", {"limit": 5, "chg_min": 2, "vol_ratio_min": 2}, "POST")
    check("手动筛选接口返回股票列表",
          bool(manual.get("stocks")) and (manual.get("total") or 0) > 0,
          f"命中 {manual.get('total')} 只")

    # ---------- 12. 前端静态资源
    print("\n[12] 检查前端资源")
    for path, keyword in (("/", "app.js"), ("/static/app.js", "sendMessage"), ("/static/style.css", "--up")):
        req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}{path}")
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as resp:
            text = resp.read().decode("utf-8")
        check(f"{path} 可访问且内容正确", keyword in text, f"{len(text)} 字节")

    # ---------- 汇总
    httpd.shutdown()
    mock.shutdown()

    print("\n" + "=" * 68)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  - {name}")
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
