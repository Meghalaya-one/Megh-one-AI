"""The "Use cases" modal in web/ai_query.html — structural tests.

Two separate failures motivate this file.

KI-210 (2026-10-09, user-reported): every use-case row was dead. The row was
built with an inline ``onclick="closeModal(); ask(${JSON.stringify(q)});"``.
``JSON.stringify`` emits a real double quote, which ends the double-quoted
``onclick`` attribute, so the browser parsed the truncated, invalid fragment
``closeModal(); ask(`` and the click did nothing — on all of the rows, silently.

The same session: the modal listed only MGNREGA, PMAY-G and FOCUS+, three of
the **seven** schemes the service answers for, so four schemes had no examples.

These tests read the HTML, so they fail if the markup regresses to an inline
handler, if a scheme is added to SCHEME_CATALOG without an example, or if a
question is filed under a section it does not route to. They assert the
invariants, not the exact wording, so editing or adding a question is free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import pipeline
from app.schema_context import SCHEME_CATALOG

_HTML = Path(__file__).resolve().parents[1] / "web" / "ai_query.html"


def _source() -> str:
    return _HTML.read_text(encoding="utf-8")


def _js_object(name: str, src: str) -> dict[str, list[str]]:
    """Parse a ``const <name> = { 'k': ['a', 'b'], ... };`` literal out of the
    page. Written by hand rather than with a JS engine: the file has no build
    step and the tests must run with nothing but the repo venv."""
    match = re.search(r"const " + name + r" = (\{.*?^\};)", src, re.S | re.M)
    assert match, f"{name} not found in {_HTML.name}"
    body = match.group(1).rstrip(";")
    body = re.sub(r"^\s*//.*$", "", body, flags=re.M)      # drop // comments
    body = re.sub(r",(\s*[\]}])", r"\1", body)             # drop trailing commas
    body = body.replace("\\u2014", "—")

    # Single-quoted JS strings -> double-quoted JSON strings.
    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            j = i + 1
            buf: list[str] = []
            while body[j] != "'":
                if body[j] == "\\":
                    buf.append(body[j : j + 2])
                    j += 2
                    continue
                buf.append(body[j])
                j += 1
            out.append(json.dumps("".join(buf)))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return json.loads("".join(out))


def _use_cases() -> dict[str, list[str]]:
    return _js_object("USE_CASES", _source())


def _questions() -> list[str]:
    return [q for qs in _use_cases().values() for q in qs]


# The scheme each single-scheme section must route to. Cross-scheme is the one
# section that is expected to name two or more.
_SECTION_SCHEME = {
    "MGNREGA": "MGNREGA",
    "PMAY-G": "PMAY-G",
    "Focus Plus": "Focus Plus",
    "Focus Legacy": "Focus Legacy",
    "CM Elevate (applications)": "CM Elevate",
    "CM Elevate Legacy (sanctions & disbursements)": "CM Elevate Legacy",
    "NRLM — Self Help Groups": "NRLM",
    "Cross-scheme": None,
}


# --------------------------------------------------------------------------
# KI-210: the rows must be clickable.
# --------------------------------------------------------------------------

def test_use_case_rows_do_not_use_an_inline_onclick():
    """The exact regression. An inline onclick carrying a JSON-stringified
    question is unparseable, because the quotes collide with the attribute's
    own delimiter."""
    # Strip // comment lines first: the fix is documented in a comment that
    # quotes the broken markup verbatim, and that must not count as a hit.
    src = re.sub(r"^\s*//.*$", "", _source(), flags=re.M)
    assert 'onclick="closeModal(); ask(' not in src
    assert not re.search(r'onclick="[^"]*JSON\.stringify', src)


def test_use_case_rows_carry_the_question_in_a_data_attribute():
    src = _source()
    open_use_cases = src[src.index("function openUseCases()") :]
    open_use_cases = open_use_cases[: open_use_cases.index("\n}")]
    assert 'data-ask="${escapeHTML(q)}"' in open_use_cases, (
        "the question must travel in an HTML-escaped data attribute"
    )
    assert "bindModalAsk()" in open_use_cases, "the rows must be bound after render"


def test_bind_modal_ask_uses_add_event_listener_and_supports_the_keyboard():
    src = _source()
    fn = src[src.index("function bindModalAsk()") :]
    fn = fn[: fn.index("\n}\n")]
    assert "addEventListener('click'" in fn
    assert "addEventListener('keydown'" in fn
    assert "'Enter'" in fn and "' '" in fn
    assert "closeModal()" in fn and "ask(q)" in fn


def test_rows_are_focusable_buttons():
    src = _source()
    open_use_cases = src[src.index("function openUseCases()") :]
    open_use_cases = open_use_cases[: open_use_cases.index("\n}")]
    assert 'role="button"' in open_use_cases
    assert 'tabindex="0"' in open_use_cases


def test_sidebar_chips_are_not_hand_escaped_into_an_inline_onclick():
    """The chips had the same shape, hand-escaping only the apostrophe, so a
    question containing a double quote would have broken out of the attribute
    exactly as the use-case rows did."""
    src = _source()
    assert "onclick=\"useQuery('${safe}')\"" not in src
    assert 'data-chip-ask="${escapeHTML(q)}"' in src


# --------------------------------------------------------------------------
# Scheme coverage: every scheme the service answers for gets examples.
# --------------------------------------------------------------------------

def test_every_scheme_in_the_catalog_has_its_own_section():
    """Checked through the SECTION -> scheme mapping, not by searching the
    whole blob for the scheme name: "NRLM" appears inside MGNREGA-free
    questions too, so a plain substring search passes even when the NRLM
    section has been renamed or removed."""
    covered = {
        _SECTION_SCHEME[section]
        for section in _use_cases()
        if _SECTION_SCHEME.get(section)
    }
    missing = [s for s in SCHEME_CATALOG if s not in covered]
    assert not missing, f"schemes with no section in the Use cases modal: {missing}"


def test_every_section_has_at_least_two_examples():
    thin = {s: qs for s, qs in _use_cases().items() if len(qs) < 2}
    assert not thin, f"sections with fewer than two examples: {thin}"


def test_every_section_is_a_known_section():
    unknown = [s for s in _use_cases() if s not in _SECTION_SCHEME]
    assert not unknown, (
        f"new section {unknown} — add it to _SECTION_SCHEME with the scheme it "
        f"must route to, so the routing test covers it"
    )


def test_sidebar_chips_cover_every_scheme_too():
    """The chip headings are prose ("Focus Plus - farmer cash benefit"), so
    this one does match on the heading text rather than a strict mapping."""
    headings = " | ".join(_js_object("SAMPLE_CATEGORIES", _source()))
    missing = [s for s in SCHEME_CATALOG if s not in headings]
    assert not missing, f"schemes with no sidebar chip category: {missing}"


# --------------------------------------------------------------------------
# Each question must reach the scheme it is filed under.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "section,question",
    [(s, q) for s, qs in _use_cases().items() for q in qs],
)
def test_question_routes_to_the_section_it_is_filed_under(section, question):
    expected = _SECTION_SCHEME[section]
    got = pipeline._shortcut_scheme(question)
    if expected is None:
        assert got and len(got) >= 2, (
            f"cross-scheme example resolved to {got}, not two or more schemes"
        )
    else:
        assert got == [expected], f"{question!r} resolved to {got}, not [{expected!r}]"


# --------------------------------------------------------------------------
# The collision and per-scheme rules from CLAUDE.md / docs/SCHEMES.md.
# --------------------------------------------------------------------------

def test_no_question_says_a_bare_focus():
    """A bare "Focus" is ambiguous between Focus Plus and Focus Legacy, and is
    always asked about, never guessed. An example that triggers a clarification
    is a bad example."""
    bare = [
        q for q in _questions()
        if re.search(r"\bfocus\b", q, re.I) and not re.search(r"focus plus|focus legacy", q, re.I)
    ]
    assert not bare, f"bare 'Focus' in: {bare}"


def test_cm_elevate_examples_ask_for_neither_money_nor_a_year():
    """CM Elevate has no money column and no time dimension, so a money or
    year question is unanswerable by construction (docs/SCHEMES.md)."""
    for q in _use_cases()["CM Elevate (applications)"]:
        low = q.lower()
        assert not re.search(r"\b(amount|disbursed|sanctioned|spend|crore|lakh|rupees|₹)\b", low), q
        assert not re.search(r"\b(19|20)\d\d-\d\d\b", low), q
        assert "financial year" not in low, q


def test_nrlm_examples_never_put_money_in_a_year():
    """NRLM's RF and CIF are cumulative and undated. Filtering them by the
    formation year returns what those SHGs hold today — a different figure that
    looks exactly like the one asked for. The scheme's highest-risk rule."""
    for q in _use_cases()["NRLM — Self Help Groups"]:
        low = q.lower()
        if re.search(r"\b(rf|cif|fund|amount|money)\b", low):
            assert not re.search(r"\b(19|20)\d\d-\d\d\b", low), q
            assert "financial year" not in low, q
            assert "trend" not in low and "growth" not in low, q


