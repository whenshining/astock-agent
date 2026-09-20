"""Agent 主循环：大模型 -> 工具调用 -> 结果回填 -> 继续，直到给出最终答复。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import config
from . import usage as usage_mod
from .engine import screener
from .llm import prompts
from .llm import tools as tool_mod
from .llm.client import DeepSeekClient, LLMError
from .memory import store as mem

Emit = Callable[[dict[str, Any]], None]

# 工具的中文展示名（前端显示用）
TOOL_LABELS = {
    "screen_stocks": "筛选股票",
    "get_stock_detail": "查询个股",
    "get_market_overview": "查看市场概览",
    "get_kline": "读取K线",
    "get_data_status": "检查数据状态",
    "search_knowledge_base": "检索知识库",
    "get_watchlist": "读取自选股",
    "backtest_strategy": "回测策略",
    "validate_strategy": "样本外验证",
    "list_strategies": "查看自选策略",
    "run_strategy": "执行自选策略",
    "save_strategy": "保存自选策略",
}


def _tool_label(name: str, args: dict[str, Any]) -> str:
    """给工具调用生成一句人话摘要，让用户知道助手在干什么。"""
    if name == "screen_stocks":
        return "正在按条件筛选全市场股票…"
    if name == "get_stock_detail":
        return f"正在查询 {args.get('code', '')} 的指标…"
    if name == "get_kline":
        return f"正在读取 {args.get('code', '')} 的日线数据…"
    if name == "get_market_overview":
        return "正在统计全市场涨跌情况…"
    if name == "get_data_status":
        return "正在检查本地数据状态…"
    if name == "search_knowledge_base":
        return f"正在检索知识库：{args.get('query', '')}"
    if name == "get_watchlist":
        return f"正在读取自选板块「{args.get('group') or '自选股'}」…"
    if name == "backtest_strategy":
        return f"正在回测「{args.get('strategy') or '策略'}」…（要扫全市场历史数据，约 1 分钟）"
    if name == "validate_strategy":
        return f"正在做样本外验证「{args.get('strategy') or '策略'}」…（约 2 分钟）"
    if name == "list_strategies":
        return "正在查看你的自选策略…"
    if name == "run_strategy":
        return f"正在执行策略「{args.get('name', '')}」…"
    if name == "save_strategy":
        return f"正在保存策略「{args.get('name', '')}」…"
    return f"正在调用 {name}…"


def _summarize_tool_result(name: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return result.get("error") or "工具执行失败"
    if name == "screen_stocks":
        hits = result.get("hit_count", 0)
        returned = result.get("returned", 0)
        return f"命中 {hits} 只，展示 {returned} 只"
    if name == "get_stock_detail":
        stock = result.get("stock") or {}
        return f"{stock.get('code')} 收盘 {stock.get('close')}，涨跌幅 {stock.get('chg')}%"
    if name == "get_market_overview":
        ov = result.get("overview") or {}
        return f"上涨 {ov.get('up')} 家 / 下跌 {ov.get('down')} 家"
    if name == "get_kline":
        return f"取到 {len(result.get('bars') or [])} 根日线"
    if name == "search_knowledge_base":
        count = result.get("count", 0)
        if not count:
            return "知识库中没有找到相关内容"
        names = "、".join(sorted({r.get("doc_name", "") for r in result.get("results") or []}))
        return f"命中 {count} 段知识（来自 {names}）"
    if name == "get_watchlist":
        return f"自选板块「{result.get('group')}」共 {result.get('count', 0)} 只"
    if name == "backtest_strategy":
        bm = result.get("benchmark") or {}
        excess = result.get("excess_return")
        text = f"回测 {result.get('total_return', 0):+.1f}%"
        if bm:
            text += f" vs {bm.get('name')} {bm.get('return_pct', 0):+.1f}%"
        if excess is not None:
            text += f"（超额 {excess:+.1f}%）"
        return text
    if name == "validate_strategy":
        summary = result.get("summary") or {}
        level = {"good": "可信", "warn": "存疑", "bad": "过拟合"}.get(
            summary.get("level", ""), "")
        return (f"样本外累计 {summary.get('out_sample_total', 0):+.1f}%"
                + (f"，判定：{level}" if level else ""))
    if name == "list_strategies":
        return f"共 {result.get('count', 0)} 个自选策略"
    if name == "run_strategy":
        strat = result.get("strategy") or {}
        return f"策略「{strat.get('name')}」命中 {result.get('hit_count', 0)} 只"
    if name == "save_strategy":
        saved = result.get("saved") or {}
        verb = "已更新" if saved.get("replaced") else "已保存"
        return f"{verb}策略「{saved.get('name')}」"
    return "完成"


def build_client(cfg: dict[str, Any] | None = None) -> DeepSeekClient:
    cfg = cfg or config.load()
    return DeepSeekClient(
        api_key=cfg.get("deepseek_api_key") or "",
        base_url=cfg.get("base_url") or "https://api.deepseek.com",
        model=cfg.get("model") or "deepseek-flash",
        temperature=float(cfg.get("temperature") or 0.3),
        thinking=bool(cfg.get("thinking", True)),
        thinking_effort=cfg.get("thinking_effort") or "high",
    )


def run_turn(
    db_file: str | Path,
    session_id: str,
    user_text: str,
    emit: Emit,
    client: DeepSeekClient | None = None,
) -> None:
    """执行一轮完整对话，事件通过 emit 回调实时推送。"""
    cfg = config.load()

    if not cfg.get("deepseek_api_key"):
        emit({
            "type": "error",
            "message": "还没有配置 DeepSeek API Key。点右上角「设置」填入后即可开始对话。",
        })
        return

    try:
        client = client or build_client(cfg)
    except LLMError as exc:
        emit({"type": "error", "message": str(exc)})
        return

    user_text = (user_text or "").strip()
    if not user_text:
        emit({"type": "error", "message": "消息内容为空。"})
        return

    mem.add_message(db_file, session_id, "user", user_text)

    # 首条消息用它给会话命名
    session = mem.get_session(db_file, session_id)
    if session and (session.get("title") in (None, "", "新对话")):
        mem.rename_session(db_file, session_id, user_text[:24])

    market = screener.stats(db_file)
    knowledge = tool_mod.knowledge_summary(db_file)
    from .strategies import prompt_summary as _strategies_prompt

    strategies_text = _strategies_prompt(db_file)
    from .engine import watchlist as _watchlist

    watchlist_info = _watchlist.summary(config.get_vipdoc())
    # 知识库为空时不会把检索工具给模型——从机制上保证「没文档就不给建议」
    live_tools = tool_mod.available_tools(db_file)
    live_tool_names = {t.get("function", {}).get("name") for t in live_tools}
    system_prompt = prompts.build_system_prompt(
        memories_text=mem.memories_prompt(db_file) if cfg.get("memory_enabled", True) else "",
        market=market,
        vipdoc_path=config.get_vipdoc() or "",
        knowledge=knowledge,
        strategies_text=strategies_text,
        watchlist=watchlist_info,
    )
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(mem.build_llm_messages(db_file, session_id))

    max_rounds = int(cfg.get("max_tool_rounds") or 6)
    final_text_parts: list[str] = []
    turn_cost = 0.0
    turn_tokens = 0

    try:
        for _round in range(max_rounds):
            assistant_message: dict[str, Any] | None = None
            round_text: list[str] = []
            round_usage: dict[str, Any] = {}

            for event in client.stream_chat(messages, tools=live_tools):
                if event["type"] == "delta":
                    round_text.append(event["content"])
                    emit({"type": "text", "delta": event["content"]})
                elif event["type"] == "reasoning":
                    emit({"type": "reasoning", "delta": event["content"]})
                elif event["type"] == "final":
                    assistant_message = event["message"]
                    round_usage = event.get("usage") or {}

            if assistant_message is None:
                emit({"type": "error", "message": "模型没有返回内容，请重试。"})
                return

            # 记账：每次模型调用都记一次用量与费用
            recorded = usage_mod.record(db_file, session_id, cfg.get("model"), round_usage)
            if recorded:
                turn_cost += recorded["cost_usd"]
                turn_tokens += recorded["total_tokens"]
                emit({"type": "usage", "usage": recorded})

            text = assistant_message.get("content") or ""
            # 官方要求：请求带 tools 时，历史轮次的 reasoning_content 必须回传，
            # 否则思考模式下的多轮工具调用会缺上下文。这里全程保留。
            reasoning = assistant_message.get("reasoning_content") or ""
            if text:
                final_text_parts.append(text)

            tool_calls = assistant_message.get("tool_calls") or []
            mem.add_message(
                db_file, session_id, "assistant", text or None,
                tool_calls=tool_calls or None,
                reasoning=reasoning or None,
            )
            assistant_out: dict[str, Any] = {"role": "assistant", "content": text}
            if reasoning:
                assistant_out["reasoning_content"] = reasoning
            if tool_calls:
                assistant_out["tool_calls"] = tool_calls
            messages.append(assistant_out)

            if not tool_calls:
                break

            for call in tool_calls:
                call_id = call.get("id") or f"call_{int(time.time() * 1000)}"
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}

                emit({
                    "type": "tool_start",
                    "id": call_id,
                    "name": name,
                    "label": _tool_label(name, args),
                    "args": args,
                })

                started = time.perf_counter()
                try:
                    if name not in live_tool_names:
                        # 例如用户刚把知识库清空：历史消息里还留着这个工具名，
                        # 模型可能照旧调用。这里明确拒绝并把原因告诉它，
                        # 免得它拿到"查不到"的结果后自己纠结前后矛盾。
                        result = {
                            "ok": False,
                            "error": (
                                f"工具 {name} 当前不可用。"
                                "（用户已清空知识库或该功能已关闭）请不要再调用它，"
                                "改为如实说明你目前没有可引用的依据。"
                            ),
                        }
                    else:
                        result = tool_mod.execute(name, args, db_file)
                except Exception as exc:  # 工具异常不应中断整轮对话
                    result = {"ok": False, "error": f"工具执行出错：{exc}"}
                elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

                payload: dict[str, Any] = {
                    "type": "tool_result",
                    "id": call_id,
                    "name": name,
                    "ok": bool(result.get("ok")),
                    "summary": _summarize_tool_result(name, result),
                    "elapsed_ms": elapsed_ms,
                }
                if name == "screen_stocks" and result.get("ok"):
                    payload["stocks"] = result.get("stocks") or []
                    payload["total"] = result.get("hit_count")
                    payload["conditions"] = result.get("conditions") or []
                    payload["trade_date"] = result.get("trade_date")
                    payload["sorted_by"] = result.get("sorted_by")
                elif name == "run_strategy" and result.get("ok"):
                    # 复用选股结果的渲染（表格），另外带上策略信息
                    strategy = result.get("strategy") or {}
                    payload["stocks"] = result.get("stocks") or []
                    payload["total"] = result.get("hit_count")
                    payload["conditions"] = [strategy.get("summary") or ""]
                    payload["trade_date"] = result.get("trade_date")
                    payload["sorted_by"] = result.get("sorted_by")
                    payload["strategy"] = strategy
                elif name == "save_strategy" and result.get("ok"):
                    payload["strategy"] = result.get("saved")
                elif name == "list_strategies" and result.get("ok"):
                    payload["strategies"] = result.get("strategies") or []
                elif name == "search_knowledge_base" and result.get("ok"):
                    # 把命中的知识片段连同出处一起推给前端，让引用可核对
                    payload["knowledge"] = result.get("results") or []
                    payload["query"] = result.get("query")
                    payload["total"] = result.get("count", 0)
                elif name in ("backtest_strategy", "validate_strategy") and result.get("ok"):
                    # 把回测结论推给前端，渲染成带判断的卡片
                    payload["backtest"] = result
                    payload["mode"] = "validate" if name == "validate_strategy" else "backtest"
                elif result.get("ok"):
                    payload["detail"] = result
                else:
                    payload["error"] = result.get("error")
                emit(payload)

                mem.add_message(
                    db_file, session_id, "tool",
                    tool_mod.result_to_text(result),
                    tool_call_id=call_id,
                    name=name,
                    meta={
                        "summary": payload["summary"],
                        "ok": payload["ok"],
                        "stocks": payload.get("stocks"),
                        "total": payload.get("total"),
                        "conditions": payload.get("conditions"),
                        "trade_date": payload.get("trade_date"),
                        "sorted_by": payload.get("sorted_by"),
                        "knowledge": payload.get("knowledge"),
                        "query": payload.get("query"),
                        "strategy": payload.get("strategy"),
                        "strategies": payload.get("strategies"),
                        "name": name,
                        "error": payload.get("error"),
                    },
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_mod.result_to_text(result),
                })
        else:
            # 工具轮次用尽
            emit({
                "type": "text",
                "delta": "\n\n（工具调用次数已达上限，如需继续请再跟我说一声。）",
            })

    except LLMError as exc:
        emit({"type": "error", "message": str(exc)})
        return
    except Exception as exc:  # 兜底，避免连接被无声中断
        emit({"type": "error", "message": f"对话出错：{exc}"})
        return

    emit({
        "type": "done",
        "session_id": session_id,
        "cost_usd": round(turn_cost, 6),
        "tokens": turn_tokens,
        "summary": usage_mod.summary(db_file),
    })

    if cfg.get("memory_enabled", True):
        _extract_memories_async(db_file, session_id, client)


# ------------------------------------------------------------------ 长期记忆自动提取

def _extract_memories_async(db_file: str | Path, session_id: str, client: DeepSeekClient) -> None:
    """后台提取长期记忆，不阻塞用户。"""

    def worker() -> None:
        try:
            messages = mem.get_messages(db_file, session_id)
        except Exception:
            return
        # 取最近的用户消息与助手回复（不含工具原始数据）
        trimmed: list[dict[str, Any]] = []
        for msg in messages[-12:]:
            if msg["role"] == "user":
                trimmed.append({"role": "user", "content": (msg.get("content") or "")[:500]})
            elif msg["role"] == "assistant" and msg.get("content"):
                trimmed.append({"role": "assistant", "content": (msg.get("content") or "")[:500]})
        if not any(m["role"] == "user" for m in trimmed):
            return

        payload = [{"role": "system", "content": prompts.MEMORY_EXTRACT_PROMPT}] + trimmed
        sink: dict[str, Any] = {}
        try:
            reply = client.chat(payload, temperature=0, max_tokens=600, usage_sink=sink)
        except Exception:
            return
        # 记忆提取也是一次真实调用，同样要记账
        if sink:
            try:
                usage_mod.record(db_file, session_id, getattr(client, "model", None), sink)
            except Exception:
                pass

        text = (reply.get("content") or "").strip()
        if not text:
            return
        # 容错解析：去掉可能的 ```json 包裹
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text.strip("`")
            text = text.lstrip("json").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            return

        for item in (data.get("memories") or [])[:5]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            kind = str(item.get("kind") or "note").strip()
            if 4 <= len(content) <= 120:
                mem.add_memory(db_file, content, kind=kind, source=session_id)

    threading.Thread(target=worker, daemon=True).start()
