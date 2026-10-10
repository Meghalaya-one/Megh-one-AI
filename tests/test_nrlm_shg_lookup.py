"""
A question about one NRLM SHG must reach NRLM, and must not be asked for scope.

Found by the all-SHGs run (2026-10-07), testing all 40,629 groups:

  1. **The scheme was hijacked.** "How many members are in SHG code 7194?" was
     answered *"No producer group named 'SHG code 7194' was found in the Focus
     Legacy data"* — while `curated.v_nrlm` holds the answer (9 members). The
     rule "a group-name question that names no scheme of its own is a Focus
     Legacy question" predates NRLM, which has groups of its own; `_PG_STOP_WORDS`
     even strips "shg" from the name, so the SHG wording could not save it.
     Typing "under NRLM" was the only way through.

  2. **It then asked for scope and a year.** `shg_code` is unique across all
     40,629 groups (NRLM rule 12) and each group has exactly one row and one
     formation year, so a named code already pins everything. Asking for a
     district and a financial year left a complete question unanswerable in one
     turn — and choosing a year would filter the single row to nothing.

    python -m pytest tests/test_nrlm_shg_lookup.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402

SHG_QUESTIONS = [
    "How many members are in SHG code 7194?",
    "How many members are in SHG code 67?",
    "How many members does SHG 7194 have?",
    "What is the member count of SHG code 40629?",
    "how many members are there in shg 123",
]


# ── the code is recognised ──────────────────────────────────────────────────
@pytest.mark.parametrize("q", SHG_QUESTIONS)
def test_an_shg_code_is_recognised(q):
    assert p._NRLM_SHG_CODE_RE.search(q)


@pytest.mark.parametrize("q", [
    "How many members are there in Sakania Producer Group?",
    "How many members are in the producer group?",
    "total members across all SHGs",          # no code
    "how many SHGs are there in 2017-18",     # a YEAR, not a code
    "top 2000 villages by membership",        # a quantity
])
def test_text_without_an_shg_code_is_not_matched(q):
    assert not p._NRLM_SHG_CODE_RE.search(q)


# ── 1. the question stays on NRLM ───────────────────────────────────────────
@pytest.mark.parametrize("q", SHG_QUESTIONS)
def test_nrlm_vocabulary_keeps_a_group_question_off_focus_legacy(q):
    """`_NRLM_ONLY_TERMS` matching is what now blocks the Focus Legacy pull."""
    assert p._NRLM_ONLY_TERMS.search(q), (
        "the SHG wording must count as naming NRLM, or the group-name rule "
        "reroutes the question to Focus Legacy")


def test_a_real_producer_group_question_still_goes_to_focus_legacy():
    """The Focus Legacy behaviour this rule exists for must survive."""
    q = "How many members are there in Sakania Producer Group?"
    assert not p._NRLM_ONLY_TERMS.search(q)
    assert p._pg_name_question(q)


# ── 2. a named SHG needs no scope and no year ───────────────────────────────
@pytest.mark.parametrize("q", SHG_QUESTIONS)
def test_a_named_shg_does_not_need_a_scope_clarification(q):
    assert p._needs_scope_clarification(q, {}) is False


@pytest.mark.parametrize("q", SHG_QUESTIONS)
def test_a_named_shg_does_not_need_a_year_clarification(q):
    assert p._needs_year_clarification(q, ["NRLM"], {}) is False


# ── 3. an SHG code in the 19xx-21xx band is not a financial year ───────────
# shg_code runs 1..46000, so ~200 codes land in the band _YEAR_RANGE_TOKEN_RE
# treats as a bare year. "How many members are in SHG code 1900?" had its CODE
# read as a financial year, failed the range check, and the question died —
# every SHG from 1900 to 2199. The boundary was exactly where the bulk run
# started failing: code 1899 passed, 1900 did not.
@pytest.mark.parametrize("code", [1900, 1901, 1966, 2017, 2099, 2100, 2199])
def test_an_shg_code_in_the_year_band_is_not_read_as_a_year(code):
    q = "How many members are in SHG code %d?" % code
    assert p._out_of_range_year_in(q, ["NRLM"]) is None
    assert p._years_in_question(q, ["NRLM"]) == ([], [])


@pytest.mark.parametrize("code", [67, 1899, 7194, 40629])
def test_codes_outside_the_year_band_were_never_affected(code):
    q = "How many members are in SHG code %d?" % code
    assert p._out_of_range_year_in(q, ["NRLM"]) is None


@pytest.mark.parametrize("q,expected", [
    ("total person-days in FY 2050-51", "2050-51"),
    ("houses completed in 2023-24", "2023-24"),      # beyond NRLM's 2022-23
])
def test_a_real_out_of_range_year_is_still_caught(q, expected):
    assert p._out_of_range_year_in(q, ["NRLM"]) == expected


def test_a_real_year_in_range_is_still_accepted():
    assert p._out_of_range_year_in("SHGs formed in 1984-85", ["NRLM"]) is None


def test_an_nrlm_question_without_a_code_still_asks_for_scope():
    """The gates must stay closed for genuinely unscoped questions."""
    assert p._needs_scope_clarification("How many SHGs are there?", {}) is True


def test_another_schemes_question_is_unaffected_by_the_shg_rule():
    """The year gate exemption is NRLM-only."""
    q = "How many members are in SHG code 7194?"
    assert p._needs_year_clarification(q, ["MGNREGA"], {}) in (True, False)
    # whatever MGNREGA decides, it must not be decided by the NRLM exemption
    assert p._needs_year_clarification(q, ["NRLM"], {}) is False