def test_no_question_contains_an_unfilled_placeholder():
    """The questions come from the officers' use-case files, which are written
    with [district] / [block] placeholders. A placeholder left in would be sent
    to the pipeline verbatim."""
    bad = [q for q in _questions() if re.search(r"[\[<]\w+[\]>]", q)]
    assert not bad, f"unfilled placeholder in: {bad}"


# --------------------------------------------------------------------------
# The Glossary modal (same file, same staleness: KI-211 covered it too).
# --------------------------------------------------------------------------

def _glossary() -> dict[str, list[dict[str, str]]]:
    """Parse ``const GLOSSARY = { 'Scheme': [{ term: '...', def: '...' }] };``.

    Separate from _js_object because the values are objects with bare (unquoted)
    JS keys, not plain string lists."""
    src = _source()
    match = re.search(r"const GLOSSARY = (\{.*?^\};)", src, re.S | re.M)
    assert match, "GLOSSARY not found"
    body = match.group(1).rstrip(";")
    body = re.sub(r"^\s*//.*$", "", body, flags=re.M)
    body = re.sub(r",(\s*[\]}])", r"\1", body)
    # Decode the — escapes the page writes, exactly as _js_object does, so
    # both parsers return the same section names.
    body = body.replace("\\u2014", "—")

    out: list[str] = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch == "'":
            j = i + 1
            buf: list[str] = []
            while body[j] != "'":
                if body[j] == "\\":
                    nxt = body[j + 1]
                    # \' is a plain apostrophe in JS but invalid in JSON.
                    buf.append("'" if nxt == "'" else body[j : j + 2])
                    j += 2
                    continue
                buf.append(body[j])
                j += 1
            out.append(json.dumps("".join(buf)))
            i = j + 1
        elif body.startswith("term:", i):
            out.append('"term":')
            i += len("term:")
        elif body.startswith("def:", i):
            out.append('"def":')
            i += len("def:")
        else:
            out.append(ch)
            i += 1
    return json.loads("".join(out))


