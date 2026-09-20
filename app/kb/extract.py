"""知识库文档解析：把各种格式的文档抽成纯文本。

零第三方依赖，能处理的格式：
    .txt / .md / .markdown   直接读（自动识别 UTF-8 / GBK）
    .csv / .tsv              逐行当文本（保留表头与数值）
    .json / .jsonl           转成可读文本
    .docx                    zipfile + XML 解析正文（含标题层级）
    .doc（旧版 Word 二进制）  不支持，提示用户另存为 .docx

其他格式（.pdf / .xlsx 等）需要额外依赖，这里明确拒绝并给出提示，
而不是静默导入一堆乱码。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

SUPPORTED_EXTENSIONS = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".docx")

_WS_RE = re.compile(r"[ \t\u3000]+")
_BLANK_RE = re.compile(r"\n{3,}")


class ExtractError(RuntimeError):
    """文档解析失败。"""


def _decode(raw: bytes) -> str:
    """尽力把字节解成文本（中文文档常见 GBK）。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    return _BLANK_RE.sub("\n\n", text).strip()


def _extract_docx(raw: bytes) -> str:
    """从 .docx 里抽正文，标题行前面加 # 便于后续按章节切分。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise ExtractError("这个 .docx 文件读不出来（可能已损坏）") from exc

    lines: list[str] = []
    # 按段落切，段落里再抽所有 <w:t> 文本
    for para in re.findall(r"<w:p[ >].*?</w:p>|<w:p/>", xml, flags=re.S):
        style = re.search(r'w:pStyle\s+w:val="([^"]+)"', para)
        text = "".join(re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, flags=re.S))
        text = re.sub(r"<[^>]+>", "", text)
        text = (text.replace("&amp;", "&").replace("&lt;", "<")
                    .replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'"))
        text = text.strip()
        if not text:
            continue
        if style and re.match(r"(?i)^(heading|标题)\s*([1-6])?", style.group(1)):
            level_match = re.search(r"(\d)", style.group(1))
            level = int(level_match.group(1)) if level_match else 1
            lines.append("#" * min(6, max(1, level)) + " " + text)
        else:
            lines.append(text)
    if not lines:
        raise ExtractError("这个 .docx 里没有抽到文字（可能是纯图片文档）")
    return "\n\n".join(lines)


def _extract_json(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text          # jsonl 或格式不规范，按纯文本处理
    return json.dumps(data, ensure_ascii=False, indent=2)


def extract(filename: str, raw: bytes) -> str:
    """把文件内容抽成归一化后的纯文本。"""
    suffix = Path(filename).suffix.lower()

    if suffix == ".docx":
        return _normalize(_extract_docx(raw))

    if suffix == ".doc":
        raise ExtractError("旧版 .doc 不支持，请用 Word 另存为 .docx 后再导入")

    if suffix in (".pdf", ".xlsx", ".xls", ".pptx"):
        raise ExtractError(
            f"{suffix} 需要额外的解析库，当前不支持。"
            "可以先把内容复制成 .txt / .md，或另存为 .docx 再导入"
        )

    if suffix not in SUPPORTED_EXTENSIONS:
        raise ExtractError(
            f"不支持的格式 {suffix or '(无扩展名)'}；"
            "可用格式：" + "、".join(SUPPORTED_EXTENSIONS)
        )

    text = _decode(raw)
    if suffix in (".json", ".jsonl"):
        text = _extract_json(text)

    text = _normalize(text)
    if not text:
        raise ExtractError("文件内容为空")
    return text
