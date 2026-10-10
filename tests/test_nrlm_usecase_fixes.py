"""
NRLM use-case QA fixes — KI-188 to KI-195 (found 2026-10-07, first live run of
the 43 NRLM use cases; see NRLM_UseCase_Test_Report_2026-10-07.xlsx).

Every test calls the REAL function, never a re-implementation (CLAUDE.md §7).
Pure Python: no model, no DB, no KB.

    python -m pytest tests/test_nrlm_usecase_fixes.py -q

One group per defect:

  KI-188  a capped page of rows reported as the whole answer
  KI-189  the token "SHG" resolved to the district South Garo Hills
  KI-190  a rupee total stated with no crore/lakh unit
  KI-191  cumulative CIF/RF attributed to a financial year
  KI-192  a one-block constituency paused for a narrowing that changes nothing
  KI-193  a DATA question answered with a "not in the reference material" refusal
  KI-194  a true zero reported as missing data
  KI-195  a name search matching exactly instead of containing
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_resolver, pipeline as p  # noqa: E402

NRLM = ["NRLM"]


# ── KI-189: "SHG" is not the district South Garo Hills ──────────────────────
# South Garo Hills lists the alias SGH, and acronym_near_misses offers a
# district whose acronym has the same letters in another order. "SHG" is an
# anagram of SGH, so EVERY NRLM question raised an entity-ambiguous pause and
# 6 of the 43 use cases could never be answered. Reproduced 10/10 live.
@pytest.fixture()
def district_catalog(monkeypatch):
    """The real catalogue shape, with the two acronyms this bug turns on."""
    monkeypatch.setattr(entity_resolver, "_catalog", {
        "NRLM": {"district": [
            {"canonical": "South Garo Hills", "acronym": "SGH"},
            {"canonical": "West Khasi Hills", "acronym": "WKH"},
            {"canonical": "East Khasi Hills", "acronym": "EKH"},
        ]},
    }, raising=False)
    monkeypatch.setattr(entity_resolver, "_place_patterns_cache", None, raising=False)


@pytest.mark.parametrize("q", [
    "Give me a district-wise summary of SHG count, active ones, and membership.",
    "What is the average number of members per SHG in each block?",
    "How many SHGs are recorded in the database for Meghalaya?",
    "Which villages have the highest total SHG membership?",
    "Provide a block-wise list of villages with their SHG counts.",
    "Generate a consolidated report showing SHG count, total members by district.",
])
def test_shg_is_never_read_as_south_garo_hills(district_catalog, q):
    assert entity_resolver.acronym_near_misses(q) == {}


def test_a_real_letter_swap_is_still_asked_about(district_catalog):
    """The behaviour the near-miss check exists for must survive the fix."""
    q = "Compare the total disbursement in WHK and EKH for Focus Plus"
    assert entity_resolver.acronym_near_misses(q) == {"WHK": ["WEST KHASI HILLS"]}


@pytest.mark.parametrize("tok", ["SHG", "SHGs", "CIF", "GP", "AC"])
def test_scheme_vocabulary_is_listed(tok):
    assert tok.upper() in entity_resolver._ACRONYM_VOCABULARY


# ── KI-190: a scaled money figure keeps its unit ────────────────────────────
# The SQL divides by 1e7 and says so in the alias (cif_cr), but the composed
# sentence read "...is 98.48." for Rs 98.48 crore — wrong by ten million to the
# officer reading it. This is the NR-07 rupee-unit risk.
def test_bare_crore_value_gets_its_unit():
    out = p._scaled_money_units(
        "The total CIF amount recorded across all SHGs is 98.48.", [{"cif_cr": 98.48}])
    assert "98.48 crore" in out


def test_lakh_alias_gets_lakh():
    out = p._scaled_money_units("The block total is 760.03.", [{"cif_lakh": 760.03}])
    assert "760.03 lakh" in out


def test_a_value_that_already_has_its_unit_is_untouched():
    a = "The total is 98.47 crore, and the highest year was 25.08 crore."
    assert p._scaled_money_units(a, [{"cif_cr": 98.47}, {"cif_cr": 25.08}]) == a


def test_counts_and_percentages_are_not_money():
    a = "There are 38,453 SHGs recorded as New and coverage is 98.48% of the target."
    assert p._scaled_money_units(a, [{"shgs": 38453}, {"cif_cr": 98.48}]) == a


def test_a_longer_number_is_not_a_partial_match():
    a = "the value 98.489 is different"
    assert p._scaled_money_units(a, [{"cif_cr": 98.48}]) == a


def test_every_scaled_value_in_a_breakdown_gets_its_unit():
    out = p._scaled_money_units("WEST JAINTIA HILLS (RF: 5.67, CIF: 18.89)",
                                [{"rf_cr": 5.67, "cif_cr": 18.89}])
    assert "5.67 crore" in out and "18.89 crore" in out


# ── KI-191: NRLM money placed in a year is a cohort figure ──────────────────
# RF and CIF are cumulative and undated; the only year is the SHG's FORMATION
# year. Grouping money by it returns what that cohort holds today, which looks
# exactly like the annual release the officer asked for. The classification
# rules already mark this critical — nothing read them, so it never fired.
_YEAR_MONEY_SQL = ("SELECT formation_financial_year_short, ROUND(SUM(cif_amount)/1e7,2) AS cif_cr "
                   "FROM curated.v_nrlm GROUP BY 1")
_DISTRICT_MONEY_SQL = ("SELECT lgd_district, ROUND(SUM(cif_amount)/1e7,2) AS cif_cr "
                       "FROM curated.v_nrlm GROUP BY 1")
_YEAR_COUNT_SQL = ("SELECT formation_financial_year_short, COUNT(*) AS shgs "
                   "FROM curated.v_nrlm GROUP BY 1")


def test_money_by_formation_year_gets_the_cohort_caveat():
    a = "The highest single-year value was 25.08 crore in 2017-18."
    out = p._nrlm_money_year_caveat(a, _YEAR_MONEY_SQL, NRLM)
    assert out != a
    assert "cumulative" in out.lower() and "formed in" in out.lower()


def test_money_filtered_to_one_year_gets_the_caveat():
    sql = ("SELECT SUM(cif_amount) AS cif FROM curated.v_nrlm "
           "WHERE formation_financial_year_short = '2017-18'")
    assert p._nrlm_money_year_caveat("The total CIF is 25.08 crore.", sql, NRLM) != \
        "The total CIF is 25.08 crore."


def test_money_by_district_is_not_dated_and_needs_no_caveat():
    a = "EAST KHASI HILLS holds 10.66 crore CIF."
    assert p._nrlm_money_year_caveat(a, _DISTRICT_MONEY_SQL, NRLM) == a


def test_shg_counts_by_year_carry_no_money_and_need_no_caveat():
    a = "2020-21 had the most SHGs at 11,354."
    assert p._nrlm_money_year_caveat(a, _YEAR_COUNT_SQL, NRLM) == a


def test_an_answer_that_already_explains_the_cohort_is_not_repeated():
    a = "SHGs FORMED in 2017-18 hold 25.08 crore CIF to date."
    assert p._nrlm_money_year_caveat(a, _YEAR_MONEY_SQL, NRLM) == a


@pytest.mark.parametrize("schemes", [["MGNREGA"], ["Focus Legacy"], ["NRLM", "MGNREGA"], None])
def test_other_schemes_are_untouched(schemes):
    a = "Some answer mentioning 25.08"
    assert p._nrlm_money_year_caveat(a, _YEAR_MONEY_SQL, schemes) == a


# ── KI-188: "how many" must not be answered with a capped page of rows ──────
# "How many SHGs are registered under Umling block?" generated a per-SHG SELECT
# with LIMIT 100; 40 rows came back and the answer said 40. The truth is 1,167.
_ROW_LIST_SQL = ("SELECT shg_code, shg_name, shg_type FROM curated.v_nrlm "
                 "WHERE lgd_block='UMLING' ORDER BY shg_name LIMIT 100")


@pytest.mark.parametrize("q", [
    "How many SHGs are registered under Umling block?",
    "What is the number of SHGs in Umling?",
    "How many SHGs have fewer than 10 female members?",
])
def test_a_count_question_answered_with_rows_is_repaired(q):
    issue = p._nrlm_count_question_listed_rows(q, NRLM, _ROW_LIST_SQL)
    assert issue and "COUNT(*)" in issue


def test_a_count_question_already_counting_passes():
    sql = "SELECT COUNT(*) AS shgs FROM curated.v_nrlm WHERE lgd_block='UMLING'"
    assert p._nrlm_count_question_listed_rows(
        "How many SHGs are registered under Umling block?", NRLM, sql) is None


@pytest.mark.parametrize("q", [
    "List all SHGs registered under Umling block.",
    "Show me all SHGs in Rogu Alda village.",
    "Give me a report of SHGs in East Khasi Hills.",
])
def test_a_list_question_may_return_rows(q):
    """"List"/"show"/"report" ask for the rows; only "how many" asks for a figure."""
    assert p._nrlm_count_question_listed_rows(q, NRLM, _ROW_LIST_SQL) is None


@pytest.mark.parametrize("q", [
    # "the highest NUMBER OF female members" carries the count cue but is a
    # RANKING of SHGs — the rows ARE the answer. Forcing COUNT(*) on it made the
    # question stop being answered at all (caught on the live re-run 2026-10-07).
    "Which SHGs have the highest number of female members?",
    "Which villages have the highest total SHG membership?",
    "Which blocks have the highest number of inactive SHGs?",
])
def test_a_ranking_question_is_not_forced_into_a_count(q):
    ranked = ("SELECT shg_code, shg_name, female_members FROM curated.v_nrlm "
              "ORDER BY female_members DESC LIMIT 100")
    assert p._nrlm_count_question_listed_rows(q, NRLM, ranked) is None


# A question asking for EVERY group must not be truncated. "Compare active and
# inactive SHGs in each district" was grouped by district AND year (360 groups),
# capped at 100, and the 100 rows summed to "13,509 active / 462 inactive across
# all districts" — the truth is 39,432 / 1,197.
_GROUPED_LIMIT_SQL = (
    "SELECT lgd_district, formation_financial_year_short, "
    "COUNT(*) FILTER (WHERE is_active) AS active_shgs "
    "FROM curated.v_nrlm GROUP BY lgd_district, formation_financial_year_short "
    "ORDER BY lgd_district LIMIT 100")


@pytest.mark.parametrize("q", [
    "Compare the number of active and inactive SHGs in each district.",
    "What is the total CIF and RF amount recorded for each district?",
    "Give me a district-wise summary of SHG count and membership.",
])
def test_an_every_group_question_keeps_no_limit(q):
    out = p._nrlm_unrequested_limit(q, NRLM, _GROUPED_LIMIT_SQL)
    assert "LIMIT" not in out.upper()


@pytest.mark.parametrize("q", [
    "Which are the top 5 blocks by SHG count?",
    "Which block has the most inactive SHGs?",
])
def test_an_explicit_ranking_keeps_its_limit(q):
    sql = ("SELECT lgd_block, COUNT(*) n FROM curated.v_nrlm GROUP BY lgd_block "
           "ORDER BY n DESC LIMIT 5")
    assert p._nrlm_unrequested_limit(q, NRLM, sql) == sql


def test_an_ungrouped_list_keeps_its_limit():
    sql = "SELECT shg_code FROM curated.v_nrlm WHERE lgd_block='UMLING' LIMIT 100"
    assert p._nrlm_unrequested_limit("List all SHGs in Umling block.", NRLM, sql) == sql


@pytest.mark.asyncio
async def test_the_true_total_is_found_when_only_the_safety_cap_applied(monkeypatch):
    """A query with no LIMIT of its own is still capped by db.run_readonly, so a
    result sitting exactly on that cap is truncated too. Without this the answer
    reports the page again — the shape UC40 regressed into once the unrequested
    LIMIT was dropped (live re-run 2026-10-07)."""
    sql = ("SELECT lgd_block, village_code, COUNT(*) AS shg_count FROM curated.v_nrlm "
           "GROUP BY lgd_block, village_code")

    async def fake_run_readonly(q, *a, **kw):
        assert "COUNT(*) AS n FROM (" in q      # it counts the model's own query
        return [{"n": 4976}]

    monkeypatch.setattr(p, "run_readonly", fake_run_readonly)
    rows = [{"lgd_block": "X", "village_code": i, "shg_count": 1}
            for i in range(p.settings.SQL_MAX_RESULT_ROWS)]
    assert await p._nrlm_list_total(sql, rows) == 4976


@pytest.mark.asyncio
async def test_a_short_result_has_no_true_total_note(monkeypatch):
    """Nothing was truncated, so no note — and no extra query is run."""
    async def boom(*a, **kw):                   # must not be called
        raise AssertionError("should not re-count a complete result")

    monkeypatch.setattr(p, "run_readonly", boom)
    sql = "SELECT lgd_block FROM curated.v_nrlm GROUP BY lgd_block"
    assert await p._nrlm_list_total(sql, [{"lgd_block": "X"}] * 56) is None


def test_the_count_guard_is_nrlm_only():
    assert p._nrlm_count_question_listed_rows(
        "How many SHGs are registered under Umling block?", ["CM Elevate"], _ROW_LIST_SQL) is None


# ── KI-195: a name search matches names CONTAINING the text ────────────────
# "Find all SHGs with the name Sun Flower" generated shg_name = 'Sun Flower'
# (2 rows); 20 SHG names contain it, so 18 groups were silently dropped.
_NAME_EQ_SQL = ("SELECT shg_code, shg_name FROM curated.v_nrlm "
                "WHERE shg_name = 'Sun Flower' ORDER BY shg_name LIMIT 100")


@pytest.mark.parametrize("q", [
    "Find all SHGs with the name Sun Flower.",
    "List SHGs called Sun Flower",
    "Show me all SHGs named Sun Flower",
])
def test_a_name_search_is_widened_to_contains(q):
    out = p._nrlm_name_search_contains(q, NRLM, _NAME_EQ_SQL)
    assert "ILIKE '%Sun Flower%'" in out and "= 'Sun Flower'" not in out


def test_a_pattern_the_model_already_wrote_is_left_alone():
    sql = "SELECT shg_name FROM curated.v_nrlm WHERE shg_name ILIKE '%Sun Flower%'"
    assert p._nrlm_name_search_contains(
        "Find all SHGs with the name Sun Flower.", NRLM, sql) == sql


def test_a_question_that_is_not_a_name_search_is_left_alone():
    assert p._nrlm_name_search_contains(
        "How many SHGs are active?", NRLM, _NAME_EQ_SQL) == _NAME_EQ_SQL


def test_the_name_widening_is_nrlm_only():
    assert p._nrlm_name_search_contains(
        "Find all SHGs with the name Sun Flower.", ["MGNREGA"], _NAME_EQ_SQL) == _NAME_EQ_SQL


# ── KI-194: a true zero is reported as "none", not as missing data ──────────
# No inactive SHG holds any CIF or RF (NR-15) — a real, meaningful zero. The
# reply was "I couldn't find any matching records in the data available", which
# an officer reads as the data being absent.
def test_inactive_and_funded_is_a_real_zero():
    sql = ("SELECT shg_code, shg_name FROM curated.v_nrlm "
           "WHERE NOT is_active AND (revolving_fund_amount > 0 OR cif_amount > 0) LIMIT 100")
    out = p._nrlm_zero_answer(sql)
    assert out and out.lstrip().startswith("None")
    assert "1,197" in out and "not missing data" in out


def test_the_zero_answer_survives_multi_line_sql():
    """The model writes multi-line SQL. A condition ending ")" then a newline must
    still be recognised, or the guard silently stops firing (caught on the live
    re-run of the use cases, 2026-10-07)."""
    sql = "\n".join([
        "SELECT shg_code, shg_name",
        "FROM curated.v_nrlm",
        "WHERE NOT is_active",
        "  AND (revolving_fund_amount > 0 OR cif_amount > 0)",
        "ORDER BY lgd_district, shg_name",
        "LIMIT 100",
    ])
    out = p._nrlm_zero_answer(sql)
    assert out and out.lstrip().startswith("None") and "1,197" in out


def test_a_self_contained_filter_that_matches_nothing_is_a_zero():
    sql = "SELECT shg_code FROM curated.v_nrlm WHERE shg_type = 'Nonexistent' LIMIT 100"
    out = p._nrlm_zero_answer(sql)
    assert out and out.lstrip().startswith("None")


@pytest.mark.parametrize("sql", [
    # a misspelt place or name CAN be the reason nothing matched — those must
    # keep the plain no-records message, exactly as _cme_zero_programmes_answer does
    "SELECT shg_name FROM curated.v_nrlm WHERE lgd_block='UMLNIG' AND NOT is_active LIMIT 100",
    "SELECT shg_name FROM curated.v_nrlm WHERE shg_name='Typo Name' LIMIT 100",
    "SELECT shg_code FROM curated.v_nrlm WHERE formation_financial_year_short='2099-00' LIMIT 100",
])
def test_a_place_or_name_filter_is_not_claimed_as_a_zero(sql):
    assert p._nrlm_zero_answer(sql) is None


# A COUNTED zero (the non-empty path): one row, one metric, value 0. The
# composer's guidance deliberately will not assert a zero from a 0/NULL cell,
# so "How many SHGs are there?" -> East Garo Hills -> FY 1984-85 answered "the
# data available doesn't cover the count of SHGs", when the count is simply 0
# (one SHG statewide was formed in 1984-85, in West Garo Hills). Found by the
# loop sweep, 2026-10-07.
_ZERO_COUNT_SQL = ("SELECT COUNT(*) AS shgs FROM curated.v_nrlm "
                   "WHERE lgd_block='CHOKPOT' AND formation_financial_year_short='1984-85'")
_ZERO_SUM_SQL = ("SELECT SUM(total_members) AS members FROM curated.v_nrlm "
                 "WHERE lgd_district='EAST GARO HILLS' "
                 "AND formation_financial_year_short='1984-85'")


def test_a_counted_zero_is_stated_as_a_real_zero():
    out = p._nrlm_counted_zero_answer(
        _ZERO_COUNT_SQL, [{"shgs": 0}], {"block": "Chokpot", "year": "FY 1984-85"})
    assert out and "0 SHGs" in out and "real zero" in out
    assert "doesn't cover" not in out


def test_an_aggregate_over_no_rows_is_also_a_real_zero():
    """SUM(...) over nothing comes back as one NULL cell, which _row_metrics drops."""
    out = p._nrlm_counted_zero_answer(
        _ZERO_SUM_SQL, [{"members": None}],
        {"district": "East Garo Hills", "year": "FY 1984-85"})
    assert out and "0 members" in out and "real zero" in out


def test_a_zero_money_total_states_the_unit_and_the_zero():
    """cif_lakh/rf_cr are scaled aliases, so a bare "0" would read ambiguously."""
    sql = ("SELECT ROUND(SUM(cif_amount)/1e5,2) AS cif_lakh FROM curated.v_nrlm "
           "WHERE lgd_block='CHOKPOT' AND formation_financial_year_short='1984-85'")
    out = p._nrlm_counted_zero_answer(
        sql, [{"cif_lakh": None}], {"block": "Chokpot", "year": "FY 1984-85"})
    assert out and "₹0 of CIF" in out and "real zero" in out
    assert "doesn't cover" not in out


@pytest.mark.parametrize("rows,sql,why", [
    ([{"shgs": 517}], _ZERO_COUNT_SQL, "a real figure must be left to the composer"),
    ([{"shgs": 0, "members": 0}], _ZERO_COUNT_SQL, "two metrics is a breakdown, not this shape"),
    ([{"shgs": 0}], "SELECT COUNT(*) AS shgs FROM curated.v_nrlm", "no WHERE: nothing was filtered"),
    ([{"weird_metric": 0}], _ZERO_COUNT_SQL, "an unrecognised column may not be a count"),
    ([{"shgs": 0}], "SELECT cif_amount AS shgs FROM curated.v_nrlm WHERE x=1",
     "a bare column read is not a counted zero"),
])
def test_the_counted_zero_answer_stays_narrow(rows, sql, why):
    assert p._nrlm_counted_zero_answer(sql, rows, {}) is None, why


# ── KI-193: a DATA question never gets a "not in my documents" refusal ──────
# "Which SHGs have received the highest RF amounts?" fell back to the KB, which
# replied that the reference material does not identify them — while the DB
# holds the answer. Same reasoning as KI-025.
@pytest.mark.parametrize("text", [
    "The provided reference material does not contain information identifying which specific "
    "SHGs have received the highest Revolving Fund amounts.",
    "The reference documents do not mention a specific Producer Group named Sakania.",
    "No information is available in the provided reference documents.",
])
def test_a_kb_refusal_is_recognised(text):
    assert p._KB_DISCLAIMS_RE.search(text)


@pytest.mark.parametrize("text", [
    "NRLM provides a Revolving Fund of Rs 15,000 to each eligible SHG.",
    "SHGs are eligible for CIF once they complete the panchasutra requirements.",
])
def test_a_real_knowledge_answer_is_not_mistaken_for_a_refusal(text):
    assert not p._KB_DISCLAIMS_RE.search(text)


# ── Two verifier false positives that killed NRLM questions outright ────────
# Both were found on the live re-run after the fixes above: the question reached
# a correct query, the verifier rejected it on every repair, and the answer died
# in the KB fallback.
_NRLM_GRAIN_ISSUE = (
    "Check 3: Table/grain - The question asks for the 'highest RF amounts' (a total/"
    "aggregation metric), but the SQL selects revolving_fund_amount as a column without "
    "any aggregation (SUM, MAX, etc.). This returns individual row values instead of the "
    "aggregated total requested.")


def test_an_aggregate_demanded_on_a_per_shg_column_is_discarded():
    """One v_nrlm row IS one SHG, so ranking SHGs by their own RF needs no SUM."""
    sql = ("SELECT shg_code, shg_name, revolving_fund_amount FROM curated.v_nrlm "
           "ORDER BY revolving_fund_amount DESC LIMIT 100")
    assert p._verifier_nrlm_grain_complaint_is_false(_NRLM_GRAIN_ISSUE, NRLM, sql)


def test_a_grain_complaint_on_a_grouped_query_still_raises():
    sql = "SELECT lgd_district, SUM(cif_amount) FROM curated.v_nrlm GROUP BY lgd_district"
    assert not p._verifier_nrlm_grain_complaint_is_false(_NRLM_GRAIN_ISSUE, NRLM, sql)


def test_the_grain_filter_is_nrlm_only():
    sql = ("SELECT shg_code, revolving_fund_amount FROM curated.v_nrlm "
           "ORDER BY revolving_fund_amount DESC LIMIT 100")
    assert not p._verifier_nrlm_grain_complaint_is_false(_NRLM_GRAIN_ISSUE, ["MGNREGA"], sql)


def test_the_formation_year_column_satisfies_the_resolved_year_key():
    """NRLM stores its only year as formation_financial_year_short = '2017-18'.
    The verifier demanded year_key = 2017 on every repair and the question died."""
    issue = ("Check 2: The RESOLVED ENTITIES block specifies year_key = 2017, but the SQL "
             "filters on formation_financial_year_short = '2017-18'.")
    sql = ("SELECT SUM(cif_amount) FROM curated.v_nrlm "
           "WHERE formation_financial_year_short = '2017-18'")
    assert p._verifier_year_complaint_is_false(issue, {"year_key": 2017}, sql)


def test_a_genuine_year_mismatch_still_raises():
    issue = ("Check 2: The RESOLVED ENTITIES block specifies year_key = 2017, but the SQL "
             "filters on formation_financial_year_short = '2019-20'.")
    sql = ("SELECT SUM(cif_amount) FROM curated.v_nrlm "
           "WHERE formation_financial_year_short = '2019-20'")
    assert not p._verifier_year_complaint_is_false(issue, {"year_key": 2017}, sql)


# ── KI-192: a one-block constituency has nothing to narrow to ───────────────
# "Show me active SHGs in Rongjeng constituency" (1 block, 144 villages) paused
# offering "the whole constituency" or "Dambo Rongjeng block" — the same rows —
# and never resolved. The answer, 921 active SHGs, was available all along.
def test_a_single_block_constituency_does_not_pause():
    assert p._ac_drilldown_clarification(
        "Show me active SHGs in Rongjeng constituency", "Rongjeng",
        {"districts": ["EAST GARO HILLS"], "blocks": ["DAMBO RONGJENG"], "villages": 144},
        "NRLM") is None


def test_a_multi_block_constituency_still_asks():
    """The narrowing is a real choice when the constituency spans more than one block."""
    cn = p._ac_drilldown_clarification(
        "Show me active SHGs in Mawkyrwat constituency", "Mawkyrwat",
        {"districts": ["SOUTH WEST KHASI HILLS"],
         "blocks": ["MAWKYRWAT", "RANIKOR"], "villages": 300}, "NRLM")
    assert cn is not None and cn.rule == "ac-narrow-scope"
    assert any("whole" in o["label"].lower() for o in cn.options)


def test_a_constituency_spanning_two_districts_still_asks():
    cn = p._ac_drilldown_clarification(
        "Show me active SHGs in Someplace constituency", "Someplace",
        {"districts": ["EAST KHASI HILLS", "RI BHOI"],
         "blocks": ["MAWPHLANG", "UMLING"], "villages": 200}, "NRLM")
    assert cn is not None
