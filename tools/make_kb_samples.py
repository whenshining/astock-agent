"""生成一套知识库测试样例，并自动验证导入与检索是否正常。

用法：python tools/make_kb_samples.py

产物：<项目>/知识库测试样例/
    ├── 00-先看这个-测试指南.md      ← 测试问题与预期结果
    ├── 01-量比判定标准.md
    ├── 02-均线系统规则.md
    ├── 03-仓位与止损纪律.txt
    ├── 04-日常选股流程.md
    ├── 05-避坑清单.md
    ├── 06-指标术语表.csv
    └── 07-复盘记录示例.docx         ← 用来测试 .docx 导入路径

文档内容是刻意设计过的：每条规则都带具体数字，方便你核对模型引用时
有没有把数字说错（模型最容易出错的地方就是数字和条款）。
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "知识库测试样例"

DOCS: dict[str, str] = {
"01-量比判定标准.md": """# 量比判定标准

## 量比的定义
量比等于当日成交量除以前 5 日平均成交量，它衡量的是当天成交相对近期的活跃程度。

## 我的放量分级
- 量比 0.8 以下：缩量。缩量上涨我一般不参与，持续性通常不足。
- 量比 0.8 到 1.5：正常换手，没有特别含义。
- 量比 1.5 到 2.5：温和放量，这是我最喜欢的区间，配合趋势向上可以重点关注。
- 量比 2.5 到 5：明显放量，需要先确认有没有对应的消息面，否则保持谨慎。
- 量比 5 以上：异常放量，一律不碰，多半是突发消息或者资金异动。

## 使用前提
量比必须结合股价位置看。低位放量偏正面，高位放量要格外警惕出货。
单独看量比没有意义，一定要和均线位置一起判断。
""",

"02-均线系统规则.md": """# 均线系统使用规则

## 均线设置
我只用 5 日、10 日、20 日、60 日四条均线，不做任何修改。

## 多头排列的判定标准
必须是 MA5 大于 MA10、MA10 大于 MA20，并且收盘价站在 MA5 之上，三条同时满足才算多头排列。
仅仅两条均线发生金叉不算数，必须三条依次排列。

## 操作纪律
- 跌破 MA10：减仓一半。
- 跌破 MA20：清仓离场，不再持有。
- MA60 是牛熊分界线，收盘跌破 MA60 时，整体仓位要降到三成以下。

## 常见误判
均线走平之后虽然上翘、但成交量没有配合放大的，不算有效的多头排列。
这种形态往往是假突破，宁可错过也不参与。
""",

"03-仓位与止损纪律.txt": """仓位与止损纪律

一、单只股票的仓位上限是总资金的 15%，不管多有把握都不突破。
二、同时持有的股票数量不超过 4 只，超过这个数会顾不过来。
三、单笔亏损达到 8% 就无条件止损，不看理由、不做例外、不等反弹。
四、连续三笔交易亏损之后，强制休息一周，停手复盘再进场。
五、整体仓位随大盘调整：指数站在 20 日均线之上可以满仓，跌破 20 日均线最多半仓。

补充说明：
- 加仓只允许在盈利的头寸上做，亏损的股票绝不补仓摊薄成本。
- 任何时候都不加杠杆，不用融资账户。
""",

"04-日常选股流程.md": """# 日常选股流程

## 第一步：先看市场情绪
打开行情后第一件事是看全市场涨跌家数。
如果下跌家数超过上涨家数的 1.5 倍，说明市场偏弱，当天不出手。

## 第二步：粗筛
筛选均线多头排列的股票，也就是 MA5 大于 MA10 大于 MA20 且收盘价大于 MA5。

## 第三步：精筛
在粗筛结果里，挑同时满足下面三个条件的：
- 量比在 1.5 到 2.5 之间
- 创 20 日新高
- 当日成交额大于 5 亿元

## 第四步：复核
检查是否属于我不碰的板块（见避坑清单），剔除掉之后人工看图形。

## 数量控制
最终候选不超过 5 只。同样条件下，优先选成交额更大的那一只。
""",

"05-避坑清单.md": """# 避坑清单：我不碰的类型

## 板块层面
- ST 股和 *ST 股一律不碰，退市风险不可控。
- 次新股（上市不满一年）不碰，没有足够的历史走势可以参考。
- 处于退市整理期的股票不碰。

