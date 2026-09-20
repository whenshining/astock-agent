"""前端一致性静态检查：JS 里引用的元素 ID / 弹窗，HTML 里是否真的存在。

这类问题不会在服务端暴露，但会让界面在浏览器里直接崩，所以单独检查一遍。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "app" / "web"


def main() -> int:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    css = (WEB / "style.css").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(re.findall(r"""\$\(['"]#([A-Za-z0-9_-]+)['"]\)""", js))
    js_ids |= set(re.findall(r"""getElementById\(['"]([A-Za-z0-9_-]+)['"]\)""", js))

    # 这些 id 是 JS 运行时动态注入的，HTML 里本来就没有，不算问题。
    # 新增动态生成的元素时把 id 加到这里。
    DYNAMIC_IDS = {
        # 回测：条件卖出的参数输入由 renderExitParams() 注入
        "bt-exit-value", "bt-expr-input", "bt-expr-msg",
    }
    js_ids -= DYNAMIC_IDS

    problems: list[str] = []

    missing = sorted(js_ids - html_ids)
    if missing:
        problems.append(f"JS 引用了 HTML 中不存在的 id：{missing}")

    unused = sorted(html_ids - js_ids)
    print(f"HTML 中的 id：{len(html_ids)} 个")
    print(f"JS 引用的 id：{len(js_ids)} 个")
    if unused:
        print(f"（未被 JS 直接引用的 id，通常是纯样式节点）: {unused}")

    # data-close 指向的弹窗必须存在
    closes = set(re.findall(r'data-close="([^"]+)"', html))
    bad_close = sorted(closes - html_ids)
    if bad_close:
        problems.append(f"data-close 指向不存在的弹窗：{bad_close}")
    print(f"data-close 弹窗：{sorted(closes)}")

    # JS 里用到的 modal id（hidden 切换）也检查
    modal_refs = set(re.findall(r"""\$\('#([a-z-]*modal)'\)""", js))
    bad_modal = sorted(modal_refs - html_ids)
    if bad_modal:
        problems.append(f"JS 操作的弹窗不存在：{bad_modal}")

    # 检查 JS 绑定的关键按钮 id 是否齐全
    required = [
        "btn-new-session", "btn-settings", "btn-memories", "btn-refresh",
        "btn-save-config", "btn-verify", "btn-add-memory", "btn-quit",
        "btn-send",
        "input", "messages", "session-list", "memory-list",
        "settings-modal", "memories-modal", "kline-modal", "usage-modal", "kb-modal",
        "strategy-modal",
        "kline-canvas", "toast", "scan-bar", "scan-fill", "scan-text",
        "status-text", "status-dot", "overview",
        "cfg-api-key", "cfg-vipdoc", "cfg-model", "cfg-thinking", "cfg-memory",
        "cfg-data-dir", "model-hint", "api-key-hint", "verify-result", "vipdoc-hint",
        "new-memory", "memory-count", "kline-title", "welcome",
        "usage-card", "usage-balance", "usage-today", "btn-refresh-usage",
        "usage-balance-box", "usage-grid", "usage-models", "usage-note",
        "btn-kb", "kb-count", "kb-list", "kb-stats", "kb-file", "kb-query",
        "btn-kb-import", "btn-kb-clear", "btn-kb-search", "kb-search-result",
        "btn-strategies", "strategy-count", "strategy-list", "strategy-stats",
        "strategy-search", "btn-strategy-search", "btn-strategy-new",
        "strategy-form", "strategy-form-title", "strategy-name", "strategy-desc",
        "strategy-params", "btn-strategy-save", "btn-strategy-cancel",
        "strategy-form-msg", "strategy-run-result",
        "btn-watchlist", "watchlist-count", "watchlist-modal",
        "watchlist-body", "watchlist-desc", "btn-watchlist-refresh", "watchlist-updated",
        "backtest-modal", "bt-title", "bt-desc", "bt-config", "bt-range", "bt-capital",
        "bt-plan", "bt-exits", "bt-exit-new", "btn-bt-add-exit", "bt-costs",
        "btn-bt-run", "btn-bt-sweep", "bt-grid-config", "bt-sweep-fields",
        "btn-bt-sweep-run", "btn-bt-sweep-cancel", "bt-progress", "bt-fill",
        "bt-progress-text", "bt-result", "bt-metrics", "bt-equity", "bt-trades",
        "bt-trades-title", "bt-sweep-result", "bt-hint", "bt-toolbar-single",
        "cfg-thinking-effort", "model-detail", "model-hint", "thinking-effort-field",
        "bt-exit-params", "bt-expr-help",
    ]
    absent = [rid for rid in required if rid not in html_ids]
    if absent:
        problems.append(f"关键元素缺失：{absent}")

    # CSS 里必须定义了核心类
    for cls in ["session-item", "tool-card", "stock-table", "bubble", "modal", "scan-bar", "up", "down"]:
        if f".{cls}" not in css:
            problems.append(f"CSS 缺少类定义：.{cls}")

    print()
    if problems:
        print("发现问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    print("前端一致性检查通过：JS 引用的元素与样式全部存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
