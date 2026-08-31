"""
Next-step suggestions shown under a completed answer ("Next steps" chips).

DETERMINISTIC, not model-generated. Every suggestion is assembled from a fixed,
scheme-aware bank keyed on:
  - the route that just answered (data vs knowledge),
  - which scheme(s) the answer covered,
  - what the question actually asked for (which metric words it used),
  - which entities resolved (district / block / village / year).

The generated SQL is also read (just its GROUP BY) so a suggestion respects the
grain the answer is already at: never "break that down by district" under a
table that is *already* per-district, and the sibling-metric suggestion keeps
the same "by district" cut instead of collapsing back to a state total.

Suggestions stay the same *kind* as the question that was just asked: a metric
(data) answer only ever offers more metric questions, and a general (knowledge)
answer only ever offers more general questions — no eligibility chip under a
number, no "how many houses" chip under a rules explainer.

So a suggestion is always a real question this service can answer against the
known curated schema or the reference KB — no model call, no latency, no
invented metric, and no "random" drift between turns. It returns a list of
{"label", "question"} (at most `_MAX`): `label` is what the user reads,
`question` is the exact text re-sent on click (the SQL / RAG layer parses it,
so it must stay literal, not softened).

Edge / denied / clarification turns get nothing here — those routes carry their
own next-step UI (starter chips on edge, the clarification options).
"""
import re

_MAX = 3