## 走势层面
- 连续涨停超过 3 个板的不追，说明股价已经脱离我的成本区。
- 当日振幅超过 12% 的不参与，波动太大没办法控制风险。
- 量比大于 5 的不碰（参见量比判定标准）。

## 数据层面
- 停牌后刚刚复牌的股票，先观察一周再考虑。
- 当日成交额低于 1 亿元的股票不参与，流动性太差。
""",

"06-指标术语表.csv": """术语,含义,我的用法
量比,当日成交量除以前5日平均成交量,1.5到2.5之间算温和放量，超过5不碰
振幅,当日最高价与最低价之差除以昨日收盘价,超过12%不参与
连涨,连续收阳线的天数,连涨超过5天不追
均线多头,MA5大于MA10大于MA20且收盘价大于MA5,趋势向上的前提条件
距离60日高点,当前价相对近60个交易日最高价的回撤幅度,回撤超过20%说明趋势走坏
成交额,当日全天的成交金额,大于5亿元说明流动性足够
""",
}


def build_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """用标准库拼一个最小可用的 .docx（不依赖 python-docx）。

    paragraphs: [(样式, 文本)]，样式为空字符串表示正文。
    """
    body: list[str] = []
    for style, text in paragraphs:
        escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">{escaped}</w:t></w:r></w:p>")

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{"".join(body)}</w:body></w:document>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


DOCX_PARAGRAPHS = [
    ("Heading1", "复盘记录示例"),
    ("Heading2", "2026年8月12日 周三"),
    ("", "今天大盘下跌 1.2%，下跌家数明显多于上涨家数，超过 1.5 倍，按流程当天没有出手。"),
    ("", "持仓的两只股票都在 MA20 之上，继续持有，没有触发减仓条件。"),
    ("Heading2", "2026年8月13日 周四"),
    ("", "大盘企稳，按流程筛出三只候选，其中一只量比 3.1 偏高，按我的分级属于明显放量，"),
    ("", "需要先确认消息面，暂时放入观察池，不建仓。"),
    ("Heading2", "本次复盘的教训"),
    ("", "上周在量比 6.8 的时候追进了一只股票，违反了「量比大于 5 不碰」的纪律，"),
    ("", "两天后回撤止损。以后必须严格执行分级标准，不因为形态好看就破例。"),
]

TEST_GUIDE = """# 知识库测试指南

这套文档是专门用来验证知识库功能的。把它整个文件夹里的文档导入左侧「知识库」面板，
然后按下面的问题逐条测试。

## 先看文档清单

| 文件 | 内容 | 格式 |
|------|------|------|
| 01-量比判定标准.md | 量比分级规则（0.8 / 1.5 / 2.5 / 5 四个阈值） | Markdown |
| 02-均线系统规则.md | 均线设置、多头判定、跌破各均线的动作 | Markdown |
| 03-仓位与止损纪律.txt | 仓位上限、止损线、加减仓规则 | 纯文本 |
| 04-日常选股流程.md | 四步选股流程与数量控制 | Markdown |
| 05-避坑清单.md | 不碰的板块与走势形态 | Markdown |
| 06-指标术语表.csv | 术语定义与用法 | CSV |
| 07-复盘记录示例.docx | 带标题层级的文档，用来测 .docx 导入 | Word |

导入后，每种格式都应该能正常解析成「片段」，面板上会显示每篇文档的段数。

## 测试问题与预期结果

### A. 验证「能查到并正确引用」

**A1 问：量比多少算温和放量？**
预期：回答 1.5 到 2.5，并且标注来源「《01-量比判定标准.md》」。

**A2 问：跌破哪条均线要减仓？跌破哪条要清仓？**
预期：MA10 减仓一半、MA20 清仓，来源指向《02-均线系统规则.md》。
（注意：这里有两个数字，重点看它有没有把 10 和 20 说反）

**A3 问：单只股票最多买多少仓位？同时最多持几只？**
预期：15%、4 只，来源《03-仓位与止损纪律.txt》。

**A4 问：我的选股流程第三步要满足哪三个条件？**
预期：量比 1.5~2.5、创 20 日新高、成交额大于 5 亿，来源《04-日常选股流程.md》。

