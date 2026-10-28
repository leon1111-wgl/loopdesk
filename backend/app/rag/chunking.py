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


_CLAUSE_RE = re.compile(r"(?<=[，、：])|(?<=[,:])(?!\d)")  # "1,299元" and "10:30" stay whole
_GLUED_RE = re.compile(r"[0-9A-Za-z０-９.．,:%％¥￥$_\-/]")  # digits, Latin words, amounts, order ids
_HARD_CUT_OVERLAP = 40


def _safe_cut(text: str, start: int, end: int) -> int | None:
    """The last position in (start, end] with no digit, Latin letter or amount sign on either
    side. A cut at exactly ``end`` turned "每台199元" into "每台19" + "9元": the answer quoting
    the first half passed the evidence check, and "9元" raised a false knowledge conflict.
    A run of comma-separated numbers ("12345,12352,…") has no such position, so the second
    choice is just after a separator."""
    for i in range(end, start, -1):
        if i >= len(text) or not (_GLUED_RE.match(text[i - 1]) or _GLUED_RE.match(text[i])):
            return i
    for i in range(end, start, -1):
        if text[i - 1] in ",;: ":
            return i
    return None


def _hard_cut(clause: str, max_chars: int) -> list[str]:
    """Cut a clause with no clause mark into windows of at most ``max_chars``, each ending at a
    safe position; consecutive windows share up to ``_HARD_CUT_OVERLAP`` characters, so the
    sentence the cut went through is whole in at least one of them."""
    overlap = min(_HARD_CUT_OVERLAP, max_chars // 5)
    windows: list[str] = []
    start = last = 0
    while len(clause) - start > max_chars:
        # No safe position means one glued run longer than the cap: a length cut is all there is.
        cut = _safe_cut(clause, start, start + max_chars) or start + max_chars
        if cut <= last:  # the overlap leads straight back to the previous cut: drop the overlap
            start = last
            continue
        windows.append(clause[start:cut])
        last = cut
        back = _safe_cut(clause, max(start, cut - 2 * overlap), cut - overlap) if cut - start > 2 * overlap else None
        start = back or cut
    windows.append(clause[start:])
    return windows


def _split_long(unit: str, max_chars: int) -> list[str]:
    """A unit longer than ``max_chars`` (a comma-only paragraph, PDF text without full stops)
    is cut at clause marks, and whatever is still too long is cut by length (``_hard_cut``).
    Without this a 9,903-character paragraph became one chunk, and the answer quoting it was
    9,906 characters."""
    if len(unit) <= max_chars:
        return [unit]
    pieces: list[str] = []
    current = ""
    for clause in (c for c in _CLAUSE_RE.split(unit) if c):
        if len(clause) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            *windows, clause = _hard_cut(clause, max_chars)
            pieces.extend(windows)
        if len(current) + len(clause) > max_chars:
            pieces.append(current)
            current = ""
        current += clause
    if current:
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


def _units(lines: list[str], max_chars: int) -> list[str]:
    units: list[str] = []
    for line in lines:
        stripped = line.strip().lstrip(">").strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "*", "•", "|")) or re.match(r"^\d+[.、]", stripped):
            parts = [stripped]  # list items and table rows stay whole unless over the cap
        else:
            parts = split_sentences(stripped)
        for part in parts:
            units.extend(_split_long(part, max_chars))
    return units


def _pack(units: list[str], max_chars: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        if current and size + len(current) + len(unit) > max_chars:  # len(current): the newlines
            chunks.append("\n".join(current))
            current = current[-overlap:] if overlap else []
            size = sum(len(u) for u in current)
            if current and size + len(current) + len(unit) > max_chars:  # the overlap must not push a chunk past the cap
                current, size = [], 0
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
        for piece in _pack(_units(body, max_chars), max_chars, overlap):
            drafts.append(ChunkDraft(heading=heading, text=piece))
    return drafts


def chunk_pages(pages: list[str], max_chars: int = 300, overlap: int = 1) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    for number, page_text in enumerate(pages, start=1):
        for piece in _pack(_units(page_text.splitlines(), max_chars), max_chars, overlap):
            drafts.append(ChunkDraft(heading=f"第 {number} 页", text=piece, page=number))
    return drafts