def test_every_scheme_has_a_glossary_section():
    """KI-211: the Glossary listed only MGNREGA, PMAY-G and FOCUS+, so four of
    the seven schemes had no terms explained at all."""
    sections = list(_glossary())
    covered = {_SECTION_SCHEME[s] for s in sections if _SECTION_SCHEME.get(s)}
    missing = [s for s in SCHEME_CATALOG if s not in covered]
    assert not missing, f"schemes with no Glossary section: {missing}"


def test_glossary_sections_are_known_sections():
    unknown = [s for s in _glossary() if s not in _SECTION_SCHEME]
    assert not unknown, f"unknown Glossary section {unknown} — add it to _SECTION_SCHEME"


def test_every_glossary_term_has_a_nonempty_definition():
    bad = [
        (section, entry.get("term"))
        for section, entries in _glossary().items()
        for entry in entries
        if not entry.get("term", "").strip() or not entry.get("def", "").strip()
    ]
    assert not bad, f"glossary entries missing a term or definition: {bad}"


def test_glossary_sections_match_the_use_case_sections():
    """Both modals are hand-maintained lists of the same schemes. Keeping the
    section names identical is what lets one mapping cover both."""
    assert set(_glossary()) <= set(_SECTION_SCHEME)
    scheme_sections = {s for s in _use_cases() if _SECTION_SCHEME.get(s)}
    glossary_sections = set(_glossary())
    assert scheme_sections == glossary_sections, (
        "Use cases and Glossary disagree about the scheme sections: "
        f"only in Use cases {scheme_sections - glossary_sections}, "
        f"only in Glossary {glossary_sections - scheme_sections}"
    )


def test_glossary_states_the_cm_elevate_and_nrlm_traps():
    """The two rules that most often produce a plausible wrong number are the
    whole reason an officer opens the glossary, so assert they are stated."""
    gloss = _glossary()
    cme = " ".join(e["def"] for e in gloss["CM Elevate (applications)"]).lower()
    assert "no" in cme and "financial year" in cme, "CM Elevate: missing the no-year rule"
    assert "sanctioned amount" in cme or "disbursement" in cme, "CM Elevate: missing the no-money rule"

    nrlm = " ".join(e["def"] for e in gloss["NRLM \u2014 Self Help Groups"]).lower()
    assert "cumulative" in nrlm, "NRLM: RF/CIF must be described as cumulative"
    assert "never" in nrlm and ("saving" in nrlm or "loan" in nrlm), (
        "NRLM: must say RF/CIF are grants, never savings or loans"
    )
