"""The tenant's hotwords: plan and product names an ASR model has never seen spelled this way.

They come from data the tenant already maintains: the domain words (textutil.DOMAIN_WORDS) that
its knowledge base actually uses, and the names of the things it sells (order items). Three ways
to use them, measured separately in the voice evaluation:

- model biasing: SeACo-Paraformer takes a hotword list; Whisper takes them in its prompt;
- text correction after any engine: a span whose pinyin equals a hotword's becomes the hotword
  ("好华会员" -> "豪华会员"), plus explicit aliases for words with Latin letters, which pinyin
  cannot cover. Aliases may only come from synthetic speech whose engine read the Latin word
  itself (not from Kokoro, whose script respells it), and are to be checked on held-out real
  speech once there is some, never tuned on it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Chunk, Order, Tenant
from ..textutil import DOMAIN_WORDS

_SPLIT = re.compile(r"[\s（）()《》【】\[\]、，,·]+")
_CJK = re.compile(r"[一-鿿]")
# What Chinese ASR models write for a Latin-letter brand word. Filled from the synthetic set's
# errors (python -m app.evals.voice report --suggest-aliases); empty means pinyin matching only.
ALIASES: dict[str, list[str]] = {}


def _name_parts(name: str) -> list[str]:
    parts = []
    for part in _SPLIT.split(name):
        part = re.sub(r"\d+个$", "", part).strip()
        if len(part) >= 2 and (_CJK.search(part) or len(part) >= 3) and not part.isdigit():
            parts.append(part)
    return parts


def tenant_terms(db: Session, tenant: Tenant, limit: int = 60) -> list[str]:
    """Hotwords for one tenant, longest first (a SeACo list works best short: keep it bounded)."""
    text = "\n".join(db.execute(select(Chunk.text).where(Chunk.tenant_id == tenant.id).limit(2000)).scalars())
    terms = {w for w in DOMAIN_WORDS if w in text}
    names = db.execute(select(Order.items).where(Order.tenant_id == tenant.id).order_by(Order.paid_at.desc())
                       .limit(500)).scalars()
    for items in names:
        for item in items or []:
            terms.update(_name_parts(str(item.get("name", ""))))
    return sorted(terms, key=lambda t: (-len(t), t))[:limit]


@lru_cache(maxsize=4096)
def _pinyin(text: str) -> tuple[str, ...]:
    try:
        from pypinyin import lazy_pinyin
    except ImportError:  # pragma: no cover - pypinyin is in requirements.txt
        return ()
    return tuple(_fuzzy(p) for p in lazy_pinyin(text))


def _fuzzy(syllable: str) -> str:
    """Merge the distinctions southern accents and ASR models blur most: zh/z, ch/c, sh/s, n/l, -ng/-n."""
    s = syllable.lower()
    for a, b in (("zh", "z"), ("ch", "c"), ("sh", "s")):
        if s.startswith(a):
            s = b + s[len(a):]
    if s.startswith("n"):
        s = "l" + s[1:]
    if s.endswith("ng"):
        s = s[:-1]
    return s


@dataclass
class Lexicon:
    terms: list[str]
    aliases: dict[str, list[str]] = field(default_factory=dict)

    def correct(self, text: str) -> tuple[str, list[tuple[str, str]]]:
        """Replace misheard hotwords; returns the text and the (heard, replaced-by) pairs."""
        fixes: list[tuple[str, str]] = []
        for term in self.terms:
            for alias in self.aliases.get(term, []):
                if alias in text:
                    text = text.replace(alias, term)
                    fixes.append((alias, term))
        for term in self.terms:
            if len(term) < 3 or not all(_CJK.match(c) for c in term):
                continue  # two-character words collide too often; Latin words need aliases
            target = _pinyin(term)
            if not target:
                break
            n, i, out = len(term), 0, []
            while i < len(text):
                window = text[i:i + n]
                if len(window) == n and window != term and all(_CJK.match(c) for c in window) and _pinyin(window) == target:
                    out.append(term)
                    fixes.append((window, term))
                    i += n
                else:
                    out.append(text[i])
                    i += 1
            text = "".join(out)
        return text, fixes


_CACHE: dict[tuple[int, int], tuple[float, Lexicon]] = {}
CACHE_SECONDS = 600.0  # product names change with orders; the knowledge base bumps kb_version


def lexicon_for(db: Session, tenant: Tenant) -> Lexicon:
    """Cached per tenant and knowledge version: building it reads the tenant's chunks and recent orders."""
    key = (tenant.id, tenant.kb_version)
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    terms = tenant_terms(db, tenant)
    lexicon = Lexicon(terms, {t: ALIASES[t] for t in terms if t in ALIASES})
    _CACHE[key] = (time.monotonic(), lexicon)
    return lexicon
