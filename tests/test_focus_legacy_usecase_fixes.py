"""
Focus Legacy use-case QA fixes (Use_Cases_-_Focus.csv, 2026-09-25).

Round 2 of the QA found these bot defects; each test pins the fix to the real
production function, and the neighbouring behaviour it must NOT change.

  TC-13  "Is there any Producer Group named Nongstoin PG?" went to the scheme
         recommender, then became a block-vs-village question.
  TC-14b / TC-15  group size SUMmed across a group's payments (20 -> 40).
  TC-18  entity_type <> 'Unresolved' dropped 11 no-village payments from a
         district total.
  TC-19  541 groups qualified; the answer listed 23 and never said so.
  TC-12  repeat-paid groups called "duplicates".
  TC-20 / TC-22 / TC-25  the SQL verifier rejected correct one-table SQL
         ("prohibited join", check 2 with nothing resolved).
  TC-23  the extractor dropped "FY 2024-25"; the scope chip then dropped the FY.
  TC-09  the briefing retrieved the institutions chunks, not the overview.

Pure Python — no model or DB.
    python -m pytest tests/test_focus_legacy_usecase_fixes.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import annotations  # noqa: E402
from app import pipeline as p  # noqa: E402

FL = ["Focus Legacy"]


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()


# ── TC-18: the Unresolved exclusion stays off non-village Focus Legacy SQL ───
def test_district_total_keeps_no_village_payments():
    sql = ("SELECT SUM(amount_disbursed) AS amount_disbursed FROM curated.v_focus_legacy "
           "WHERE lgd_district = 'EAST KHASI HILLS' AND entity_type <> 'Unresolved' LIMIT 1")
    out = p._cm_legacy_keep_unresolved_off_village(
        "What is the total amount disbursed for East Khasi Hills?", FL, sql)
    assert "Unresolved" not in out
    assert "lgd_district = 'EAST KHASI HILLS'" in out


def test_village_count_keeps_the_exclusion():
    sql = ("SELECT COUNT(DISTINCT village_code) FROM curated.v_focus_legacy "
           "WHERE entity_type <> 'Unresolved'")
    assert p._cm_legacy_keep_unresolved_off_village("How many villages?", FL, sql) == sql


def test_other_schemes_untouched():
    sql = "SELECT COUNT(*) FROM curated.v_pmay WHERE entity_type <> 'Unresolved'"
    assert p._cm_legacy_keep_unresolved_off_village("total houses", ["PMAY-G"], sql) == sql


# ── TC-23: an explicit FY the extractor dropped is back-filled ────────────────
def test_explicit_fy_backfilled():
    q = "What was the total amount remitted in FY 2024-25 for Focus Legacy"
    assert p._backfill_explicit_year(q, FL, {}) == {"year": "2024-25"}


@pytest.mark.parametrize("q", [
    "Compare FY 2022-23 and FY 2024-25",          # two years: not a single-year slot
    "Total remitted in FY 2023-24",                # absent year: the gap guard's job
    "Total remitted in 2024",                      # bare year: left to the extractor
])
def test_backfill_leaves_other_shapes_alone(q):
    assert p._backfill_explicit_year(q, FL, {}) == {}


def test_backfill_never_overrides_the_extractor():
    assert p._backfill_explicit_year("FY 2024-25", FL, {"year": "2022-23"}) == {"year": "2022-23"}


# ── TC-13: a PG name lookup is not a recommendation, nor a place ─────────────
def test_pg_name_lookup_is_not_a_recommendation():
    assert not p._is_recommendation_request("Is there any Producer Group named Nongstoin PG?")
    assert not p._is_recommendation_request("is there any pg called Sakania?")


@pytest.mark.parametrize("q", [
    "I am in a farmers' group, suggest a scheme",
    "my friend named Ram is a farmer, suggest me a scheme",
])
def test_real_recommendations_still_fire(q):
    assert p._is_recommendation_request(q)


def test_place_scans_do_not_see_the_group_name():
    assert "Nongstoin" not in p._question_without_pg_name(
        "Is there any Producer Group named Nongstoin PG?")
    q = "Total disbursement in Nongstoin block"
    assert p._question_without_pg_name(q) == q


# Reported live 2026-09-25: after a Betasing-block question, "is there any
# producer group named sakania pg?" was rewritten as a follow-up ("...in Betasing
# block") and answered "no such group" — the "there" of "is there" read as a
# back-reference. A name lookup is standalone unless it points back explicitly.
@pytest.mark.parametrize("q", [
    "is there any producer group named nongstoin pg?",
    "is there any producer group named sakania pg?",
    "is there any pg called sakania?",
    "Is there any Producer Group named Nongstoin PG in Betasing block?",
])
def test_name_lookup_is_not_a_followup(q):
    assert not p.looks_like_followup(q)


@pytest.mark.parametrize("q", [
    "is there any producer group named sakania pg there?",
    "is there a pg named sakania in that block?",
    "what about East Garo Hills?",
    "is there any data for it?",
])
def test_real_followups_unchanged(q):
    assert p.looks_like_followup(q)


def test_group_name_followed_by_a_place_is_still_a_group_name():
    q = "Is there any Producer Group named Nongstoin PG in Betasing block?"
    assert p._PG_NAMED_ENTITY.search(q).group("name") == "Nongstoin PG"
    assert "Betasing" in p._question_without_pg_name(q)
    assert "Nongstoin" not in p._question_without_pg_name(q)


# Bulk QA (290 sampled groups, 2026-09-25): group-name questions are answered
# deterministically. The parser must catch every group phrasing and leave
# place questions to the normal pipeline.
@pytest.mark.parametrize("q,kind,name", [
    ("How many members are there in Chelchak Pineapple P.g?", "size", "Chelchak Pineapple P.g"),
    ("How many members are there in Bak-15 Wachal Pg for all of Meghalaya, all years", "size", "Bak-15 Wachal Pg"),
    ("How many members are there in Teinam for Focus Legacy for all of Meghalaya, all years", "size", "Teinam"),
    ("How many members does Iatreilang have?", "size", "Iatreilang"),
    ("is there any producer group named sakania pg?", "exists", "sakania pg"),
    ("how many members are there in nongstoin pg?", "size", "nongstoin pg"),
])
def test_group_name_questions_are_parsed(q, kind, name):
    assert p._pg_name_question(q) == (kind, name)


@pytest.mark.parametrize("q", [
    "How many members are there in West Garo Hills?",
    "What is the total number of PG members in West Garo Hills?",
    "How many PG members were covered in Betasing block for Focus Legacy across all financial years?",
    "Is there any Producer Group named Sakania PG in Betasing block?",   # carries its own place
])
def test_place_questions_are_not_group_lookups(q):
    assert p._pg_name_question(q) is None


def test_group_name_words_ignore_suffix_and_punctuation():
    assert p._pg_name_tokens("Chelchak Pineapple P.g") == ["chelchak", "pineapple"]
    assert p._pg_name_tokens("Bak-15 Wachal Producer Group") == ["bak", "15", "wachal"]


def test_empty_raw_geo_columns_are_read_from_lgd():
    sql = ("SELECT COUNT(DISTINCT pg_id) FROM curated.v_focus_legacy "
           "WHERE UPPER(TRIM(block_name_raw)) = 'NONGSTOIN'")
    out = p._focus_legacy_geo_columns(FL, sql)
    assert "lgd_block" in out and "block_name_raw" not in out
    assert p._focus_legacy_geo_columns(["CM Elevate Legacy"], sql) == sql


# ── TC-14b / TC-15: group size is MAX per group, never a SUM of payments ─────
def test_group_size_sum_is_caught():
    q = "List top 5 PGs which has more than 15 members"
    sql = ("SELECT pg_id, MAX(pg_name), SUM(no_of_pg_members) AS memberships FROM "
           "curated.v_focus_legacy GROUP BY pg_id HAVING SUM(no_of_pg_members) > 15")
    assert p._focus_legacy_group_size_summed(q, FL, sql)
    sql1 = ("SELECT SUM(no_of_pg_members) AS memberships FROM curated.v_focus_legacy "
            "WHERE pg_name ILIKE '%umtyrkhow%'")
    assert p._focus_legacy_group_size_summed("How many members are there in Umtyrkhow Pg?", FL, sql1)


def test_membership_totals_keep_their_sum():
    # TC-16 / TC-17: memberships paid for, district or statewide — SUM is right.
    sql = ("SELECT SUM(no_of_pg_members) AS memberships FROM curated.v_focus_legacy "
           "WHERE lgd_district = 'WEST GARO HILLS'")
    assert not p._focus_legacy_group_size_summed(
        "What is the total number of PG members in West Garo Hills?", FL, sql)
    # A money question carries SUM(no_of_pg_members) beside the amount by rule.
    money = ("SELECT pg_id, SUM(no_of_pg_members), SUM(amount_disbursed) FROM "
             "curated.v_focus_legacy GROUP BY pg_id HAVING SUM(amount_disbursed) > 100000")
    assert not p._focus_legacy_group_size_summed(
        "Which Producer Groups received more than Rs 1,00,000?", FL, money)
    assert not p._focus_legacy_group_size_summed(
        "How many members are there in X?", ["Focus Plus"],
        "SELECT SUM(no_of_pg_members) FROM t WHERE pg_name ILIKE '%x%'")


def test_group_size_examples_in_the_bank():
    top = annotations.few_shot_examples(FL, "How many members are there in Bak 15 Banana Dijogre?",
                                        top_k=3)
    assert any("MAX(no_of_pg_members)" in e["sql"] for e in top)


# ── TC-12: duplicates wording ────────────────────────────────────────────────
def test_duplicate_question_gets_the_repeat_payment_note():
    notes = p._focus_legacy_answer_notes("Are there any duplicate Producer Groups there?")
    assert notes and "REPEAT PAYMENT" in notes[0]
    assert p._focus_legacy_answer_notes("How many producer groups are there?") == []


# ── TC-20 / TC-22 / TC-25 / TC-23: verifier false positives ──────────────────
def test_prohibited_join_on_a_one_table_query_is_discarded():
    issue = ("Check 1: The SQL joins directly to 'curated.v_focus_legacy', which is a prohibited "
             "join. The rules state: 'NEVER join any -> curated.v_focus_legacy directly.'")
    sql = ("SELECT financial_year_short, COUNT(*) AS records FROM curated.v_focus_legacy "
           "GROUP BY financial_year_short")
    assert p._verifier_join_complaint_is_false(issue, sql)
    absent = ("PROHIBITED JOIN violated. The SQL joins directly to "
              "'curated.fact_focus_legacy_disbursement' instead of curated.v_focus_legacy")
    assert p._verifier_join_complaint_is_false(absent, sql)


def test_a_real_prohibited_join_still_raises():
    issue = "Check 1: prohibited join to curated.fact_pmay_house"
    sql = ("SELECT * FROM curated.v_focus_legacy f JOIN curated.fact_pmay_house h "
           "ON h.geography_key = f.geography_key")
    assert not p._verifier_join_complaint_is_false(issue, sql)
    assert not p._verifier_join_complaint_is_false("Check 4: wrong metric column", "SELECT 1")


def test_check2_with_nothing_resolved_is_discarded():
    issue = ("Check 2: The RESOLVED ENTITIES block is empty ... the SQL is filtering/aggregating "
             "on a dimension (district) that was not resolved")
    assert p._verifier_check2_on_empty_entities(issue, {})
    assert not p._verifier_check2_on_empty_entities(issue, {"district": "EAST KHASI HILLS"})
    assert not p._verifier_check2_on_empty_entities("Check 4: wrong metric", {})


def test_fy_label_filter_matches_the_resolved_year():
    issue = ("Check 2: The RESOLVED ENTITIES block specifies year_key = 2024, but the SQL "
             "filters on financial_year_short = '2024-25'.")
    ok = "SELECT SUM(amount_disbursed) FROM curated.v_focus_legacy WHERE financial_year_short = '2024-25'"
    bad = "SELECT SUM(amount_disbursed) FROM curated.v_focus_legacy WHERE financial_year_short = '2022-23'"
    assert p._verifier_year_complaint_is_false(issue, {"year_key": 2024}, ok)
    assert not p._verifier_year_complaint_is_false(issue, {"year_key": 2024}, bad)


# ── TC-09: an overview/briefing request ─────────────────────────────────────
def test_overview_request_detected():
    assert p._OVERVIEW_REQUEST.search(
        "Give me a short overview of Focus Legacy that I can use for an official briefing")
    assert not p._OVERVIEW_REQUEST.search("Who can benefit under the Focus Legacy scheme")
