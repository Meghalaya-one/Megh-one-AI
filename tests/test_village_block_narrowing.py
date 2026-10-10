"""
A village question that NAMES its block must not ask which village is meant.

Found by the all-blocks / all-constituencies / all-villages / all-SHGs run
(2026-10-07). Two defects, both in the village path, both hit within the first
60 villages:

  1. **The loop.** "How many SHGs are there in Bhangarpar village, Demdema block,
     West Garo Hills?" replied *"'Bhangarpar' corresponds to more than one
     village: Bhangarpar in DEMDEMA block, ANGARIPARA in TIKRIKILLA block. Which
     of these is intended?"* — ignoring the block the question had just named —
     and every chip re-asked the same thing. 5 of the first 60 villages.

     Cause: the branch that resolves a block is an `elif`, so a question naming
     BOTH a village and a block takes the other arm and `resolved["block"]` is
     empty when the village is resolved. The mention-extractor could not be used
     as a fallback: it tagged the block on one call and dropped it on the next
     for the identical question. `_block_named_for_village` therefore scans the
     question text, and only where the word "block" is attached to the name.

  2. **The false zero.** "Pabomari village, Demdema block" resolved village_code
     272748 correctly, then the generator added `AND lgd_block = 'DEMDHEMA'` — a
     spelling that exists nowhere in the data — and the answer was a confident
     "0 SHGs" for a village holding 23. `_mgnrega_drop_geo_beside_village` already
     removed exactly this (MGNREGA's MAWLIEH incident) but was scoped to MGNREGA;
     it now covers every scheme whose view carries village_code.

    python -m pytest tests/test_village_block_narrowing.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402


# ── 1. the block named in the question is found, deterministically ──────────
@pytest.mark.parametrize("question,expected", [
    ("How many SHGs are there in Bhangarpar village, Demdema block, West Garo Hills?", "DEMDEMA"),
    ("How many SHGs are there in Apalgre village, Selsella block, West Garo Hills?", "SELSELLA"),
    ("How many SHGs are there in Kotchugre village, Batabari block, West Garo Hills?", "BATABARI"),
    ("SHGs in Haribhanga village, Tikrikilla block", "TIKRIKILLA"),
    ("members in Belbari village, in the Demdema C&RD block", "DEMDEMA"),
])
def test_a_block_written_beside_the_word_block_is_found(question, expected, monkeypatch):
    """The extractor is unreliable here, so the text is scanned. With mentions
    empty — the shape that actually looped — the scan must still find it."""
    monkeypatch.setattr(p, "resolve_dimension",
                        lambda text, scheme, dim: _fake(text, dim))
    assert p._block_named_for_village(question, {}, ["NRLM"]) == expected


class _R:
    def __init__(self, status, canonical=None):
        self.status, self.canonical = status, canonical


_BLOCKS = {"DEMDEMA", "SELSELLA", "BATABARI", "TIKRIKILLA", "UMLING", "CHOKPOT"}


def _fake(text, dim):
    t = str(text).strip().upper()
    if dim == "block" and t in _BLOCKS:
        return _R("resolved", t)
    return _R("not_found")


def test_the_extractor_mention_is_used_when_it_survives(monkeypatch):
    monkeypatch.setattr(p, "resolve_dimension", lambda text, scheme, dim: _fake(text, dim))
    assert p._block_named_for_village(
        "SHGs in Bhangarpar village", {"block": "Demdema"}, ["NRLM"]) == "DEMDEMA"


@pytest.mark.parametrize("question", [
    # No "block" word anywhere: a bare name must NEVER be taken as a block —
    # 26 of 56 block names are also constituency or village names, which is why
    # scan_dimension refuses to scan them.
    "How many SHGs are there in Demdema?",
    "SHGs in Selsella",
    "How many SHGs are there in Chokpot village, West Garo Hills?",
])
def test_a_bare_name_is_never_taken_as_the_block(question, monkeypatch):
    monkeypatch.setattr(p, "resolve_dimension", lambda text, scheme, dim: _fake(text, dim))
    assert p._block_named_for_village(question, {}, ["NRLM"]) is None


def test_an_unknown_block_name_resolves_to_nothing(monkeypatch):
    monkeypatch.setattr(p, "resolve_dimension", lambda text, scheme, dim: _fake(text, dim))
    assert p._block_named_for_village(
        "SHGs in X village, Nowhere block", {}, ["NRLM"]) is None


def test_no_scheme_means_no_narrowing():
    assert p._block_named_for_village("SHGs in A village, Demdema block", {}, []) is None
    assert p._block_named_for_village("SHGs in A village, Demdema block", {}, None) is None


# ── 1b. a name the question calls a BLOCK is never resolved as the village ──
# "…in Songgitalgre village, Rongram block, West Garo Hills" returned
# village=None on 6 of 8 identical extractor calls. The BLOCK name then flowed
# into the village slot, resolved to the village "Rongram Bazar" (code 273541),
# and the answer was a confident 9 SHGs for a village that holds 8 — a silent
# wrong number. The question's own wording has to outrank the extractor.
SONGGI_Q = "How many SHGs are there in Songgitalgre village, Rongram block, West Garo Hills?"


@pytest.fixture()
def rongram_is_a_block(monkeypatch):
    _BLOCKS.add("RONGRAM")
    monkeypatch.setattr(p, "resolve_dimension", lambda text, scheme, dim: _fake(text, dim))
    yield
    _BLOCKS.discard("RONGRAM")


def test_the_questions_block_is_identified(rongram_is_a_block):
    assert p._block_named_for_village(SONGGI_Q, {}, ["NRLM"]) == "RONGRAM"


def _village_slot_survives(question, village_text, schemes=("NRLM",)):
    """The guard as resolve_entities applies it: clear the village slot when it
    holds the question's BLOCK name and the question never calls that name a
    village. Returns what the slot holds afterwards."""
    import re as _re
    blk = p._block_named_for_village(question, {}, list(schemes))
    if blk and str(village_text).strip().upper() == str(blk).upper() \
            and not _re.search(rf"\b{_re.escape(str(village_text))}\s+village\b",
                               question, _re.IGNORECASE):
        return None
    return village_text


def test_the_block_name_is_cleared_from_the_village_slot(rongram_is_a_block):
    """This is the silent wrong number: 'Rongram' in the village slot resolved to
    the village 'Rongram Bazar' and answered 9 for a village that holds 8."""
    assert _village_slot_survives(SONGGI_Q, "Rongram") is None


def test_the_real_village_name_is_left_alone(rongram_is_a_block):
    assert _village_slot_survives(SONGGI_Q, "Songgitalgre") == "Songgitalgre"


def test_a_name_the_question_calls_a_village_is_kept(rongram_is_a_block):
    """26 of 56 block names are also village names. "Rongram village, Rongram
    block" names both, so the village slot must survive."""
    q = "SHGs in Rongram village, Rongram block, West Garo Hills"
    assert _village_slot_survives(q, "Rongram") == "Rongram"