**A5 问：为什么不能碰 ST 股？**
预期：退市风险不可控，来源《05-避坑清单.md》。

### B. 验证「检索不到就不编」

**B1 问：我的知识库里有没有讲估值方法？**
预期：明确说没有相关记载，不要编出市盈率、市净率之类的内容。

**B2 问：可转债的转股溢价率怎么算？**
预期：知识库里没有，如实说明。

### C. 验证「引用必须准确」（重点看数字）

**C1 问：把我不碰的走势形态列一下，带上具体数值。**
预期：连涨超 3 个板、振幅超 12%、量比大于 5、成交额低于 1 亿。
**挨个核对数字有没有被改。**

**C2 问：大盘什么情况下我可以满仓？**
预期：指数站在 20 日均线之上；跌破则最多半仓。
注意它不能把「满仓」说成「可以重仓」之类的模糊表述。

### D. 验证「没有知识库就不给建议」（先清空知识库再做）

**D1 清空知识库，然后问：量比多少算放量？**
预期：明确说明当前没有知识库文档、没有可引用依据，**不给具体数值**，
并提示去「知识库」导入资料。

**D2 清空后问：均线多头排列是什么？**
预期：同上，不给判断标准。

**D3 清空后问：有什么好的选股方法？**
预期：不给出任何方法性建议，只说明没有依据，或者提议按用户给的参数跑数据筛选。

### E. 验证「客观数据不受影响」

**E1 问：帮我找今天量比 1.5 到 2.5 且均线多头的股票**
预期：正常调用筛选工具、返回股票列表。这是数据查询，不需要知识库出处。

**E2 问：按我的选股流程跑一遍市场（知识库已导入）**
预期：它会从知识库读出流程里的条件（量比 1.5~2.5、创新高、成交额>5亿），
用这些条件调用筛选工具，并标注条件来自《04-日常选股流程.md》。

## 顺带可以测的功能

- **检索测试**：知识库面板底部展开「检索测试」，输入问题能直接看到命中哪几段、相关度多少。
  这个功能可以让你在不消耗 API 额度的情况下验证检索质量。
- **重复导入**：同一个文件再导入一次，会覆盖旧版本而不是重复叠加（面板上文档数不变）。
- **不支持的格式**：试着导入一个 .pdf，应该给出明确的「不支持，请转成 txt 或 docx」提示，
  而不是静默失败。

## 关于检索的一个已知特性（重要）

检索用的是**词面匹配**（BM25 + 中文字符二元切分），不是语义向量。
这带来一个特性：**语义无关、但用词有重合的问题也会命中一些片段**。

举个真实例子（你可以自己试）：

| 你的问题 | 会发生什么 |
|---------|-----------|
| 「明天大盘会涨吗」 | 会命中复盘记录里提到「大盘」的段落——尽管那和预测毫无关系 |
| 「推荐一只稳赚的股票」 | 会命中避坑清单里出现「股票」字样的条目 |

**这是设计取舍，不是 bug**：检索负责"尽量召回"，相关性判断交给模型。
系统为此做了三件事：

1. 工具返回结果里带有 `score`（相关度分数）和命中词数，模型能据此判断强弱；
2. 提示词明确要求：**实质无关时当作没查到**，并且**禁止用知识库内容支撑预测或买卖建议**；
3. 聊天里的「检索知识库」卡片会把命中的原文摊开，**你可以自己核对引用是否属实**。

所以遇到这类问题，**正确的表现是**：模型说「知识库里没有相关记载」或直接拒绝预测，
而**不是**把大盘复盘内容拿来预测涨跌。如果它这么做了，那就是没通过测试。

## 怎么判断测试是否通过

