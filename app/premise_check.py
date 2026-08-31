"""
backend/premise_check.py — catch a false numeric premise in a DATA question.

A question can *assert* a quantity as already-true and ask about it:

    "How is construction progressing on the 1.71 L sanctioned PMAY-G houses?"
    "Of the ₹500 Cr released, how much is unspent?"
    "What share of the 520999 job cards were used?"

The SQL layer answers the *question* ("construction progress", "unspent",
"share") and ignores the asserted number entirely, so the composer is free to
repeat "1.71 L sanctioned" as fact even when the data says 2057 houses were
sanctioned. On a government dashboard that reads as the assistant endorsing a
wrong figure.

This module extracts each asserted quantity, checks it against the query
result, and — when the data contradicts it — returns a note string. The notes
ride the same `notes` channel `entity_resolver` already uses; `compose_response`
is told to lead with the correction instead of answering as though the premise
held.

Deterministic, no model call. Precision over recall: it only fires on a clear
declarative quantity against a small (1-3 row) result, and stays silent when it
cannot be sure.
"""
from __future__ import annotations

import decimal
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── scale words ────────────────────────────────────────────────────────────────
_SCALE = {
    "k": 1_000, "thousand": 1_000,
    "l": 100_000, "lac": 100_000, "lacs": 100_000, "lakh": 100_000, "lakhs": 100_000,
    "m": 1_000_000, "mn": 1_000_000, "million": 1_000_000, "millions": 1_000_000,
    "cr": 10_000_000, "crore": 10_000_000, "crores": 10_000_000,
    "bn": 1_000_000_000, "billion": 1_000_000_000, "billions": 1_000_000_000,
}

# A number, then an optional scale word. Single-letter scales (k / l / m, either
# case) are only honoured as a standalone token right after the digits — "1.71 L",
# "500k" — so a stray letter mid-word can't be read as a multiplier.
_QTY_RE = re.compile(
    r"(?<![\w.])"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)"
    r"\s*"
    r"(?P<scale>lakhs?|lacs?|crores?|thousand|millions?|billions?|mn|bn|cr|[KLM]\b|[klm](?=\s|$))?",
    re.IGNORECASE,
)

_YEAR_RE = re.compile(r"^(?:19|20|21)\d\d$")

# Words right before the number that mean it is NOT an asserted stock but a
# ranking cut, a comparison threshold, or a relative-time span.
_PRECEDING_STOPWORDS = {
    "top", "bottom", "first", "last", "past", "next", "latest", "recent", "previous",
    "over", "under", "above", "below", "least", "most", "than", "least",
    "up",  # "up to N"
}
_PRECEDING_STOP_PHRASES = ("more than", "less than", "at least", "at most", "up to",
                           "fewer than", "greater than", "no more than", "as many as")
# Words right after the number that mark it as a ranking length, not a stock.
_FOLLOWING_STOPWORDS = {
    "highest", "lowest", "largest", "smallest", "biggest", "top", "best", "worst",
    "leading", "years", "year", "months", "month", "days", "day", "weeks", "quarters",
}

# Determiners that flag the number as a *given* the question builds on.
_GIVEN_DETERMINERS = {"the", "those", "these", "that", "this", "all", "its", "their",
                      "of", "on", "among", "amongst", "from", "out", "these"}

# Metric vocabulary — only used to decide whether a small, scale-less number
# reads as an asserted stock ("the 900 houses sanctioned") rather than noise.
# NOT used to pick a column: a generated result often labels a column ambiguously
# (e.g. "House Sanctioned" is a *construction stage* in this schema, not the
# total sanctioned count), so matching a premise to a column by keyword produces
# confident but wrong "corrections". The check below compares against every
# value instead.
_ALL_KW = {
    "sanctioned", "sanction", "sanctions", "released", "release", "disbursed",
    "completed", "complete", "completions", "pending", "incomplete", "unspent",
    "approved", "allotted", "allocated", "proposed", "registered", "geotagged",
    "expenditure", "spent", "utilised", "utilized", "persondays",
    "houses", "house", "housing", "dwelling", "dwellings", "units",
    "beneficiaries", "households", "persons", "people",
    "amount", "rupees", "crore", "crores", "lakh", "lakhs", "money", "funds",
    "jobcards", "cards", "works", "assets", "villages", "workers",
}

# Multi-word metric phrases, normalised to the single tokens above so window
# scanning catches them.
_PHRASE_NORMALISE = [
    (re.compile(r"job\s*cards?", re.I), "jobcards"),
    (re.compile(r"person[\s-]?days?", re.I), "persondays"),
]

_WORD_RE = re.compile(r"[a-z][a-z-]*", re.I)


@dataclass
class Premise:
    value: float          # the asserted quantity, scale expanded
    text: str             # the span as the user wrote it, e.g. "1.71 L"
    keywords: list[str] = field(default_factory=list)   # metric words around it


def _normalise_phrases(text: str) -> str:
    for pat, repl in _PHRASE_NORMALISE:
        text = pat.sub(repl, text)
    return text


