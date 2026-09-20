"""本地 Mock 大模型服务：模拟 DeepSeek 的 OpenAI 兼容接口。

用途：没有 API Key 时也能把「对话 → 工具调用 → 结果回填」整条链路跑通，
便于开发调试、界面预览和自动化测试。

用法：
    python tools/mock_llm.py --port 8799

然后让程序连它：
    set ASTOCK_BASE_URL=http://127.0.0.1:8799
    set DEEPSEEK_API_KEY=mock
    python -m app.main --no-browser
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Mock 固定发起的选股请求
SCREEN_ARGS = {"chg_min": 2, "vol_ratio_min": 2, "sort_by": "vol_ratio", "limit": 5}


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    request_log: list[str] = []
    system_log: list[str] = []   # 收到的全部系统提示词，便于确认程序实际发了什么

    def log_message(self, fmt: str, *args) -> None:
        pass

    # ---------------------------------------------------------- 路由

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {}

        messages = payload.get("messages") or []
        system = next((m.get("content") or "" for m in messages if m.get("role") == "system"), "")
        if system:
            MockHandler.system_log.append(system)

        if not payload.get("stream"):
            if "长期记忆" in system:
                body = {"memories": [{"kind": "preference", "content": "用户偏好量比大于 2 的放量股票"}]}
                return self._json({"choices": [{"message": {"role": "assistant", "content": json.dumps(body, ensure_ascii=False)}}]})
            return self._json({"choices": [{"message": {"role": "assistant", "content": "pong"}}]})

        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if has_tool_result:
            return self._stream_text(messages)
        return self._stream_tool_call()

    def do_GET(self) -> None:
        self._json({
            "ok": True,
            "mock": True,
            "requests": len(MockHandler.request_log),
            "system_prompts": MockHandler.system_log,
        })

    # ---------------------------------------------------------- SSE

    def _sse_start(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _sse_write(self, chunk: dict) -> None:
        self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _json(self, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream_tool_call(self) -> None:
        MockHandler.request_log.append("tool_call")
        raw = json.dumps(SCREEN_ARGS, ensure_ascii=False)
        half = len(raw) // 2
        self._sse_start()
        self._sse_write({"choices": [{"index": 0, "delta": {"role": "assistant", "content": "好的，我来筛一遍。"}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_mock_1", "type": "function", "function": {"name": "screen_stocks", "arguments": ""}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[:half]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": raw[half:]}}
        ]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _stream_text(self, messages: list[dict]) -> None:
        MockHandler.request_log.append("summary")
        tool_msg = next((m for m in reversed(messages) if m.get("role") == "tool"), None)
        info = {}
        try:
            info = json.loads((tool_msg or {}).get("content") or "{}")
        except ValueError:
            pass
        stocks = info.get("stocks") or []
        lines = [f"筛完了，一共命中 {info.get('hit_count', '?')} 只，按{info.get('sorted_by', '量比')}排序。"]
        for s in stocks[:3]:
            name = s.get("name") or s.get("code")
            lines.append(f"- {s.get('code')} {name}：收 {s.get('close')} 元，涨 {s.get('chg')}%，量比 {s.get('vol_ratio')}")
        lines.append("这几只都是明显放量的，注意追高风险。以上为技术面筛选结果，非投资建议。")
        text = "\n".join(lines)

        self._sse_start()
        for i in range(0, len(text), 24):
            self._sse_write({"choices": [{"index": 0, "delta": {"content": text[i:i + 24]}, "finish_reason": None}]})
        self._sse_write({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地 Mock DeepSeek 服务")
    parser.add_argument("--port", type=int, default=8799)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), MockHandler)
    print(f"Mock DeepSeek 服务已启动: http://127.0.0.1:{args.port}")
    print("设置 ASTOCK_BASE_URL 指向它即可让程序使用。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