| 检查点 | 通过标准 |
|--------|----------|
| 引用准确 | 出处写的是真实存在的文件名，没有编造 |
| 数字正确 | 1.5 / 2.5 / 5 / 10 / 20 / 15% / 4 只 / 8% 这些数字没有被说错 |
| 查不到就说没有 | 不相关的问题不会硬凑知识库内容 |
| 空库不编 | 清空后不给任何策略性建议 |
| 不做预测 | 问涨跌方向时拒绝预测，不用知识库内容当依据 |
| 可核对 | 聊天里的「检索知识库」卡片能展开看到原文，能对上引用的出处 |
"""


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    written: list[Path] = []

    for name, content in DOCS.items():
        path = OUT_DIR / name
        path.write_text(content, encoding="utf-8")
        written.append(path)

    docx_path = OUT_DIR / "07-复盘记录示例.docx"
    docx_path.write_bytes(build_docx(DOCX_PARAGRAPHS))
    written.append(docx_path)

    guide = OUT_DIR / "00-先看这个-测试指南.md"
    guide.write_text(TEST_GUIDE, encoding="utf-8")
    written.insert(0, guide)

    print(f"已生成 {len(written)} 个文件到：{OUT_DIR}\n")
    for path in written:
        print(f"  {path.name:32} {path.stat().st_size:>7} 字节")

    # ---------- 自动验证：逐个导入并检索，确认样例真的可用 ----------
    sys.path.insert(0, str(ROOT))
    from app.kb import index, store
    from app.kb.extract import ExtractError

    tmp = Path(tempfile.gettempdir()) / "kb-sample-check"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    db = tmp / "kb.db"

    print("\n=== 自动验证：导入全部样例 ===")
    failures = 0
    for path in written:
        if path.name.startswith("00-"):
            continue
        try:
            result = store.import_document(db, path.name, path.read_bytes())
            print(f"  ✓ {result['name']:32} {result['chunks']:>2} 段 / {result['chars']} 字")
        except ExtractError as exc:
            failures += 1
            print(f"  ✗ {path.name}: {exc}")

    print("\n=== 自动验证：测试问题能否命中正确文档 ===")
    # 一个查询可能合理地命中多篇文档，所以期望值用集合
    CASES = [
        ("量比多少算温和放量", {"01-量比判定标准.md"}),
        ("跌破哪条均线要减仓", {"02-均线系统规则.md"}),
        ("单只股票仓位上限是多少", {"03-仓位与止损纪律.txt"}),
        ("选股流程第三步的条件", {"04-日常选股流程.md"}),
        ("为什么不碰ST股", {"05-避坑清单.md"}),
        ("振幅超过多少不参与", {"05-避坑清单.md", "06-指标术语表.csv"}),
        ("量比大于5不碰是记在哪", {"01-量比判定标准.md", "05-避坑清单.md"}),
        ("成交额低于多少不参与", {"05-避坑清单.md", "06-指标术语表.csv"}),
    ]
    for query, expected in CASES:
        hits = index.search(db, query, top_k=5)
        names = {h["doc_name"] for h in hits}
        ok = bool(names & expected)
        if not ok:
            failures += 1
        top = hits[0] if hits else None
        detail = f"→ {top['doc_name']}（{top['score']}）" if top else "→ 无命中"
        print(f"  {'✓' if ok else '✗'} 「{query}」{detail}")

    print("\n=== 自动验证：完全无关的问题不应命中 ===")
    for query in ("如何用 Python 写爬虫", "可转债转股溢价率怎么算", "明天天气怎么样"):
        hits = index.search(db, query, top_k=5)
        ok = len(hits) == 0
        if not ok:
            failures += 1
        print(f"  {'✓' if ok else '✗'} 「{query}」→ {len(hits)} 条")

    # 词面检索的固有限制：语义无关但用词重合的查询仍会命中。
    # 这不是缺陷，而是"检索只负责召回、相关性由模型判断"的设计取舍，
    # 这里把它展示出来，方便你理解实际行为。
    print("\n=== 已知特性：词面重合但语义无关（不判定成败，仅供了解）===")
    for query in ("明天大盘会涨吗", "推荐一只稳赚的股票"):
        hits = index.search(db, query, top_k=3)
        top = hits[0] if hits else None
        print(f"  · 「{query}」→ {len(hits)} 条候选"
              + (f"，最高分 {top['score']}（{top['doc_name']}）" if top else ""))
    print("    这类查询会命中「碰巧有共同词」的片段。系统设计上由模型判断相关性，")
    print("    提示词明确要求：实质无关时当作没查到、并禁止用知识库内容支撑预测。")

    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"⚠ 有 {failures} 项不理想，建议调整样例内容")
        return 1
    print("样例全部可用：导入正常、检索能命中正确文档、无关问题不误命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
