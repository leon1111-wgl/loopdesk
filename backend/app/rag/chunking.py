"""Document parsing and heading-aware chunking.

Chunks never cross a heading boundary, and every chunk remembers its heading path
("售后与退款政策 > 退款到账时间"). The heading is indexed together with the text, which
fixes the classic failure where a chunk says "3-7 个工作日" but not *what* takes 3-7 days.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from ..textutil import split_sentences

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt", ".pdf")


@dataclass
class ChunkDraft:
    heading: str
    text: str
    page: int | None = None


class UnsupportedFile(ValueError):
    pass


def parse_upload(filename: str, data: bytes) -> tuple[str, list[str] | None]:
    """Return (full_text, pages). ``pages`` is set for PDFs so chunks keep page numbers."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        return "\n\n".join(pages), pages
    if lower.endswith((".md", ".markdown", ".txt")):
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return data.decode(encoding), None
            except UnicodeDecodeError:
                continue
        raise UnsupportedFile("无法识别文件编码，请转为 UTF-8")
    raise UnsupportedFile(f"暂不支持该文件类型，支持：{', '.join(SUPPORTED_SUFFIXES)}")


def _units(lines: list[str]) -> list[str]:
    units: list[str] = []
    for line in lines:
        stripped = line.strip().lstrip(">").strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "*", "•", "|")) or re.match(r"^\d+[.、]", stripped):
            units.append(stripped)  # list items and table rows stay whole
        else:
            units.extend(split_sentences(stripped))
    return units


def _pack(units: list[str], max_chars: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(unit) > max_chars:
            chunks.append("\n".join(current))
            current = current[-overlap:] if overlap else []
            size = sum(len(u) for u in current)
        current.append(unit)
        size += len(unit)
    if current:
        chunks.append("\n".join(current))
    return chunks


def chunk_markdown(text: str, max_chars: int = 300, overlap: int = 1) -> list[ChunkDraft]:
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, list[str]]] = []
    lines: list[str] = []

    def flush() -> None:
        if any(line.strip() for line in lines):
            sections.append((" > ".join(title for _, title in stack), list(lines)))
        lines.clear()

    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
        else:
            lines.append(line)
    flush()

    drafts: list[ChunkDraft] = []
    for heading, body in sections:
        for piece in _pack(_units(body), max_chars, overlap):
            drafts.append(ChunkDraft(heading=heading, text=piece))
    return drafts


def chunk_pages(pages: list[str], max_chars: int = 300, overlap: int = 1) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for number, page_text in enumerate(pages, start=1):
        for piece in _pack(_units(page_text.splitlines()), max_chars, overlap):
            drafts.append(ChunkDraft(heading=f"第 {number} 页", text=piece, page=number))
    return drafts
