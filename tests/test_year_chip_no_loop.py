"""
A financial-year chip the pipeline OFFERS must never be rejected as out of range.

User report 2026-10-07 ("it is looping again and again"), found by the loop sweep:
"How many SHGs are there?" paused for scope, then for a year, offered **FY 1984-85**
— NRLM's earliest formation year — and when that chip was tapped answered *"data is
available only for the financial years 1984-85, …"* and showed the same list again.
The pipeline was refusing a year out of its own list, so the thread could never end.

Cause: `_parse_year_key` only understood 2010-2039. NRLM is the first scheme whose
data starts before 2010 (formation years 1984-85 … 2022-23), so every pre-2010 chip
parsed as None, was treated as an unparseable year, and re-raised `year-out-of-range`.

The core test is the invariant, not the symptom: for every scheme, every year chip
the pipeline offers must parse and be in range. Scheme eight gets that check free.

    python -m pytest tests/test_year_chip_no_loop.py -q
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app.schema_context import SCHEME_CATALOG  # noqa: E402


# ── the invariant: an offered year is an accepted year ──────────────────────
@pytest.mark.parametrize("scheme", sorted(SCHEME_CATALOG))
def test_every_offered_year_parses_and_is_in_range(scheme):
    years, _live = p._available_years_for([scheme])
    if not years:
        pytest.skip("%s has no time dimension" % scheme)
    for y in years:
        # exactly the chip text the pipeline builds: "FY 1984-85"
        key = p._parse_year_key("FY %s" % y)
        assert key is not None, (
            "%s offers FY %s as a chip but _parse_year_key cannot read it -> the "
            "chip loops" % (scheme, y))
        assert p._year_in_data_range(key, [scheme]), (
            "%s offers FY %s as a chip but _year_in_data_range rejects it -> the "
            "chip loops" % (scheme, y))


@pytest.mark.parametrize("scheme", sorted(SCHEME_CATALOG))
def test_no_year_chip_resumes_into_an_out_of_range_pause(scheme):
    """Follow each year chip one hop and confirm it would not re-raise."""
    years, _live = p._available_years_for([scheme])
    if not years:
        pytest.skip("%s has no time dimension" % scheme)
    for y in years[:4] + years[-4:]:
        resumed = "How many records are there for FY %s" % y
        m = p._YEAR_RANGE_TOKEN_RE.search(resumed)
        assert m, "the chip text %r carries no recognisable year token" % resumed
        key = p._parse_year_key(m.group(1))
        assert key is not None and p._year_in_data_range(key, [scheme]), (
            "%s: the chip %r would raise year-out-of-range again" % (scheme, resumed))


# ── the specific regression ────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("1984-85", 1984),          # NRLM's earliest; the year that looped
    ("FY 1984-85", 1984),
    ("1993-94", 1993),
    ("1995-96", 1995),
    ("1998-99", 1998),
    ("1999-00", 1999),
    ("2000-01", 2000),
    ("2009-10", 2009),
])
def test_pre_2010_financial_years_parse(text, expected):
    assert p._parse_year_key(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("2017-18", 2017),
    ("2023-2024", 2023),
    ("FY 2023-24", 2023),
    ("FY23", 2023),
    ("25-26", 2025),
    ("2023", 2023),
    ("in 2019-20", 2019),
])
def test_existing_year_forms_are_unchanged(text, expected):
    assert p._parse_year_key(text) == expected


@pytest.mark.parametrize("text", [
    "top 2000 villages",        # a quantity, not a year
    "1500 households",
    "the 1984 census",          # a BARE pre-2010 number stays non-year
    "5000 rupees",
    "list 100 SHGs",
])
def test_a_bare_number_is_still_not_a_year(text):
    """Widening the range must not turn ordinary quantities into years — only an
    explicit NNNN-NN range gains the older centuries."""
    assert p._parse_year_key(text) is None


# ── the out-of-range reply itself must not hand back the bad year ──────────
@pytest.mark.parametrize("question,bad", [
    ("total person-days in Chokpot for FY 2050-51", "2050-51"),
    ("How many SHGs are there in 1850-51", "1850-51"),
    ("SHGs in Chokpot, 2050-51", "2050-51"),
])
def test_out_of_range_chips_drop_the_rejected_year(question, bad):
    cn = p._year_out_of_range_clarification(question, bad, ["NRLM"])
    assert cn.rule == "year-out-of-range"
    for opt in cn.options:
        assert bad not in opt["question"], (
            "chip %r still carries the rejected year %s -> loop" % (opt["label"], bad))


def test_a_genuinely_impossible_year_is_still_refused():
    """The guard must keep working: 2050-51 is in no scheme."""
    key = p._parse_year_key("2050-51")
    assert key == 2050
    assert not p._year_in_data_range(key, ["NRLM"])