_SCHEME_RX = {
    "MGNREGA": re.compile(r"\bmgnrega\b|\bmnrega\b|\bnrega\b", re.IGNORECASE),
    "PMAY-G": re.compile(r"\bpmay[\s-]?g?\b|\bawa+s?\b", re.IGNORECASE),
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def _asked(question: str, *needles: str) -> bool:
    ql = _norm(question)
    return any(n in ql for n in needles)


def _primary_schemes(schemes: list[str], question: str) -> list[str]:
    """The scheme(s) to build follow-ups for: what the answer used, else what
    the question names, else both."""
    schemes = [s for s in (schemes or []) if s in _SCHEME_RX]
    if schemes:
        return schemes
    named = [s for s, rx in _SCHEME_RX.items() if rx.search(question or "")]
    return named or ["MGNREGA", "PMAY-G"]


# ── answer grain (read off the generated SQL's GROUP BY) ────────────────────
_LEVEL_RANK = {"state": 0, "district": 1, "block": 2, "village": 3}

_GROUPBY_RX = re.compile(
    r"group\s+by\s+(.+?)(?:\border\s+by\b|\blimit\b|\bhaving\b|\)|;|$)",
    re.IGNORECASE | re.DOTALL,
)
_DISTRICT_COL_RX = re.compile(r"district|dist_?name", re.IGNORECASE)
_BLOCK_COL_RX = re.compile(r"\bblock\b|block_?name|lgd_block", re.IGNORECASE)
_VILLAGE_COL_RX = re.compile(r"village|lgd_village|panchayat", re.IGNORECASE)


def _answer_grain(sql: str | None) -> str | None:
    """The finest geography level the answer is grouped to, read off the SQL's
    GROUP BY (this is the same signal the UI shows as its 'GROUP BY lgd_district'
    source chip). None when the query returns a single aggregate row."""
    if not sql:
        return None
    m = _GROUPBY_RX.search(sql)
    if not m:
        return None
    cols = m.group(1)
    if _VILLAGE_COL_RX.search(cols):
        return "village"
    if _BLOCK_COL_RX.search(cols):
        return "block"
    if _DISTRICT_COL_RX.search(cols):
        return "district"
    return None


def _filter_level(ents: dict) -> str:
    """The finest geography the question is *filtered* to (one named area)."""
    if ents.get("village_code") or ents.get("village"):
        return "village"
    if ents.get("block"):
        return "block"
    if ents.get("district"):
        return "district"
    return "state"


def _view_level(ents: dict, grain: str | None) -> str:
    """What the user is actually looking at: the finer of the area they filtered
    to and the grain the answer is grouped by."""
    flt = _filter_level(ents)
    if grain and _LEVEL_RANK.get(grain, 0) > _LEVEL_RANK[flt]:
        return grain
    return flt


def _grain_suffix(ents: dict, grain: str | None) -> str:
    """', by district' / ', by block' — only when the answer is grouped finer
    than any area filter, so a sibling-metric suggestion keeps the same cut."""
    flt = _filter_level(ents)
    if grain and _LEVEL_RANK.get(grain, 0) > _LEVEL_RANK[flt]:
        return f", by {grain}"
    return ""


def _top_area_from_rows(rows: list | None, col_rx: "re.Pattern") -> str | None:
    """Name of the row with the largest metric value, keyed by the first column
    whose name matches `col_rx`. None if the row shape isn't obvious — this only
    feeds a suggestion, so a wrong guess is a slightly-off drill target, not a
    bad answer."""
    if not rows or not isinstance(rows[0], dict):
        return None
    keys = list(rows[0].keys())
    name_key = next((k for k in keys if col_rx.search(k)), None)
    if not name_key:
        return None
    num_key = next(
        (k for k in keys
         if k != name_key
         and isinstance(rows[0].get(k), (int, float))
         and not isinstance(rows[0].get(k), bool)),
        None,
    )
    if not num_key:
        return None
    best: tuple[str, float] | None = None
    for r in rows:
        v, n = r.get(num_key), r.get(name_key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or n in (None, ""):
            continue
        if best is None or v > best[1]:
            best = (str(n), float(v))
    return best[0] if best else None


def _scope_phrase(ents: dict) -> str:
    """A human-readable geography/time scope drawn from the resolved entities,
    e.g. ' in Ri Bhoi' or ' in Mawkyrwat block, South West Khasi Hills'. Falls
    back to ' in Meghalaya' so every suggestion is a complete question."""
    d = ents.get("district")
    b = ents.get("block")
    if b and d:
        return f" in {str(b).title()} block, {str(d).title()}"
    if b:
        return f" in {str(b).title()} block"
    if d:
        return f" in {str(d).title()}"
    return " in Meghalaya"


def _drill_down(ents: dict, grain: str | None = None,
                rows: list | None = None) -> dict | None:
    """One suggestion that breaks the same answer down a level finer than what
    the user is currently looking at. None once we are at village grain.

    Grain-aware: if the answer is *already* grouped per-district (but not
    filtered to one district), we don't re-ask for a per-district breakdown —
    we offer to open the largest district a level deeper instead."""
    view = _view_level(ents, grain)
    if view == "village":
        return None

    # A named area is in play — drill straight inside it.
    if view == "block" and ents.get("block"):
        q = f"Break that down by village within {str(ents['block']).title()} block."
        return {"label": q, "question": q}
    if view == "district" and ents.get("district"):
        q = f"Break that down by block within {str(ents['district']).title()}."
        return {"label": q, "question": q}

    # Grouped/aggregated but not filtered to one area.
    if view == "state":
        q = "Break that down by district."
        return {"label": q, "question": q}
    if view == "district":
        top = _top_area_from_rows(rows, _DISTRICT_COL_RX)
        if top:
            q = f"Break that down by block within {top.title()}."
            return {"label": q, "question": q}
        return None
    if view == "block":
        top = _top_area_from_rows(rows, _BLOCK_COL_RX)
        if top:
            q = f"Break that down by village within {top.title()} block."
            return {"label": q, "question": q}
        return None
    return None


# ── DATA route ──────────────────────────────────────────────────────────────
def _fill_metric(out: list[str], question: str, candidates: list[str]) -> None:
    """Top `out` up to `_MAX` using metric questions from `candidates`, in order,
    skipping any that restate the question or already appear. Used in place of
    the old rules/eligibility tail so a data answer's next steps stay entirely
    metric-driven."""
    have = {_norm(x) for x in out}
    asked = _norm(question)
    for c in candidates:
        if len(out) >= _MAX:
            break
        k = _norm(c)
        if not k or k == asked or k in have:
            continue
        have.add(k)
        out.append(c)


def _pmay_data(question: str, ents: dict, grain: str | None = None,
               rows: list | None = None) -> list[dict]:
    scope = _scope_phrase(ents)
    gsuf = _grain_suffix(ents, grain)
    out: list[dict] = []

    # 1. The complementary PMAY-G metric to the one just asked, kept at the same
    #    grain as the answer (', by district') so it reads as a step forward.
    if _asked(question, "sanction") and not _asked(question, "complet"):
        out.append(f"How many PMAY-G houses have been completed{scope}{gsuf}?")
    elif _asked(question, "complet") and not _asked(question, "sanction"):
        out.append(f"How many PMAY-G houses have been sanctioned{scope}{gsuf}?")
    elif _asked(question, "release", "utilis", "utiliz", "amount", "fund", "money"):
        out.append(f"What is the PMAY-G fund utilisation rate{scope}{gsuf}?")
    elif _asked(question, "completion rate", "in progress", "in-progress"):
        out.append(f"How much PMAY-G money has been released against the sanctioned amount{scope}{gsuf}?")
    else:
        out.append(f"What is the PMAY-G completion rate{scope}{gsuf}?")

    # 2. Drill one level finer than what the user is looking at.
    d = _drill_down(ents, grain, rows)
    if d:
        out.append(d["question"])

    # 3. Another PMAY-G metric — keep the whole set metric-driven, no rules chip.
    _fill_metric(out, question, [
        f"How many PMAY-G houses have been completed{scope}{gsuf}?",
        f"How many PMAY-G houses have been sanctioned{scope}{gsuf}?",
        f"What is the PMAY-G completion rate{scope}{gsuf}?",
        f"How much PMAY-G money has been released against the sanctioned amount{scope}{gsuf}?",
    ])
    return [{"label": q, "question": q} for q in out]


def _mgnrega_data(question: str, ents: dict, grain: str | None = None,
                  rows: list | None = None) -> list[dict]:
    scope = _scope_phrase(ents)
    gsuf = _grain_suffix(ents, grain)
    out: list[str] = []

    # 1. The complementary metric, kept at the answer's grain (', by district').
    if _asked(question, "person day", "persondays", "person-day", "man day", "work day", "muster"):
        out.append(f"What was the total MGNREGA wage expenditure{scope}{gsuf}?")
    elif _asked(question, "expenditure", "spend", "spent", "wage", "material", "cost"):
        out.append(f"How many MGNREGA person-days were generated{scope}{gsuf}?")
    elif _asked(question, "job card"):
        out.append(f"How many households completed 100 days of MGNREGA work{scope}{gsuf}?")
    elif _asked(question, "100 day", "hundred day"):
        out.append(f"How many MGNREGA person-days were generated{scope}{gsuf}?")
    else:
        out.append(f"What was the total MGNREGA expenditure{scope}{gsuf}?")

    if not _asked(question, "trend", "over time", "over the", "year on year", "compare", "change"):
        out.append(f"How have MGNREGA person-days changed over the last three years{scope}?")

    d = _drill_down(ents, grain, rows)
    if d:
        out.append(d["question"])

    # Keep the whole set metric-driven — no eligibility chip under a number.
    _fill_metric(out, question, [
        f"What was the total MGNREGA wage expenditure{scope}{gsuf}?",
        f"How many MGNREGA person-days were generated{scope}{gsuf}?",
        f"How many households completed 100 days of MGNREGA work{scope}{gsuf}?",
        f"How many active MGNREGA job cards are there{scope}{gsuf}?",
    ])
    return [{"label": q, "question": q} for q in out]


def _cross_scheme_data(question: str, ents: dict, grain: str | None = None,
                       rows: list | None = None) -> list[dict]:
    scope = _scope_phrase(ents)
    gsuf = _grain_suffix(ents, grain)
    out: list[str] = []
    # Only offer the spend comparison if that's not what was just asked.
    if not _asked(question, "spend", "spent", "expenditure", "money", "cost", "compare"):
        out.append(f"Compare MGNREGA and PMAY-G spending{scope}{gsuf}.")
    out.append(f"Which villages have both MGNREGA and PMAY-G activity{scope}?")
    d = _drill_down(ents, grain, rows)
    if d:
        out.append(d["question"])
    # Keep the whole set metric-driven — no eligibility chip under a number.
    _fill_metric(out, question, [
        f"Compare MGNREGA and PMAY-G spending{scope}{gsuf}.",
        f"Which villages have both MGNREGA and PMAY-G activity{scope}?",
        f"How many households have benefited from MGNREGA and PMAY-G combined{scope}?",
    ])
    return [{"label": q, "question": q} for q in out]


# ── KNOWLEDGE route ─────────────────────────────────────────────────────────
# Ordered ladder per scheme. Each entry: (distinctive words that mean "already
# asked, skip it", the question text). A general answer only ever offers more
# general questions — nothing from the data side is mixed in.
_KNOWLEDGE_LADDER = {
    "PMAY-G": [
        (("eligib", "who can", "who qualifies"), "Who is eligible for PMAY-G?"),
        (("document", "paper", "proof"), "What documents are required to apply for PMAY-G?"),
        (("apply", "application", "how do i get", "registration"), "How do I apply for PMAY-G?"),
        (("assistance", "how much money", "subsidy", "unit cost", "amount given"),
         "How much financial assistance does PMAY-G provide per house?"),
        (("select", "priority", "secc", "waitlist", "beneficiary list"),
         "How is a PMAY-G beneficiary selected from the SECC list?"),
    ],
    "MGNREGA": [
        (("eligib", "who can", "who qualifies"), "Who is eligible for MGNREGA work?"),
        (("job card", "apply", "registration", "how do i get"), "How do I apply for a MGNREGA job card?"),
        (("wage rate", "how much paid", "daily wage", "payment"), "What is the MGNREGA wage rate?"),
        (("what work", "type of work", "permissible", "allowed work"), "What kinds of work are allowed under MGNREGA?"),
        (("100 day", "hundred day", "guarantee"), "What is the 100-day guarantee under MGNREGA?"),
    ],
}
def _knowledge(question: str, schemes: list[str]) -> list[dict]:
    out: list[str] = []
    for scheme in schemes:
        for needles, q in _KNOWLEDGE_LADDER.get(scheme, []):
            if len(out) >= _MAX:
                break
            if _asked(question, *needles):
                continue
            if q not in out:
                out.append(q)
        if len(out) >= _MAX:
            break
    return [{"label": q, "question": q} for q in out[:_MAX]]


# ── entry point ─────────────────────────────────────────────────────────────
def build_followups(route: str, question: str, schemes: list[str],
                    resolved_entities: dict, sql: str | None = None,
                    rows: list | None = None) -> list[dict]:
    """Up to `_MAX` {label, question} next-step suggestions for a data/knowledge
    answer. Empty list for any other route or if nothing sensible applies.

    `sql` is the generated query for a data answer — only its GROUP BY is read,
    to keep suggestions at the grain the answer is already at. `rows` is the
    result set, used to name the largest area when drilling one level deeper."""
    q = question or ""
    ents = resolved_entities or {}
    primary = _primary_schemes(schemes, q)
    grain = _answer_grain(sql) if route == "data" else None

    if route == "knowledge":
        opts = _knowledge(q, primary)
    elif route == "data":
        if len(primary) >= 2:
            opts = _cross_scheme_data(q, ents, grain, rows)
        elif primary[0] == "PMAY-G":
            opts = _pmay_data(q, ents, grain, rows)
        else:
            opts = _mgnrega_data(q, ents, grain, rows)
    else:
        return []

    # Drop any suggestion that just restates the question, and any duplicate.
    asked_norm = _norm(q)
    seen: set[str] = set()
    cleaned: list[dict] = []
    for o in opts:
        key = _norm(o["question"])
        if not key or key == asked_norm or key in seen:
            continue
        seen.add(key)
        cleaned.append(o)
    return cleaned[:_MAX]
