"""暴露给大模型的选股工具（OpenAI function calling 格式）+ 执行器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import config
from ..engine import screener
from ..engine.tdx import read_day

# 给模型看的工具声明
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "screen_stocks",
            "description": (
                "按价量技术面条件筛选 A 股（数据为本地通达信日线，只含 OHLCV 派生指标）。"
                "所有条件都是可选的，没给的条件不加限制。返回命中数量与候选股列表。"
                "注意：不含市盈率/市净率/市值/换手率，也不含盘中实时数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_price": {"type": "number", "description": "收盘价下限（元）"},
                    "max_price": {"type": "number", "description": "收盘价上限（元）"},
                    "chg_min": {"type": "number", "description": "当日涨跌幅下限（%）"},
                    "chg_max": {"type": "number", "description": "当日涨跌幅上限（%），如只要下跌可设 0"},
                    "chg5_min": {"type": "number", "description": "近 5 日累计涨幅下限（%）"},
                    "chg20_min": {"type": "number", "description": "近 20 日累计涨幅下限（%）"},
                    "vol_ratio_min": {"type": "number", "description": "量比下限（今日成交量/前5日均量），1.5 表示明显放量"},
                    "vol_ratio_max": {"type": "number", "description": "量比上限，如筛缩量可设 1"},
                    "amplitude_min": {"type": "number", "description": "当日振幅下限（%）"},
                    "amplitude_max": {"type": "number", "description": "当日振幅上限（%）"},
                    "amp20_max": {"type": "number", "description": "近 20 日平均振幅上限（%），用于筛低波动"},
                    "consec_up_min": {"type": "integer", "description": "连续上涨天数下限"},
                    "amount_min": {"type": "number", "description": "当日成交额下限（元），如 5 亿 = 500000000"},
                    "amount_max": {"type": "number", "description": "当日成交额上限（元）"},
                    "new_high60": {"type": "boolean", "description": "true=只要创 60 日新高的（收盘价为近60日最高）"},
                    "new_high20": {"type": "boolean", "description": "true=只要创 20 日新高的"},
                    "ma_bull": {"type": "boolean", "description": "true=只要均线多头排列（MA5>MA10>MA20 且收盘价>MA5）"},
                    "min_drawdown60": {
                        "type": "number",
                        "description": "距 60 日高点的回撤下限（%，负数，如 -10 表示距高点回撤不超过 10% 即 -10 以上）",
                    },
                    "boards": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["sh_main", "star", "sz_main", "chinext"]},
                        "description": "板块限制：沪市主板/科创板/深市主板/创业板，不填则全市场",
                    },
                    "watchlist": {
                        "type": "boolean",
                        "description": (
                            "true = 只在用户的自选股里筛选。"
                            "用户说「在我自选股里找…」时设为 true。"
                        ),
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": ["chg", "chg5", "chg20", "vol_ratio", "amplitude", "consec_up", "close", "amount"],
                        "description": "排序字段，默认 chg（涨跌幅）",
                    },
                    "sort_order": {"type": "string", "enum": ["desc", "asc"], "description": "排序方向，默认 desc 降序"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20，最大 100"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock_detail",
            "description": "查询单只股票的最新指标详情（收盘价、涨跌幅、量比、振幅、均线、连涨、是否创新高、成交额等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位股票代码，如 600519"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_overview",
            "description": "获取全市场概览：涨跌家数、涨停/跌停数量、均线多头家数、创60日新高家数、平均涨跌幅、数据日期。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_kline",
            "description": "获取单只股票最近的日线 K 线数据（开高低收、成交量、成交额），用于分析走势形态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "6 位股票代码"},
                    "days": {"type": "integer", "description": "返回最近多少个交易日，默认 30，最大 120"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_watchlist",
            "description": (
                "读取用户在通达信里的自选股，返回这些股票及其当前指标。"
                "用户问「我的自选股怎么样」「我自选里有没有…」时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "with_metrics": {
                        "type": "boolean",
                        "description": "是否返回每只股票的当前指标（默认 true）。只想看名单时设 false。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_status",
            "description": "查询本地日线数据状态：最新交易日、缓存股票数量、vipdoc 路径。用户问数据是否最新时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def _strategy_param_properties() -> dict[str, Any]:
    """由 PARAM_SPEC 生成 save_strategy 的参数定义，避免两处维护。"""
    from ..engine.screener import BOARDS, SORT_FIELDS
    from ..strategies import PARAM_SPEC

    board_values = list(BOARDS.keys())
    sort_values = list(SORT_FIELDS.keys())
    properties: dict[str, Any] = {}
    for spec in PARAM_SPEC:
        key, kind = spec["key"], spec["type"]
        label = spec["label"] + (f"（{spec['unit']}）" if spec.get("unit") else "")
        if kind == "bool":
            properties[key] = {"type": "boolean", "description": label}
        elif kind == "integer":
            properties[key] = {"type": "integer", "description": label}
        elif kind == "boards":
            properties[key] = {
                "type": "array", "items": {"type": "string", "enum": board_values},
                "description": f"{label}：沪市主板/科创板/深市主板/创业板",
            }
        elif kind == "sort":
            properties[key] = {"type": "string", "enum": sort_values, "description": label}
        elif kind == "order":
            properties[key] = {"type": "string", "enum": ["asc", "desc"], "description": label}
        elif kind == "amount":
            properties[key] = {"type": "number",
                               "description": f"{label}（单位：亿元，例如 5 表示 5 亿）"}
        else:
            properties[key] = {"type": "number", "description": label}
    return properties


# 策略工具：让模型能查、能跑、能存用户的自选策略
STRATEGY_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_strategies",
            "description": (
                "列出用户已保存的自选策略（含名称、条件和描述）。"
                "用户提到「我的某个策略」但你不确定它包含什么条件时，先调用它看清楚。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_strategy",
            "description": (
                "按名称执行一个已保存的策略，直接返回筛选结果。"
                "用户说「用XX策略选股」时用它，**不要自己去猜或复述参数**——"
                "策略参数以保存的为准。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "策略名称（可只写关键部分，会自动模糊匹配）"},
                    "limit": {"type": "integer", "description": "可选，覆盖策略里保存的返回条数"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_strategy",
            "description": (
                "把一组选股条件保存成用户的命名策略。"
                "用户说「把这个存成策略」或你们商定好了一套条件想固化下来时使用。"
                "**保存前务必先把策略内容复述一遍请用户确认**，除非他已经明确说「保存」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "策略名称，简短好记，例如「放量上涨」"},
                    "description": {"type": "string", "description": "一句话说明这个策略的选股逻辑"},
                    "params": {
                        "type": "object",
                        "description": "筛选条件。只填需要的字段，没提到的不要填。",
                        "properties": _strategy_param_properties(),
                    },
                },
                "required": ["name", "params"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "backtest_strategy",
            "description": (
                "用历史数据回测一个策略，看它到底能不能赚钱。返回总收益、最大回撤、胜率、"
                "盈亏比，以及同期大盘基准的对照和超额收益。"
                "用户问「这个策略能赚钱吗」「回测一下」「跑一年能赚多少」时用它。"
                "耗时约 1 分钟（要扫描全市场历史数据）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "description": "策略名称，例如「放量上涨」。留空则用用户正在讨论的那个策略。",
                    },
                    "range": {
                        "type": "string",
                        "enum": ["3m", "6m", "1y", "2y"],
                        "description": "回测区间，默认 1y（近一年）。",
                    },
                    "capital": {
                        "type": "number",
                        "description": "初始资金，默认 10 万。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_strategy",
            "description": (
                "样本外滚动验证：判断一个策略是真实有效，还是调参数调出来的过拟合。"
                "做法是把区间切段，前一段调参、后一段用**没见过的数据**检验，滚动推进。"
                "用户问「这个策略靠谱吗」「会不会过拟合」「能不能上实盘」时用它。"
                "耗时约 2 分钟（要扫描全市场 + 多组参数各跑一遍），比回测慢。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "description": "策略名称。留空则用用户正在讨论的那个策略。",
                    },
                    "range": {
                        "type": "string",
                        "enum": ["1y", "2y", "3y"],
                        "description": "验证区间，默认 2y（样本外验证需要足够长度，太短切不出几折）。",
                    },
                    "capital": {"type": "number", "description": "初始资金，默认 10 万。"},
                },
            },
        },
    },
]


# 知识库检索工具：**只有知识库里有文档时才会提供给模型**。
# 这是「没有知识库就绝不提建议」的硬保证——模型拿不到工具，就无从引用。
KB_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_knowledge_base",
        "description": (
            "在用户导入的本地知识库文档中检索相关片段。"
            "凡是要给出策略、规则、经验、判读标准这类说法，都必须先用它查证，不要凭记忆回答。"
            "返回的每条结果都带 citation 字段（出处），引用时必须原样标注。"
            "如果返回 0 条，说明知识库里没有相关记载，此时不要给出任何建议性内容。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索用的关键词或问题，越具体越好（例如「量比 阈值」「止损 规则」）",
                },
                "top_k": {"type": "integer", "description": "返回条数，默认 4，最多 8"},
            },
            "required": ["query"],
        },
    },
}


def available_tools(db_file: str | Path) -> list[dict[str, Any]]:
    """本轮可用的工具：数据工具 + 策略工具（始终）+ 知识库工具（仅在知识库非空时）。"""
    from ..kb import store as kb_store

    tools = list(TOOLS)
    tools.extend(STRATEGY_TOOLS)      # 策略工具始终可用，没有策略时执行会给出明确提示
    try:
        if kb_store.stats(db_file).get("ready"):
            tools.append(KB_TOOL)
    except Exception:
        pass          # 知识库异常不该影响正常选股
    return tools


def knowledge_summary(db_file: str | Path) -> dict[str, Any]:
    """给系统提示词用的知识库状态。"""
    from ..kb import store as kb_store

    try:
        return kb_store.stats(db_file)
    except Exception:
        return {"documents": 0, "chunks": 0, "chars": 0, "ready": False}

# 精简后的股票字段（避免把整行数据塞给模型，浪费 token）
_STOCK_FIELDS = [
    "code", "name", "close", "chg", "chg5", "chg20", "vol_ratio",
    "amplitude", "consec_up", "ma5", "ma10", "ma20", "ma_bull",
    "new_high60", "drawdown60", "amount",
]


def _trim(stock: dict[str, Any]) -> dict[str, Any]:
    return {k: stock.get(k) for k in _STOCK_FIELDS if stock.get(k) is not None}


def execute(name: str, args: dict[str, Any], db_file: str | Path) -> dict[str, Any]:
    """执行工具调用，返回结果字典（会被序列化后交给模型）。"""
    if name == "screen_stocks":
        from ..engine import store as _store

        if not _store.cache_status(db_file).get("ready"):
            return {
                "ok": False,
                "error": "本地行情缓存尚未建立，无法筛选。请让用户点击界面右上角的「刷新数据」按钮建立缓存（首次约需 1-2 分钟）。",
            }
        params = dict(args or {})
        limit = params.get("limit")
        try:
            limit = max(1, min(100, int(limit))) if limit else 20
        except (TypeError, ValueError):
            limit = 20
        params["limit"] = limit
        # 只看自选股：每次执行时重新读 .blk 文件，用户在通达信里加的自选会自动跟上
        if params.get("watchlist"):
            from ..engine import watchlist as wl

            params, wl_count = wl.apply_watchlist_filter(params, config.get_vipdoc())
            if not wl_count:
                info = wl.load(config.get_vipdoc())
                return {
                    "ok": False,
                    "error": info.get("error") or "没读到你的自选股。",
                }
        result = screener.screen(db_file, params)
        return {
            "ok": True,
            "hit_count": result["total"],
            "returned": result["returned"],
            "trade_date": result["trade_date"],
            "conditions": result["conditions"],
            "sorted_by": f"{result['sort_label']}{'降序' if result['sort_desc'] else '升序'}",
            "stocks": [_trim(s) for s in result["stocks"]],
            "note": "本地日线技术面筛选结果，非投资建议。",
        }

    if name == "get_stock_detail":
        code = str((args or {}).get("code", "")).strip()
        if not code:
            return {"ok": False, "error": "缺少股票代码"}
        detail = screener.stock_detail(db_file, code[-6:].zfill(6))
        if not detail:
            return {"ok": False, "error": f"未找到 {code} 的数据（可能不在缓存中，或不是沪深主板/创业板/科创板股票）"}
        return {"ok": True, "stock": _trim(detail), "note": "非投资建议。"}

    if name == "get_market_overview":
        data = screener.stats(db_file)
        if not data.get("ready"):
            return {"ok": False, "error": "本地数据尚未建立缓存，请让用户先点击「刷新数据」。"}
        return {"ok": True, "overview": data, "note": "全市场统计，非投资建议。"}

    if name == "get_kline":
        params = args or {}
        code = str(params.get("code", "")).strip()[-6:].zfill(6)
        try:
            days = max(5, min(120, int(params.get("days") or 30)))
        except (TypeError, ValueError):
            days = 30
        vipdoc = config.get_vipdoc()
        market = "sh" if code.startswith(("6", "9")) else "sz"
        path = Path(vipdoc) / market / "lday" / f"{market}{code}.day"
        bars = read_day(path, max_records=days)
        if not bars:
            return {"ok": False, "error": f"未找到 {code} 的日线数据"}
        return {
            "ok": True,
            "code": code,
            "bars": [
                {
                    "date": str(b.date), "open": b.open, "high": b.high,
                    "low": b.low, "close": b.close,
                    "volume": b.volume, "amount": round(b.amount, 2),
                }
                for b in bars
            ],
            "note": "非投资建议。",
        }

    if name == "get_watchlist":
        from ..engine import watchlist as wl

        info = wl.load(config.get_vipdoc())
        if not info["available"]:
            return {"ok": False, "error": info.get("error") or "没读到自选股。"}

        with_metrics = (args or {}).get("with_metrics", True)
        if not with_metrics:
            return {
                "ok": True,
                "count": info["count"],
                "codes": info["codes"],
                "days_ago": info.get("days_ago"),
                "note": "这是用户自选股的代码列表。",
            }

        stocks: list[dict[str, Any]] = []
        missing: list[str] = []
        for code in info["codes"]:
            detail = screener.stock_detail(db_file, code)
            if detail:
                stocks.append(_trim(detail))
            else:
                missing.append(code)

        return {
            "ok": True,
            "count": info["count"],
            "days_ago": info.get("days_ago"),
            "stocks": stocks,
            "missing": missing or None,
            "note": (
                "这是用户自选股里的股票及其当前指标"
                + (f"；其中 {len(missing)} 只在本地行情缓存里没有数据（可能是停牌或已退市）" if missing else "")
                + "。想让筛选只在这个范围内进行，可以调用 screen_stocks 并设 watchlist=true。"
                + "自选股是用户自己挑选的，不代表任何推荐。"
            ),
        }

    if name == "get_data_status":
        from ..engine import store as _store

        status = _store.cache_status(db_file)
        status["vipdoc_path_config"] = config.get_vipdoc()
        return {"ok": True, "status": status}

    if name == "search_knowledge_base":
        from ..kb import index as kb_index
        from ..kb import store as kb_store

        stats = kb_store.stats(db_file)
        if not stats.get("ready"):
            return {
                "ok": True,
                "count": 0,
                "results": [],
                "note": "用户还没有导入任何知识库文档。你不知道任何策略或规则，不要给出建议性内容。",
            }

        query = str((args or {}).get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "缺少检索关键词"}
        try:
            top_k = max(1, min(8, int((args or {}).get("top_k") or 4)))
        except (TypeError, ValueError):
            top_k = 4

        hits = kb_index.search(db_file, query, top_k=top_k)
        if not hits:
            return {
                "ok": True,
                "query": query,
                "count": 0,
                "results": [],
                "note": "知识库里没有找到与该问题相关的内容。请如实说明没有相关记载，不要凭空给出建议。",
            }
        return {
            "ok": True,
            "query": query,
            "count": len(hits),
            "results": [
                {
                    "citation": h["citation"],
                    "doc_name": h["doc_name"],
                    "heading": h["heading"],
                    "seq": h["seq"],
                    "text": h["text"],
                    "score": h["score"],
                    "matched_terms": h.get("matched_terms", 0),
                }
                for h in hits
            ],
            "note": (
                "以上是知识库中与问题相关的候选片段（按词面相关度排序，**不是答案**）。"
                "score 越低越可能只是碰巧有词重合、实质无关；matched_terms 是命中的查询词个数，"
                "个数少通常说明关联很弱。请自行判断：与问题实质无关的，当作没查到处理。"
                "引用其中任何内容时，必须在回复里原样标注 citation 出处，"
                "格式如「（来源：《文件名》· 章节 · 第 N 段）」。"
                "绝不能用知识库内容去支撑预测或买卖建议。"
            ),
        }

    # ---------------- 自选策略

    if name == "list_strategies":
        from ..strategies import list_all

        items = list_all(db_file)
        return {
            "ok": True,
            "count": len(items),
            "strategies": [
                {
                    "name": item["name"],
                    "description": item.get("description") or "",
                    "summary": item["summary"],
                    "uses": item.get("uses", 0),
                }
                for item in items
            ],
            "note": (
                "用 run_strategy 按名称执行某个策略；执行时参数以保存的为准，不要自己改写。"
                if items else
                "用户还没有保存任何策略。可以问他要不要新建一个，或用 save_strategy 帮他存。"
            ),
        }

    if name == "run_strategy":
        # 注意：screener 已在模块顶部导入，这里**不能**再写
        # `from ..engine import screener`——那会让它变成 execute() 的局部变量，
        # 导致函数里其他分支引用 screener 时报 UnboundLocalError。
        from ..strategies import find, mark_used

        query = str((args or {}).get("name") or "").strip()
        if not query:
            return {"ok": False, "error": "缺少策略名称"}
        strategy = find(db_file, query)
        if not strategy:
            return {
                "ok": False,
                "error": f"没有找到名为「{query}」的策略。可以先用 list_strategies 看看用户保存了哪些。",
            }

        params = dict(strategy["params"])
        if (args or {}).get("limit"):
            try:
                params["limit"] = max(1, min(100, int(args["limit"])))
            except (TypeError, ValueError):
                pass
        # 策略里可能限定「只看自选股」，执行时重新读文件
        if params.get("watchlist"):
            from ..engine import watchlist as wl

            params, _ = wl.apply_watchlist_filter(params, config.get_vipdoc())

        result = screener.screen(db_file, params)
        mark_used(db_file, strategy["id"])
        return {
            "ok": True,
            "strategy": {
                "name": strategy["name"],
                "description": strategy.get("description") or "",
                "summary": strategy["summary"],
            },
            "hit_count": result["total"],
            "returned": result["returned"],
            "trade_date": result["trade_date"],
            "sorted_by": f"{result['sort_label']}{'降序' if result['sort_desc'] else '升序'}",
            "stocks": [_trim(s) for s in result["stocks"]],
            "note": f"以上是按用户保存的策略「{strategy['name']}」执行的结果。汇报时说明用的是这个策略、以及它的条件。",
        }

    if name == "save_strategy":
        from ..strategies import save as save_strategy

        strategy_name = str((args or {}).get("name") or "").strip()
        description = str((args or {}).get("description") or "").strip()
        params = (args or {}).get("params") or {}
        try:
            saved = save_strategy(db_file, strategy_name, description, params, source="agent")
        except ValueError as exc:
            return {"ok": False, "error": f"保存失败：{exc}"}
        return {
            "ok": True,
            "saved": {
                "name": saved["name"],
                "description": saved.get("description") or "",
                "summary": saved["summary"],
                "replaced": saved.get("replaced", False),
            },
            "note": (
                ("已更新原有同名策略。" if saved.get("replaced") else "已新建策略。")
                + "汇报时把策略名和条件告诉用户，并提示他可以在「策略」面板里修改或删除。"
            ),
        }

    if name in ("backtest_strategy", "validate_strategy"):
        return _run_backtest_tool(name, args or {}, db_file)

    return {"ok": False, "error": f"未知工具：{name}"}


# ------------------------------------------------------------------ 回测工具

# 回测很慢（要扫全市场历史数据），同一个策略短时间内重复问就直接给缓存
_BACKTEST_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_BACKTEST_TTL = 900            # 15 分钟
_BACKTEST_CACHE_MAX = 12

# 样本外验证用的默认参数网格
DEFAULT_SWEEP_GRID = {"hold_days": [3, 5, 10], "stop_loss": [5, 8, 10]}

# 记住每个策略最近一次的样本外验证结果：可信度评估要判断「验没验证过」，
# 没验证过的结果最高只能给「中等」。
_VALIDATION_MEMORY: dict[str, dict[str, Any]] = {}


def _range_dates(preset: str) -> tuple[int, int]:
    """把 3m/6m/1y/2y 换算成起止交易日。"""
    from ..backtest import data as bt_data

    days = {"3m": 63, "6m": 122, "1y": 244, "2y": 488, "3y": 732}.get(str(preset), 244)
    calendar = bt_data.trading_dates(config.get_vipdoc(), 19900101, 20991231)
    if not calendar:
        return 0, 0
    return calendar[max(0, len(calendar) - days)], calendar[-1]


def _cache_key(*parts: Any) -> str:
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)


def _cache_get(key: str) -> dict[str, Any] | None:
    import time as _time

    item = _BACKTEST_CACHE.get(key)
    if not item:
        return None
    if (_time.time() - item[0]) > _BACKTEST_TTL:
        _BACKTEST_CACHE.pop(key, None)
        return None
    return item[1]


def _cache_put(key: str, value: dict[str, Any]) -> None:
    import time as _time

    if len(_BACKTEST_CACHE) >= _BACKTEST_CACHE_MAX:
        oldest = min(_BACKTEST_CACHE, key=lambda k: _BACKTEST_CACHE[k][0])
        _BACKTEST_CACHE.pop(oldest, None)
    _BACKTEST_CACHE[key] = (_time.time(), value)


def _pick_strategy(db_file: str | Path, wanted: str) -> tuple[dict[str, Any] | None, str]:
    """按名字找策略，返回 (策略, 错误说明)。"""
    from ..strategies import find as find_strategy
    from ..strategies import list_all as list_strategies

    available = list_strategies(db_file)
    if not available:
        return None, (
            "用户还没有保存任何策略，没法回测。"
            "可以先聊清楚他想找什么样的股票，用 save_strategy 存一个再回测。"
        )

    text = (wanted or "").strip()
    if text:
        found = find_strategy(db_file, text)
        if not found:
            for item in available:
                if text in item["name"] or item["name"] in text:
                    found = item
                    break
        if found:
            return found, ""
        return None, ("没有找到叫「" + text + "」的策略。用户保存的策略有："
                      + "、".join(s["name"] for s in available) + "。")

    if len(available) == 1:
        return available[0], ""
    return None, ("不确定要回测哪一个策略，需要用户指定。用户保存的策略有："
                  + "、".join(s["name"] for s in available) + "。")


def _downsample(points: list[Any], target: int = 50) -> list[Any]:
    if not points:
        return []
    step = max(1, len(points) // target)
    picked = points[::step]
    if points[-1] not in picked:
        picked = picked + [points[-1]]
    return picked


def _trim_backtest(result: dict[str, Any]) -> dict[str, Any]:
    """裁掉大数组——整份净值曲线、基准点位、几百笔交易塞进模型上下文毫无必要。

    裁之前 17K 字符（光基准的 244 个点位就占了大头），裁完约 2K。
    """
    trimmed = {k: v for k, v in result.items() if k not in ("equity_curve", "trades")}
    trimmed["equity_curve"] = _downsample(result.get("equity_curve") or [])

    benchmark = result.get("benchmark")
    if isinstance(benchmark, dict):
        benchmark = dict(benchmark)
        benchmark["curve"] = _downsample(benchmark.get("curve") or [], 20)
        trimmed["benchmark"] = benchmark

    trades = result.get("trades") or []
    trimmed["trades"] = trades[:12]
    trimmed["trade_count"] = result.get("trade_count", len(trades))
    return trimmed


def _run_backtest_tool(name: str, args: dict[str, Any], db_file: str | Path) -> dict[str, Any]:
    """跑回测 / 样本外验证，并把结论一起给模型（结论比数字重要）。"""
    from ..backtest import engine as bt_engine

    target, error = _pick_strategy(db_file, str(args.get("strategy") or ""))
    if target is None:
        return {"ok": False, "error": error}

    # 样本外验证需要更长的区间，不然切不出几折
    default_range = "2y" if name == "validate_strategy" else "1y"
    range_key = str(args.get("range") or default_range)
    start_date, end_date = _range_dates(range_key)
    if not start_date or not end_date:
        return {"ok": False, "error": "没有读到交易日历，请确认通达信的日线数据可用。"}

    try:
        capital = float(args.get("capital") or 100000)
    except (TypeError, ValueError):
        capital = 100000.0

    params = target["params"]
    plan = target["plan"]
    grid = DEFAULT_SWEEP_GRID if name == "validate_strategy" else None

    # 策略里可能勾了「只看我的自选股」。必须在这里解析成代码集合——
    # 否则回测会拿全市场去跑，跟「试跑」的行为不一致，用户看到的条件不是实际跑的条件。
    universe_warning = ""
    if params.get("watchlist"):
        from ..engine import watchlist as wl_mod

        params, wl_count = wl_mod.apply_watchlist_filter(params, config.get_vipdoc())
        if wl_count:
            universe_warning = (
                f"本次只在用户的 {wl_count} 只自选股范围内回测。"
                "自选股是【现在】的名单，拿它去交易过去等于事后选股（look-ahead bias），"
                "结果偏乐观，只能参考，不能当成真实策略表现。必须把这个前提告诉用户。"
            )

    key = _cache_key(name, target["id"], params, plan, start_date, end_date, capital)
    cached = _cache_get(key)
    if cached:
        result = dict(cached)
        result["cached"] = True
        return result

    vipdoc = config.get_vipdoc()
    if name == "backtest_strategy":
        result = bt_engine.run_backtest(vipdoc, params, plan, start_date, end_date, capital=capital)
    else:
        result = bt_engine.walk_forward(
            vipdoc, params, plan, start_date, end_date, grid, folds=3, capital=capital
        )
        if not result.get("ok"):
            return result

    result["strategy"] = {"id": target["id"], "name": target["name"], "summary": target["summary"]}
    result["range"] = range_key
    result["ok"] = True
    if universe_warning:
        result["universe_warning"] = universe_warning

    # 可信度评估。必须在 _trim_backtest 之前算——收益集中度要看完整的交易列表，
    # 裁到 12 笔之后就算不出来了。
    from ..backtest import credibility as credibility_mod

    memory = _VALIDATION_MEMORY.get(str(target["id"]), {})
    if name == "backtest_strategy":
        result["credibility"] = credibility_mod.assess(
            result,
            validated=memory.get("validated"),
            sweep=memory.get("sweep"),
        )
    else:
        # 记住这次验证结果，下次回测同一条策略时可信度会把它算进去
        _VALIDATION_MEMORY.setdefault(str(target["id"]), {})["validated"] = result

    # 把「这策略到底行不行」的结论直接算好给模型，
    # 免得模型看到正收益就报喜不报忧
    if name == "backtest_strategy":
        bm = result.get("benchmark") or {}
        if bm:
            result["excess_return"] = round(result["total_return"] - bm["return_pct"], 2)
            if result["excess_return"] >= 0:
                result["verdict"] = (
                    f"跑赢{bm['name']} {result['excess_return']:.2f} 个百分点，"
                    "但要提醒用户单次回测无法证明策略有效，建议做样本外验证。"
                )
            else:
                result["verdict"] = (
                    f"跑输{bm['name']} {abs(result['excess_return']):.2f} 个百分点——"
                    "同期直接买指数比这个策略强，它没有产生超额价值。必须如实告诉用户。"
                )
        result = _trim_backtest(result)
    else:
        summary = result.get("summary") or {}
        result["verdict"] = summary.get("verdict", "")
        result["level"] = summary.get("level", "")
        benchmark = result.get("benchmark")
        if isinstance(benchmark, dict):
            benchmark = dict(benchmark)
            benchmark["curve"] = _downsample(benchmark.get("curve") or [], 20)
            result["benchmark"] = benchmark
        result["must_report"] = (
            "样本外验证的结论必须原样、明确地告诉用户，不能软化。"
            "level=bad 时要直接说「不能上实盘」。"
        )

    _cache_put(key, result)
    return result


def result_to_text(result: dict[str, Any]) -> str:
    """把工具结果转成给模型的消息文本。"""
    return json.dumps(result, ensure_ascii=False, default=str)