def _to_number(value: object) -> "int | float | None":
    """Coerce a result cell to a real number, or None. Local copy so this module
    has no import edge back into pipeline.py."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, decimal.Decimal):
        f = float(value)
        return int(f) if f.is_integer() else f
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
            return int(s) if "." not in s else float(s)
    return None


def _window_keywords(text: str, start: int, end: int, *, before: int = 4, after: int = 4) -> list[str]:
    """Metric words within `before` tokens left and `after` tokens right of the
    number span [start, end)."""
    left_words = _WORD_RE.findall(text[:start].lower())[-before:]
    right_words = _WORD_RE.findall(text[end:].lower())[:after]
    found: list[str] = []
    for w in (*left_words, *right_words):
        if w in _ALL_KW and w not in found:
            found.append(w)
    return found


def _is_asserted_quantity(text: str, m: re.Match, value: float, has_scale: bool) -> bool:
    """True when the matched number reads as a stock the question takes as given,
    not a ranking length / threshold / time span / year."""
    raw = m.group("num").replace(",", "")
    if _YEAR_RE.match(raw) and not has_scale:
        return False
    # a percentage is a rate, handled elsewhere
    tail = text[m.end():m.end() + 8].lstrip()
    if tail.startswith("%") or tail[:7].lower().startswith("percent"):
        return False

    left = text[:m.start()].lower()
    left_words = _WORD_RE.findall(left)
    prev1 = left_words[-1] if left_words else ""
    prev2 = " ".join(left_words[-2:]) if len(left_words) >= 2 else ""
    if prev1 in _PRECEDING_STOPWORDS or prev2 in _PRECEDING_STOP_PHRASES:
        return False

    right_words = _WORD_RE.findall(text[m.end():].lower())
    nxt = right_words[0] if right_words else ""
    if nxt in _FOLLOWING_STOPWORDS:
        return False

    has_separator = "," in m.group("num")
    kw = _window_keywords(text, m.start(), m.end())
    given_ctx = prev1 in _GIVEN_DETERMINERS or (len(left_words) >= 2 and left_words[-2] in _GIVEN_DETERMINERS)

    # Accept when the magnitude alone says "stock" (a scale word, a thousands
    # separator, or >= 1000), or when a metric word sits next to it AND it is
    # introduced as a given ("the 900 houses sanctioned").
    if has_scale or has_separator or value >= 1000:
        return bool(kw) or has_scale or has_separator or value >= 10_000
    return bool(kw) and given_ctx


def extract_premises(question: str) -> list[Premise]:
    q = _normalise_phrases(question or "")
    out: list[Premise] = []
    for m in _QTY_RE.finditer(q):
        try:
            base = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        scale_tok = (m.group("scale") or "").lower().strip()
        has_scale = bool(scale_tok)
        value = base * _SCALE.get(scale_tok, 1) if has_scale else base
        if value <= 0:
            continue
        if not _is_asserted_quantity(q, m, value, has_scale):
            continue
        span = question[m.start():m.end()].strip() if len(question) >= m.end() else m.group(0).strip()
        out.append(Premise(value=value, text=span or m.group(0).strip(),
                           keywords=_window_keywords(q, m.start(), m.end())))
    return out


def _pretty_col(col: str) -> str:
    return col.replace("_", " ").strip().title()


def _fmt(v: "int | float") -> str:
    if isinstance(v, float) and not v.is_integer():
        return f"{v:,.2f}"
    return f"{int(v):,}"


def _close(a: float, b: float) -> bool:
    """Within 2% of the larger magnitude, or half a unit — tolerant of the
    rounding baked into '1.71 L' but not of an order-of-magnitude gap."""
    hi = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(0.02 * hi, 0.5)


def check_premises(question: str, rows: list[dict]) -> list[str]:
    """Notes for any asserted quantity in `question` the result contradicts.
    Empty list when there is no premise, the result is too large to reason about
    cell-by-cell, or every premise is consistent with the data."""
    premises = extract_premises(question)
    if not premises:
        return []
    if not rows or len(rows) > 3:
        return []

    cells: list[tuple[str, "int | float"]] = []
    seen: set[tuple[str, float]] = set()
    for row in rows:
        for col, val in row.items():
            n = _to_number(val)
            if n is None:
                continue
            key = (col, float(n))
            if key in seen:
                continue
            seen.add(key)
            cells.append((col, n))
    if not cells:
        return []

    notes: list[str] = []
    for p in premises:
        # Consistent as long as SOME figure in the result is close to the asserted
        # number — deliberately lenient. A column whose label seems to disagree
        # (e.g. a "House Sanctioned" *stage* count vs. the total sanctioned house
        # count) is not evidence the premise is wrong.
        if any(_close(n, p.value) for _c, n in cells):
            continue

        near_c, near_n = min(cells, key=lambda cn: abs(cn[1] - p.value))
        notes.append(
            f'The question treats "{p.text}" (~{_fmt(p.value)}) as an established figure, but '
            f'no value in this result is close to it (nearest: {_pretty_col(near_c)} = '
            f'{_fmt(near_n)}). Do not restate "{p.text}" as fact; answer from the figures the '
            f'query actually returned and note that the assumed number is not supported here.'
        )
    return notes
