"""DeepSeek 客户端：OpenAI 兼容接口的最小实现（纯标准库，支持流式与工具调用）。

不用第三方 SDK 的原因：打包成 exe 时依赖越少越稳，体积也越小。
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
import urllib.request
from typing import Any, Iterator

DEFAULT_TIMEOUT = 300


class LLMError(RuntimeError):
    """调用大模型失败。"""


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-flash",
        temperature: float = 0.3,
        timeout: int = DEFAULT_TIMEOUT,
        thinking: bool = True,
        thinking_effort: str = "high",
    ) -> None:
        if not api_key:
            raise LLMError("未配置 DeepSeek API Key，请在设置里填写。")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.thinking = thinking
        self.thinking_effort = thinking_effort if thinking_effort in ("low", "high", "max") else "high"
        self._ctx = ssl.create_default_context()

    # ------------------------------------------------------------ 内部

    def _base_payload(self, messages: list[dict[str, Any]], temperature: float | None = None) -> dict[str, Any]:
        """构造请求体。

        思考模式按官方要求用 `thinking` 字段开关；该模式下 temperature 不生效，
        因此只在关闭思考时才发送它（发了也不报错，但会误导人）。

        思考强度用顶层的 `reasoning_effort`，取值 low / high / max（官方默认 high）。
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "thinking": {"type": "enabled" if self.thinking else "disabled"},
        }
        if self.thinking:
            payload["reasoning_effort"] = self.thinking_effort
        else:
            payload["temperature"] = self.temperature if temperature is None else temperature
        return payload

    def _request(self, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            method="POST",
        )
        # 直连：不走系统代理（代理环境下 DeepSeek 反而握手失败）
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=self._ctx),
        )
        try:
            return opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            if exc.code == 401:
                raise LLMError("DeepSeek API Key 无效或已过期（401）。请在设置里检查 Key。") from exc
            if exc.code == 402:
                raise LLMError("DeepSeek 账户余额不足（402）。") from exc
            if exc.code == 429:
                raise LLMError("请求过于频繁或额度受限（429），请稍后再试。") from exc
            if exc.code == 400:
                lowered = detail.lower()
                if "tool" in lowered or "function" in lowered:
                    raise LLMError(
                        f"当前模型（{self.model}）似乎不支持工具调用，而选股正好依赖它。"
                        "请在「设置」里把模型改成 deepseek-chat 后重试。"
                        f"（接口原文：{detail[:160]}）"
                    ) from exc
            raise LLMError(f"DeepSeek 接口返回 {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"无法连接 DeepSeek（{exc.reason}）。请检查网络。") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise LLMError("连接 DeepSeek 超时，请稍后再试。") from exc

    # ------------------------------------------------------------ 非流式

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage_sink: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """一次性返回完整回复。

        usage_sink 传入字典时，会把本次的 token 用量写进去（用于计费统计）。
        """
        payload = self._base_payload(messages, temperature)
        payload["stream"] = False
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if max_tokens:
            payload["max_tokens"] = max_tokens

        resp = self._request(payload)
        with resp:
            data = json.loads(resp.read().decode("utf-8"))
        if usage_sink is not None and isinstance(data.get("usage"), dict):
            usage_sink.update(data["usage"])
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("DeepSeek 返回了空结果。")
        return choices[0].get("message", {})

    # ------------------------------------------------------------ 流式

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """流式对话。依次产出事件：

        {"type": "delta",     "content": "..."}   文本增量
        {"type": "reasoning", "content": "..."}   思维链增量（reasoner 模型）
        {"type": "final",     "message": {...}, "finish_reason": "stop"|"tool_calls", "usage": {...}}
        """
        payload = self._base_payload(messages, temperature)
        payload["stream"] = True
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = self._request(payload)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage: dict[str, Any] = {}

        with resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue

                # 官方说明：最后一个 chunk 会带整次请求的 token 用量；
                # 开了 stream_options.include_usage 时，该 chunk 的 choices 是空数组。
                # 所以必须在看 choices 之前先把 usage 取走，否则会被跳过。
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                text = delta.get("content")
                if text:
                    content_parts.append(text)
                    yield {"type": "delta", "content": text}

                reason = delta.get("reasoning_content")
                if reason:
                    reasoning_parts.append(reason)
                    yield {"type": "reasoning", "content": reason}

                for call in delta.get("tool_calls") or []:
                    index = call.get("index", 0)
                    slot = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += fn["arguments"]

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
            finish_reason = "tool_calls"

        yield {"type": "final", "message": message, "finish_reason": finish_reason, "usage": usage}

    # ------------------------------------------------------------ 连通性

    def verify(self) -> dict[str, Any]:
        """最小代价验证 Key 是否可用。"""
        message = self.chat([{"role": "user", "content": "ping"}], max_tokens=1, temperature=0)
        return {"ok": True, "model": self.model, "reply": (message.get("content") or "")[:20]}