# ── 1c. the scan backstop must run when the question SAYS "village" ─────────
# The backstop was gated on `_stated_level is None`, on the reasoning that a
# question saying "village" had already gone down the extractor's village path.
# It had not: for "…in Doldegre village, Gambegre block, West Garo Hills" the
# extractor returned no village at all, so nothing filtered at village grain and
# the answer came back for a different village (or none). Six villages in the
# first 2,400 failed this way, each with a plausible wrong number.
def test_the_backstop_runs_for_a_question_that_says_village():
    """The gate now admits _stated_level == "village"; read it off the source so
    a refactor that narrows it back fails here."""
    import inspect
    src = inspect.getsource(p.resolve_entities)
    assert '_stated_level in (None, "village")' in src, (
        "the village scan backstop must run when the question states the village "
        "level — the extractor drops the village name on exactly those questions")


@pytest.mark.parametrize("question", [
    "How many SHGs are there in Doldegre village, Gambegre block, West Garo Hills?",
    "How many SHGs are there in Dilma Adap village, Adokgre block, North Garo Hills?",
])
def test_the_village_level_is_stated_by_these_questions(question):
    """Guards the premise of the test above: these really do state the level."""
    assert p._explicit_level_in(question, ["NRLM"]) == "village"


# ── 2. a block literal beside a resolved village_code is dropped ────────────
# village_code already identifies the village; a block/district literal the
# generator adds can only narrow it wrongly, and a misspelt one zeroes it.
_VILLAGE_SQL = ("SELECT COUNT(*) AS shgs FROM curated.v_nrlm "
                "WHERE village_code = 272748 AND lgd_block = 'DEMDHEMA' "
                "AND lgd_district = 'WEST GARO HILLS'")


@pytest.mark.parametrize("scheme", ["NRLM", "MGNREGA"])
def test_a_block_literal_beside_the_village_code_is_removed(scheme):
    out = p._mgnrega_drop_geo_beside_village(
        [scheme], {"resolved": {"village_code": 272748}}, _VILLAGE_SQL)
    assert "village_code = 272748" in out
    assert "DEMDHEMA" not in out and "lgd_block" not in out
    assert "lgd_district" not in out


def test_nrlm_is_in_the_village_code_scheme_list():
    assert "NRLM" in p._VILLAGE_CODE_SCHEMES and "MGNREGA" in p._VILLAGE_CODE_SCHEMES


def test_a_scheme_without_village_code_identity_is_untouched():
    out = p._mgnrega_drop_geo_beside_village(
        ["CM Elevate"], {"resolved": {"village_code": 272748}}, _VILLAGE_SQL)
    assert out == _VILLAGE_SQL


def test_sql_that_does_not_filter_the_resolved_village_is_untouched():
    sql = "SELECT COUNT(*) FROM curated.v_nrlm WHERE lgd_block = 'DEMDEMA'"
    assert p._mgnrega_drop_geo_beside_village(
        ["NRLM"], {"resolved": {"village_code": 272748}}, sql) == sql


# ── 3. a non-numeric literal in an integer code column ─────────────────────
# `block_lgd_code = 'RANIKOR'` — the block NAME in the integer code column —
# made Postgres raise "invalid input syntax for type integer". Every repair
# wrote it again and 5 Ranikor villages died in the KB fallback. The clause is
# redundant beside the village_code, so dropping it is both correct and the only
# thing that clears the type error. Only reproduced at concurrency, which is why
# a single-case re-run looked healthy.
@pytest.mark.parametrize("bad", [
    "AND block_lgd_code = 'RANIKOR'",
    "AND district_lgd_code = 'SOUTH WEST KHASI HILLS'",
    "AND block_lgd_code = 6544",
])
def test_a_code_column_filter_beside_the_village_is_dropped(bad):
    sql = "SELECT COUNT(*) FROM curated.v_nrlm WHERE village_code = 277168 " + bad
    out = p._mgnrega_drop_geo_beside_village(
        ["NRLM"], {"resolved": {"village_code": 277168}}, sql)
    assert out.rstrip() == "SELECT COUNT(*) FROM curated.v_nrlm WHERE village_code = 277168"


def test_a_non_numeric_village_code_literal_is_substituted():
    """The same class in the village_code column itself."""
    sql = "SELECT COUNT(*) FROM curated.v_nrlm WHERE village_code = 'RANIKOR'"
    out = p._mgnrega_pin_village_code(
        ["NRLM"], {"resolved": {"village_code": 277168}}, sql)
    assert "village_code = 277168" in out and "RANIKOR" not in out


# ── 3b. "&" is part of a village name, not a separator ─────────────────────
# 5 villages statewide carry an ampersand ("MAWKOHMIT & MAWKYNSAH", "MAWBLEI A
# & B"). The scan truncated at it, offered the unmatchable "Mawkohmit", resolved
# no village, and the generator then put the NAME in the integer village_code
# column — "invalid input syntax for type integer" on every repair, and the
# question died in the KB fallback with 17 SHGs sitting in the data.
@pytest.mark.parametrize("question,expected", [
    ("How many SHGs are there in Mawkohmit & Mawkynsah village, Mairang block, "
     "Eastern West Khasi Hills?", "Mawkohmit & Mawkynsah"),
    ("SHGs in Mawblei A & B village", "Mawblei A & B"),
])
def test_a_village_name_with_an_ampersand_is_scanned_whole(question, expected):
    cands = p._village_scan_candidates(question, "NRLM")
    assert expected in cands, (
        "the ampersand name must survive the scan, not be truncated at '&': %r" % cands)


def test_an_ordinary_name_is_unaffected_by_the_ampersand_change():
    cands = p._village_scan_candidates(
        "How many SHGs are there in Doldegre village, Gambegre block, West Garo Hills?", "NRLM")
    assert "Doldegre" in cands


# ── 3c. a one-figure answer must state THAT figure, not the row count ───────
# "How many SHGs are there in Jaiaw Pdeng village, Bhoirymbong block, Ri Bhoi?"
# generated the CORRECT `SELECT COUNT(*) AS shgs ... WHERE village_code = 277979`
# — which returns 12 — and the composer answered "There is 1 SHG": it reported
# the number of ROWS, not the counted value. Intermittent (3 of 5 repeats), which
# is the worst kind of wrong number: it reads perfectly and is off by 12x.
def test_nrlm_is_a_one_figure_scheme():
    assert ["NRLM"] in p._ONE_FIGURE_SCHEMES


def test_an_answer_that_states_the_row_count_is_rejected():
    """_answer_covers_metrics is what the one-figure guard tests; it must reject
    the row count and accept the real figure."""
    row = {"shgs": 12}
    assert not p._answer_covers_metrics(
        "There is 1 SHG in Jaiaw Pdeng village, Ri Bhoi district.", row)
    assert p._answer_covers_metrics("There are 12 SHGs in Jaiaw Pdeng village.", row)


# ── 4. an EXACT village name beats a look-alike ────────────────────────────
# "Mawmluh II" (an exact name, 12 SHGs) was offered as ambiguous against
# "MAWMLUH A", and "WEST RANGASORA" against "EAST RANGASORA" — different
# villages, not candidates. The rule that prevents this already existed but was
# gated on a scheme list NRLM was not in, so every chip re-asked the question.
def test_nrlm_is_a_village_exact_name_scheme():
    assert "NRLM" in p._VILLAGE_EXACT_NAME_SCHEMES
    assert p._village_scheme(["NRLM"]) == "NRLM"


def test_nrlm_does_not_inherit_the_focus_plus_narrowing():
    """_VILLAGE_NARROW_SCHEMES carries Focus Plus's ward / urban-body narrowing,
    which NRLM's data has no counterpart for."""
    assert "NRLM" not in p._VILLAGE_NARROW_SCHEMES


@pytest.mark.parametrize("schemes,expected", [
    (["MGNREGA"], "MGNREGA"),
    (["Focus Plus"], "Focus Plus"),
    (["PMAY-G"], "PMAY-G"),
    (["CM Elevate"], "CM Elevate"),
    (["NRLM"], "NRLM"),
    # MGNREGA keeps its first-position gate; every other scheme only counts when
    # it is the ONE scheme asked about, so a two-scheme list yields None.
    (["MGNREGA", "NRLM"], "MGNREGA"),
    (["NRLM", "MGNREGA"], None),
    (["NRLM", "Focus Plus"], None),
    (["Focus Legacy"], None),
    ([], None),
])
def test_the_village_scheme_gate_is_unchanged_for_everyone_else(schemes, expected):
    assert p._village_scheme(schemes) == expected


def test_a_village_list_is_untouched():
    """Several villages resolved: the block is not redundant there."""
    sql = "SELECT COUNT(*) FROM curated.v_nrlm WHERE village_code IN (1,2) AND lgd_block = 'X'"
    assert p._mgnrega_drop_geo_beside_village(
        ["NRLM"], {"resolved": {"village_code": [1, 2]}}, sql) == sql
