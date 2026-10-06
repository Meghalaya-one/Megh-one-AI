"""
CM Elevate use-case QA fixes (CM Elevate.csv, 2026-09-27).

The live QA (23 / 30) found these bot defects (KNOWN_ISSUES KI-068 … KI-075);
each test pins the fix to the real production function. The SQL and answers
below are the ones the bot produced in that QA.

  OFF-005      programme totals dropped the 51 Unresolved (no-village)
               applications and the text named 5 of 15 programmes (KI-068).
  OFF-009b/029b  two named programmes → one merged count, no split (KI-069).
  OFF-018a     "pending at level 2" → level 2 AND On Hold = 0, "doesn't
               cover" (KI-070).
  OFF-027      comparison with no difference (KI-071).
  OFF-028b     "pending in each sector" lost every 0-pending sector (KI-072).
  OFF-025a     scheme x sector answer garbled ("respectively … each
               corresponding row") (KI-073).
  KI-074       "pending" = verification On Hold only (decided 2026-09-28).
  KI-075       "PRIME SEED" asked "which scheme?".

No model or DB: run_readonly is stubbed where needed.
    python -m pytest tests/test_cmelevate_usecase_fixes.py -q
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402

CME = ["CM Elevate"]
SEED = "PRIME Small Enterprise Empowerment and Development (SEED)"
PIG, POU, DAI = ("Meghalaya Piggery Development Scheme", "Meghalaya Poultry Farming Scheme",
                 "Meghalaya Dairy Development Scheme")


def run(coro):
    return asyncio.run(coro)


# ── KI-068: Unresolved placeholder kept off non-village totals ───────────────
def test_programme_totals_keep_unresolved_applications():
    sql = ("SELECT scheme_name, COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate "
           "WHERE entity_type <> 'Unresolved' GROUP BY scheme_name ORDER BY applicants DESC LIMIT 100")
    out = p._cm_legacy_keep_unresolved_off_village(
        "How many applicants are there under each CM ELEVATE program?", CME, sql)
    assert "Unresolved" not in out and "GROUP BY scheme_name" in out


def test_village_counts_keep_the_unresolved_filter():
    sql = ("SELECT lgd_village_name, COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate "
           "WHERE scheme_name = 'Meghalaya Poultry Farming Scheme' AND lgd_block = 'MAWPHLANG' "
           "AND entity_type <> 'Unresolved' GROUP BY lgd_village_name")
    assert p._cm_legacy_keep_unresolved_off_village("in each village of Mawphlang block", CME, sql) == sql


def test_programme_list_naming_five_of_fifteen_is_rebuilt():
    rows = [{"scheme_name": n, "applicants": v} for n, v in
            [(SEED, 3633), (PIG, 3010), (POU, 516), ("PRIME Tourism Vehicle Scheme", 422),
             ("Chief Minister's Green Taxi Scheme", 10), ("Meghalaya Cinema Theatre Scheme", 2)]]
    ans = ("The Chief Minister's Green Taxi Scheme has 10 applicants, while the Meghalaya Cinema Theatre "
           "Scheme has 2 applicants. The remaining schemes range up to 3,633 applicants for PRIME SEED.")
    out = p._cme_complete_list("How many applicants are there under each CM ELEVATE program?", ans, rows)
    for part in ("PRIME SEED 3,633", "Piggery Development 3,010", "Poultry Farming 516",
                 "PRIME Tourism Vehicle 422", "Cinema Theatre 2"):
        assert part in out
    assert "Total 7,593" in out


def test_top_few_answer_to_a_which_is_highest_question_is_not_rebuilt():
    rows = [{"scheme_name": PIG, "pending_applications": 472}, {"scheme_name": SEED, "pending_applications": 286},
            {"scheme_name": POU, "pending_applications": 65}, {"scheme_name": DAI, "pending_applications": 30}]
    ans = "Meghalaya Piggery Development Scheme has the highest number of pending applications with 472."
    q = "Which CM ELEVATE programs have the highest number of pending applications?"
    assert p._cme_complete_list(q, ans, rows) == ans


def test_status_breakdown_with_unbracketed_not_recorded_is_kept():
    rows = [{"status": "Valid", "applications": 2028}, {"status": "On Hold", "applications": 492},
            {"status": "(not recorded)", "applications": 18}]
    ans = "The application status distribution in Ri Bhoi shows 2,028 Valid, 492 On Hold, and 18 not recorded."
    assert p._cme_complete_list("What is the application status distribution in Ri Bhoi?", ans, rows) == ans


# ── KI-069: a merged multi-programme count is split by programme ─────────────
def test_merged_two_programme_count_is_split_by_scheme_name():
    sql = ("SELECT COUNT(DISTINCT request_id) AS applicants\nFROM curated.v_cm_elevate\n"
           "WHERE lgd_district = 'EAST KHASI HILLS'\n"
           f"  AND scheme_name IN ('{SEED}', 'PRIME Tourism Vehicle Scheme')\nLIMIT 1")
    out = p._cm_elevate_split_scheme_in_list(CME, sql)
    assert out.startswith("SELECT scheme_name, COUNT(DISTINCT request_id)")
    assert "GROUP BY scheme_name" in out and "LIMIT 1" not in out
    assert "(SEED)" in out   # the literal with its own parenthesis is untouched


def test_split_adds_scheme_name_to_an_existing_group_by():
    sql = ("SELECT lgd_district, COUNT(*) AS n FROM curated.v_cm_elevate "
           "WHERE scheme_name IN ('A', 'B') GROUP BY lgd_district")
    out = p._cm_elevate_split_scheme_in_list(CME, sql)
    assert "SELECT scheme_name, lgd_district" in out and "GROUP BY scheme_name, lgd_district" in out


@pytest.mark.parametrize("schemes,sql", [
    (["CM Elevate Legacy"], "SELECT COUNT(*) FROM curated.v_cm_elevate_disbursement WHERE scheme_name IN ('A','B')"),
    (CME, "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE scheme_name IN ('A')"),
    (CME, "SELECT scheme_name, COUNT(*) FROM curated.v_cm_elevate WHERE scheme_name IN ('A','B') GROUP BY scheme_name"),
])
def test_split_leaves_other_shapes_alone(schemes, sql):
    assert p._cm_elevate_split_scheme_in_list(schemes, sql) == sql


def test_programme_split_answer_gets_each_figure_and_the_combined_total():
    sql = f"SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate WHERE scheme_name IN ('{SEED}', '{DAI}') GROUP BY scheme_name"
    rows = [{"scheme_name": SEED, "applications": 1172}, {"scheme_name": DAI, "applications": 41}]
    ans = "There are 1213 valid applications under PRIME SEED and Dairy in West Garo Hills."
    out = p._cme_multi_scheme_total("How many applications under PRIME SEED and Dairy are currently Valid "
                                    "in West Garo Hills?", sql, rows, ans)
    assert "PRIME SEED: 1,172" in out and "Dairy Development: 41" in out
    assert "Combined" not in out   # 1213 was already stated


def test_combined_total_added_when_missing():
    sql = f"SELECT scheme_name, COUNT(*) AS n FROM curated.v_cm_elevate WHERE scheme_name IN ('{PIG}', '{POU}') GROUP BY scheme_name"
    rows = [{"scheme_name": PIG, "n": 400}, {"scheme_name": POU, "n": 48}]
    ans = "Piggery has 400 and Poultry has 48 applications on hold."
    out = p._cme_multi_scheme_total("on hold under Piggery and Poultry", sql, rows, ans)
    assert "Combined across these 2 programmes: 448" in out


# ── KI-182: a named programme with no applications is stated as 0 ───────────
# The SQL and answers below are the bot's own, from the OFF-009 all-pairs re-test
# (2026-10-05): one row back, the 0 programme silently missing from the answer.
CIN, WAR, TAXI = ("Meghalaya Cinema Theatre Scheme", "Meghalaya Warehouse Scheme", "Chief Minister's Green Taxi Scheme")


def _pair_sql(a, b, tail="GROUP BY scheme_name\nLIMIT 1000"):
    lit = lambda s: s.replace("'", "''")  # noqa: E731
    return ("SELECT scheme_name, COUNT(DISTINCT request_id) AS applicants\nFROM curated.v_cm_elevate\n"
            f"WHERE lgd_district = 'SOUTH GARO HILLS'\n  AND scheme_name IN ('{lit(a)}', '{lit(b)}')\n{tail}")


def test_zero_programme_named_only_one_is_stated_as_zero():
    rows = [{"scheme_name": WAR, "applicants": 10}]
    out = p._cme_multi_scheme_total("How many applicants are there in South Garo Hills under Meghalaya Cinema "
                                    "Theatre Scheme and Meghalaya Warehouse Scheme?", _pair_sql(CIN, WAR), rows,
                                    "Meghalaya Warehouse Scheme: 10 applicants.")
    # user screenshot 2026-10-05: one clear explanation, in the question's order
    assert out == ("There are 10 applicants in South Garo Hills across the 2 programmes you asked about:\n"
                   "- Meghalaya Cinema Theatre Scheme: 0 — no applicants recorded in South Garo Hills\n"
                   "- Meghalaya Warehouse Scheme: 10 applicants\n\n"
                   "All 10 applicants are under the Meghalaya Warehouse Scheme; the Meghalaya Cinema Theatre Scheme "
                   "has none in South Garo Hills.")


def test_zero_programme_merged_into_one_figure_is_split_out():
    # "There are 12 applicants … under A and B" read as if 12 covered both
    rows = [{"scheme_name": "PRIME Agriculture Response Vehicle Scheme", "applicants": 12}]
    ans = ("There are 12 applicants in East Jaintia Hills under the PRIME Agriculture Response Vehicle Scheme "
           "and Chief Minister's Green Taxi Scheme.")
    out = p._cme_multi_scheme_total("q", _pair_sql("PRIME Agriculture Response Vehicle Scheme", TAXI), rows, ans)
    assert "- PRIME Agriculture Response Vehicle Scheme: 12 applicants" in out
    assert "- Chief Minister's Green Taxi Scheme: 0 — no applicants recorded" in out
    assert ans not in out                      # the misleading merged sentence is gone


def test_one_applicant_is_singular():
    rows = [{"scheme_name": "Meghalaya Goat Farming Scheme", "applicants": 1}]
    ans = ("There is 1 applicant under the Meghalaya Goat Farming Scheme in East Jaintia Hills. No applicants "
           "are recorded for the Meghalaya Sports & Wellness Centre Scheme in this area.")
    sql = _pair_sql("Meghalaya Sports & Wellness Centre Scheme", "Meghalaya Goat Farming Scheme")
    out = p._cme_multi_scheme_total("q", sql, rows, ans)
    assert out.startswith("There is 1 applicant in South Garo Hills across the 2 programmes")
    assert "The only applicant is under the Meghalaya Goat Farming Scheme" in out


def test_no_separate_count_is_not_a_stated_zero():
    rows = [{"scheme_name": "Meghalaya Dairy Development Scheme", "applicants": 1}]
    ans = ("West Jaintia Hills has 1 applicant under the Meghalaya Dairy Development Scheme and Meghalaya "
           "Motorcaravan Scheme. The result shows 1 applicant for the Meghalaya Dairy Development Scheme, with no "
           "separate count provided for the Meghalaya Motorcaravan Scheme.")
    out = p._cme_multi_scheme_total("q", _pair_sql("Meghalaya Dairy Development Scheme",
                                                   "Meghalaya Motorcaravan Scheme"), rows, ans)
    assert "- Meghalaya Motorcaravan Scheme: 0 — no applicants recorded" in out
    assert "no separate count" not in out


def test_three_programmes_one_zero_gets_zero_and_total():
    sql = (f"SELECT scheme_name, COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate "
           f"WHERE lgd_district = 'RI BHOI' AND scheme_name IN ('{PIG}', '{POU}', '{CIN}') GROUP BY scheme_name")
    rows = [{"scheme_name": PIG, "applicants": 400}, {"scheme_name": POU, "applicants": 48}]
    out = p._cme_multi_scheme_total("q", sql, rows, "Piggery has 400 and Poultry has 48 applicants.")
    assert out.startswith("There are 448 applicants in Ri Bhoi across the 3 programmes you asked about:")
    assert "- Meghalaya Cinema Theatre Scheme: 0 — no applicants recorded in Ri Bhoi" in out
    assert out.endswith("The Meghalaya Cinema Theatre Scheme has no applicants in Ri Bhoi, so the total comes from "
                        "the other 2 programmes.")


def test_programmes_listed_in_the_order_the_question_names_them():
    # live 2026-10-05: the SQL's IN list put Any Business Venture first; the officer asked Motorcaravan first
    rows = [{"scheme_name": "Meghalaya Any Business Venture Scheme", "applicants": 28}]
    out = p._cme_multi_scheme_total(
        "How many applicants are there in South Garo Hills under Meghalaya Motorcaravan Scheme and Meghalaya Any "
        "Business Venture Scheme?", _pair_sql("Meghalaya Any Business Venture Scheme", "Meghalaya Motorcaravan Scheme"),
        rows, "x")
    assert out.index("Motorcaravan Scheme: 0") < out.index("Any Business Venture Scheme: 28")


def test_duplicated_group_by_still_gets_the_clear_answer():
    # live 2026-10-05: "GROUP BY scheme_name, scheme_name ORDER BY scheme_name LIMIT 100"
    sql = _pair_sql(TAXI, SEED, "GROUP BY scheme_name, scheme_name ORDER BY scheme_name LIMIT 100")
    out = p._cme_multi_scheme_total("q", sql, [{"scheme_name": SEED, "applicants": 124}],
                                    f"{SEED}: 124 applicants.")
    assert out.startswith("There are 124 applicants in South Garo Hills across the 2 programmes you asked about:")


def test_zero_with_on_hold_names_the_status_in_every_line():
    # live 2026-10-05: "on hold in WGH under cinema theatre and piggery" -> "…Piggery…: 20 applications." (no "on hold")
    sql = (f"SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_district = "
           f"'WEST GARO HILLS' AND scheme_name IN ('{CIN}', '{PIG}') AND data_verified = 'On Hold' GROUP BY scheme_name")
    out = p._cme_multi_scheme_total("q", sql, [{"scheme_name": PIG, "applications": 20}],
                                    "Meghalaya Piggery Development Scheme: 20 applications.")
    assert out.startswith("There are 20 applications on hold in West Garo Hills across the 2 programmes")
    assert "- Meghalaya Cinema Theatre Scheme: 0 — no applications on hold recorded in West Garo Hills" in out
    assert "- Meghalaya Piggery Development Scheme: 20 applications on hold" in out


def test_pending_at_level_one_is_named_and_singular():
    sql = (f"SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_district = "
           f"'RI BHOI' AND LOWER(current_level) = 'level1' AND scheme_name IN ('{CIN}', '{WAR}') GROUP BY scheme_name")
    out = p._cme_multi_scheme_total("q", sql, [{"scheme_name": WAR, "applications": 1}], "x")
    assert out.startswith("There is 1 application pending at level 1 in Ri Bhoi")
    assert "The only application pending at level 1 is under the Meghalaya Warehouse Scheme" in out


def test_zero_with_a_filter_the_wording_cannot_name_keeps_the_old_line():
    # a sector filter: the rebuilt wording would drop it, so the KI-182 line is appended instead
    sql = _pair_sql(CIN, WAR).replace("GROUP BY", "AND scheme_specific ->> 'sector_id' = 'Piggery'\nGROUP BY")
    ans = "Meghalaya Warehouse Scheme: 3 applicants in the Piggery sector."
    out = p._cme_multi_scheme_total("q", sql, [{"scheme_name": WAR, "applicants": 3}], ans)
    assert out.startswith(ans) and "Cinema Theatre: 0 (none recorded); Warehouse: 3." in out


@pytest.mark.parametrize("sql", [
    # a misspelt literal also returns no row — that is not a 0
    _pair_sql("Meghalaya Cinema Theater Scheme", WAR),
    # a LIMIT that can drop a group, a HAVING, an OR: absence is not a 0
    _pair_sql(CIN, WAR, "GROUP BY scheme_name ORDER BY applicants DESC LIMIT 1"),
    _pair_sql(CIN, WAR, "GROUP BY scheme_name HAVING COUNT(*) > 5"),
    _pair_sql(CIN, WAR).replace("AND scheme_name", "OR scheme_name"),
    # an average of nothing is not 0
    _pair_sql(CIN, WAR).replace("COUNT(DISTINCT request_id) AS applicants", "AVG(loan_amount) AS applicants"),
])
def test_absence_that_is_not_a_zero_is_not_filled(sql):
    rows = [{"scheme_name": WAR, "applicants": 10}]
    ans = "Meghalaya Warehouse Scheme: 10 applicants."
    assert p._cme_multi_scheme_total("q", sql, rows, ans) == ans


# ── both named programmes 0: an empty result is answered as 0, not "no records" ──
# User screenshot 2026-10-05: "How many applicants are there in SWKH under agro tourism
# and green taxi scheme?" -> "I couldn't find any matching records for South West Khasi Hills, …"
AGRO = "Agro Tourism Villa Scheme"
SWKH_SQL = ("SELECT scheme_name, COUNT(DISTINCT request_id) AS applicants\nFROM curated.v_cm_elevate\n"
            "WHERE lgd_district = 'SOUTH WEST KHASI HILLS'\n"
            f"  AND scheme_name IN ('Chief Minister''s Green Taxi Scheme', '{AGRO}')\nGROUP BY scheme_name\nLIMIT 1000")


def _place_lookup(monkeypatch, found=True):
    seen = []

    async def fake(sql, params=None):
        seen.append((sql, params))
        return [{"hit": 1}] if found else []
    monkeypatch.setattr(p, "fetch_rows", fake)
    return seen


def test_empty_two_programme_count_in_a_real_district_says_zero(monkeypatch):
    seen = _place_lookup(monkeypatch)
    out = run(p.compose_response("How many applicants are there in SWKH under agro tourism and green taxi scheme?",
                                 SWKH_SQL, [], entities={"district": "South West Khasi Hills"}, schemes=CME))
    assert out == ("There are no applicants under the Chief Minister's Green Taxi Scheme or the Agro Tourism Villa "
                   "Scheme in South West Khasi Hills — the data records 0 for each.\n\n"
                   "Chief Minister's Green Taxi: 0; Agro Tourism Villa: 0.")
    # the place is checked with the app's own bound query, never the model's SQL
    assert seen == [("SELECT 1 AS hit FROM curated.v_cm_elevate WHERE lgd_district = $1 LIMIT 1",
                     ["SOUTH WEST KHASI HILLS"])]


def test_empty_one_programme_count_with_a_block_says_zero(monkeypatch):
    _place_lookup(monkeypatch)
    sql = ("SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate WHERE "
           f"LOWER(lgd_block) = 'mawphlang' AND scheme_name = '{CIN}' GROUP BY scheme_name")
    out = run(p._cme_zero_programmes_answer(sql))
    assert out == "There are no applications under the Meghalaya Cinema Theatre Scheme in Mawphlang block — the data records 0."


def test_empty_on_hold_count_says_zero_on_hold(monkeypatch):
    _place_lookup(monkeypatch)
    sql = SWKH_SQL.replace("GROUP BY", "AND data_verified = 'On Hold'\nGROUP BY")
    out = run(p._cme_zero_programmes_answer(sql))
    assert out.startswith("There are no applicants on hold under the Chief Minister's Green Taxi Scheme or the "
                          "Agro Tourism Villa Scheme in South West Khasi Hills — the data records 0 for each.")


def test_place_not_in_the_data_keeps_the_no_records_message(monkeypatch):
    _place_lookup(monkeypatch, found=False)      # e.g. a misspelt district: not a 0
    out = run(p.compose_response("q", SWKH_SQL, [], entities={"district": "South West Khasi Hills"}, schemes=CME))
    assert out.startswith("I couldn't find any matching records")


@pytest.mark.parametrize("sql", [
    SWKH_SQL.replace(AGRO, "Agro Tourism Villas Scheme"),                         # not an exact programme name
    SWKH_SQL.replace("GROUP BY", "AND gender_id = '2'\nGROUP BY"),                 # a filter the wording cannot name
    SWKH_SQL.replace("GROUP BY", "AND current_level = 'level7'\nGROUP BY"),        # not a level in the data
    SWKH_SQL.replace("GROUP BY scheme_name", "GROUP BY scheme_name HAVING COUNT(*) > 5"),
    SWKH_SQL.replace("AND scheme_name", "OR scheme_name"),
    SWKH_SQL.replace("COUNT(DISTINCT request_id)", "AVG(loan_amount)"),
    SWKH_SQL.replace("GROUP BY scheme_name", "GROUP BY lgd_block"),
])
def test_empty_results_that_are_not_a_zero_are_not_answered_as_zero(monkeypatch, sql):
    _place_lookup(monkeypatch)
    assert run(p._cme_zero_programmes_answer(sql)) is None


def test_other_schemes_keep_the_no_records_message(monkeypatch):
    _place_lookup(monkeypatch)
    out = run(p.compose_response("q", SWKH_SQL, [], entities={"district": "South West Khasi Hills"},
                                 schemes=["Focus Plus"]))
    assert out.startswith("I couldn't find any matching records")


def test_both_programmes_present_is_unchanged():
    rows = [{"scheme_name": SEED, "applicants": 544}, {"scheme_name": WAR, "applicants": 2}]
    ans = ("There are 544 applicants under PRIME Small Enterprise Empowerment and Development (SEED) and 2 "
           "applicants under Meghalaya Warehouse Scheme in East Garo Hills.  Combined across these 2 programmes: "
           "546 applicants.")
    assert p._cme_multi_scheme_total("q", _pair_sql(SEED, WAR), rows, ans) == ans


# ── KI-070: "pending at level N" ─────────────────────────────────────────────
def test_pending_at_level_drops_the_on_hold_filter():
    sql = ("SELECT COUNT(*) AS applications\nFROM curated.v_cm_elevate\n"
           "WHERE LOWER(current_level) = 'level2'\n  AND data_verified = 'On Hold'\nLIMIT 1")
    out = p._cm_elevate_level_pending("How many CM ELEVATE applications are pending at level 2?", CME, sql)
    assert "On Hold" not in out and "current_level) = 'level2'" in out
    assert "(LOWER(current_level) = 'level2' AND scheme_specific ->> 'file_status' = 'Pending')" in out  # KI-186


# ── KI-186: pending at level N = level AND file_status 'Pending'; "unique" = distinct ──
# User screenshot 2026-10-05: "how many cm elevate unique applications are pending at level 1
# for all of Meghalaya" -> SELECT COUNT(*) … WHERE LOWER(current_level) = 'level1' -> 8,372.
SHOT_Q = "how many cm elevate unique applications are pending at level 1 for all of Meghalaya"
SHOT_SQL = ("SELECT COUNT(*) AS applications\nFROM curated.v_cm_elevate\n"
            "WHERE LOWER(current_level) = 'level1'\nLIMIT 1")


def test_screenshot_question_gets_pending_state_and_distinct_count():
    out = p._cm_elevate_unique_applications(SHOT_Q, CME, p._cm_elevate_level_pending(SHOT_Q, CME, SHOT_SQL))
    assert out == ("SELECT COUNT(DISTINCT request_id) AS applications\nFROM curated.v_cm_elevate\n"
                   "WHERE (LOWER(current_level) = 'level1' AND scheme_specific ->> 'file_status' = 'Pending')\nLIMIT 1")


def test_pending_state_added_inside_a_count_filter_too():
    sql = ("SELECT lgd_district, COUNT(*) FILTER (WHERE LOWER(current_level) = 'level2') AS pending "
           "FROM curated.v_cm_elevate GROUP BY lgd_district")
    out = p._cm_elevate_level_pending("applications pending at level 2 by district", CME, sql)
    assert "FILTER (WHERE (LOWER(current_level) = 'level2' AND scheme_specific ->> 'file_status' = 'Pending'))" in out


@pytest.mark.parametrize("q,sql", [
    # plain "pending" (no level) stays On Hold — KI-074, unchanged
    ("How many applications are pending in Ri Bhoi?",
     "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE data_verified = 'On Hold' AND lgd_district = 'RI BHOI'"),
    # a level without "pending" is just the level
    ("How many applications are at level 2?",
     "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level2'"),
    # already carries the Pending state
    ("pending at level 1", "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level1' "
                           "AND scheme_specific ->> 'file_status' = 'Pending'"),
    # "on hold at level 1" is asked explicitly
    ("How many applications are on hold pending at level 1?",
     "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level1' AND data_verified = 'On Hold'"),
])
def test_other_pending_and_level_questions_are_unchanged(q, sql):
    assert p._cm_elevate_level_pending(q, CME, sql) == sql


@pytest.mark.parametrize("q", ["How many applications are there in Ri Bhoi?",
                               "How many applicants are there in Ri Bhoi?"])
def test_count_star_kept_without_unique(q):
    sql = "SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_district = 'RI BHOI'"
    assert p._cm_elevate_unique_applications(q, CME, sql) == sql


def test_unique_guard_is_cm_elevate_only():
    sql = "SELECT COUNT(*) FROM curated.v_cm_elevate_disbursement"
    assert p._cm_elevate_unique_applications("unique applications", ["CM Elevate Legacy"], sql) == sql


@pytest.mark.parametrize("bad,good", [
    # live 2026-10-05: verifier rejected all 4 attempts -> "couldn't build a working query"
    ("WHERE LOWER(lgd_district) = 'NORTH GARO HILLS'", "WHERE lgd_district = 'NORTH GARO HILLS'"),
    ("WHERE LOWER(lgd_district) = 'north garo hills'", "WHERE lgd_district = 'NORTH GARO HILLS'"),
    ("WHERE UPPER(v.lgd_block) IN ('Tura')", "WHERE v.lgd_block IN ('TURA')"),
    # live 2026-10-05: current_level = 'level 1' -> a false 0 against 1,624
    ("WHERE current_level = 'level 1'", "WHERE LOWER(current_level) = 'level1'"),
    ("WHERE LOWER(current_level) = 'Level-2'", "WHERE LOWER(current_level) = 'level2'"),
    ("WHERE current_level = 'LEVEL0'", "WHERE LOWER(current_level) = 'level0'"),
])
def test_case_folded_literals_are_made_matchable(bad, good):
    sql = f"SELECT COUNT(*) FROM curated.v_cm_elevate {bad}"
    assert p._uppercase_geo_literals(p._cm_elevate_case_safe_literals(CME, sql)) == \
        f"SELECT COUNT(*) FROM curated.v_cm_elevate {good}"


def test_case_safe_literals_leave_other_schemes_and_canonical_sql_alone():
    sql = "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level1' AND lgd_district = 'RI BHOI'"
    assert p._cm_elevate_case_safe_literals(CME, sql) == sql
    other = "SELECT 1 FROM curated.v_x WHERE LOWER(lgd_district) = 'RI BHOI'"
    assert p._cm_elevate_case_safe_literals(["Focus Plus"], other) == other


NGH_SQL = ("SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level1' "
           "AND scheme_specific ->> 'file_status' = 'Pending' AND lgd_district = 'NORTH GARO HILLS' LIMIT 1")
NGH_ISSUE = ("Check 2: The RESOLVED ENTITIES block specifies lgd_district = 'NORTH GARO HILLS', but the SQL uses "
             "LOWER(lgd_district) = 'NORTH GARO HILLS'. The check requires the exact value from the RESOLVED ENTITIES "
             "block to be present verbatim in the WHERE clause.")


def test_verifier_check2_on_a_verbatim_place_is_discarded():
    # live 2026-10-05: rejected 4 times -> "couldn't build a working query"
    assert p._verifier_scheme_specific_complaint_is_false(NGH_ISSUE, CME, NGH_SQL, {"district": "NORTH GARO HILLS"})


@pytest.mark.parametrize("sql,resolved", [
    (NGH_SQL.replace("lgd_district = ", "LOWER(lgd_district) = "), {"district": "NORTH GARO HILLS"}),  # really folded
    (NGH_SQL.replace("NORTH GARO HILLS", "WEST GARO HILLS"), {"district": "NORTH GARO HILLS"}),        # wrong place
    (NGH_SQL, {"district": "NORTH GARO HILLS", "cm_scheme": "Meghalaya Piggery Development Scheme"}),  # not only places
])
def test_verifier_check2_with_a_real_place_problem_is_kept(sql, resolved):
    assert not p._verifier_scheme_specific_complaint_is_false(NGH_ISSUE, CME, sql, resolved)


TOP_Q = "Which CM ELEVATE programs have the highest number of pending applications?"


@pytest.mark.parametrize("where,expected", [
    # live 2026-10-05, 4 of 6 runs: "Tourism Vehicle 89" against the true On Hold leader
    ("WHERE LOWER(current_file_status) LIKE 'sendback%' OR current_file_status IS NULL GROUP BY scheme_name",
     "WHERE data_verified = 'On Hold' GROUP BY scheme_name"),
    ("WHERE lgd_district = 'RI BHOI' AND LOWER(current_file_status) = 'forward' GROUP BY scheme_name",
     "WHERE lgd_district = 'RI BHOI' AND data_verified = 'On Hold' GROUP BY scheme_name"),
])
def test_plain_pending_on_file_status_is_restored_to_on_hold(where, expected):
    sql = f"SELECT scheme_name, COUNT(*) AS pending FROM curated.v_cm_elevate {where} ORDER BY pending DESC LIMIT 10"
    assert p._cm_elevate_plain_pending_file_status(TOP_Q, CME, sql) == \
        f"SELECT scheme_name, COUNT(*) AS pending FROM curated.v_cm_elevate {expected} ORDER BY pending DESC LIMIT 10"


@pytest.mark.parametrize("q", ["How many applications are pending at level 1?",          # level: KI-186 rule
                               "How many applications are sent back and pending?",       # names a file status
                               "What is the pending status distribution?",              # a status question
                               "How many applications are pending or on hold?"])
def test_file_status_kept_when_the_question_points_at_it(q):
    sql = "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_file_status) LIKE 'sendback%'"
    assert p._cm_elevate_plain_pending_file_status(q, CME, sql) == sql


def test_file_status_mixed_with_another_column_is_left_alone():
    sql = ("SELECT COUNT(*) FROM curated.v_cm_elevate WHERE (current_file_status = 'forward' OR "
           "lgd_district = 'RI BHOI')")
    assert p._cm_elevate_plain_pending_file_status(TOP_Q, CME, sql) == sql


def test_level_pending_zero_answer_names_the_level():
    sql = (f"SELECT scheme_name, COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_district = 'RI BHOI' "
           f"AND (LOWER(current_level) = 'level1' AND scheme_specific ->> 'file_status' = 'Pending') "
           f"AND scheme_name IN ('{CIN}', '{WAR}') GROUP BY scheme_name")
    out = p._cme_multi_scheme_total("q", sql, [{"scheme_name": WAR, "applications": 4}], "x")
    assert out.startswith("There are 4 applications pending at level 1 in Ri Bhoi across the 2 programmes")


def test_explicit_on_hold_at_a_level_is_kept():
    sql = "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level1' AND data_verified = 'On Hold'"
    assert p._cm_elevate_level_pending("How many are on hold at level 1?", CME, sql) == sql


def test_plain_pending_filtered_on_a_level_is_restored_to_on_hold():
    # the 019a re-test regression: no level named, SQL filtered level1 (SEED "3,633 pending")
    sql = ("SELECT scheme_name, COUNT(*) AS pending_applications FROM curated.v_cm_elevate "
           "WHERE LOWER(current_level) = 'level1' GROUP BY scheme_name ORDER BY pending_applications DESC LIMIT 10")
    out = p._cm_elevate_pending_without_level(
        "Which CM ELEVATE programs have the highest number of pending applications?", CME, sql)
    assert "WHERE data_verified = 'On Hold' GROUP BY scheme_name" in out and "current_level" not in out


@pytest.mark.parametrize("q", ["How many applications are pending at level 2?",
                               "Show the level-wise breakdown of pending applications"])
def test_a_level_named_in_the_question_keeps_the_level_filter(q):
    sql = "SELECT COUNT(*) FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level2'"
    assert p._cm_elevate_pending_without_level(q, CME, sql) == sql


def test_genuine_zero_is_stated_not_hedged():
    out = run(p._cm_elevate_answer_guarantees(
        "How many applications are pending at level 2?", "SELECT COUNT(*) AS applications FROM x",
        [{"applications": 0}], "The data available doesn't cover pending applications at level 2.", {}))
    assert out.startswith("0 applications") and "cover" not in out


# ── KI-071: comparison differences ───────────────────────────────────────────
def test_programme_by_district_comparison_states_the_differences():
    rows = [{"scheme_name": PIG, "lgd_district": "RI BHOI", "applications": 1944},
            {"scheme_name": PIG, "lgd_district": "WEST KHASI HILLS", "applications": 229},
            {"scheme_name": POU, "lgd_district": "RI BHOI", "applications": 242},
            {"scheme_name": POU, "lgd_district": "WEST KHASI HILLS", "applications": 84}]
    ans = ("Piggery recorded 1,944 applications in Ri Bhoi and 229 in West Khasi Hills. "
           "Poultry recorded 242 in Ri Bhoi and 84 in West Khasi Hills.")
    out = p._cme_comparison("Compare applicant counts for Piggery and Poultry across Ri Bhoi and "
                            "West Khasi Hills.", rows, ans)
    assert "Piggery Development: Ri Bhoi is higher by 1,715 (1,944 vs 229)" in out
    assert "Poultry Farming: Ri Bhoi is higher by 158" in out
    assert "West Khasi Hills: Piggery Development is higher by 145" in out


def test_no_comparison_cue_no_change():
    rows = [{"scheme_name": PIG, "lgd_district": "RI BHOI", "n": 1}, {"scheme_name": PIG, "lgd_district": "X", "n": 2}]
    assert p._cme_comparison("How many in each district?", rows, "a") == "a"


# ── KI-072: zero-count sectors survive a status filter ───────────────────────
def test_sector_status_filter_moves_into_count_filter():
    sql = ("SELECT COALESCE(scheme_specific ->> 'sector_id', '(not recorded)') AS sector,\n"
           "       COUNT(*) AS applications\nFROM curated.v_cm_elevate\n"
           "WHERE lgd_district = 'WEST KHASI HILLS'\n  AND data_verified = 'On Hold'\n"
           "GROUP BY 1\nORDER BY applications DESC\nLIMIT 100")
    out = p._cm_elevate_sector_zero_groups(CME, sql)
    assert "COUNT(*) FILTER (WHERE data_verified = 'On Hold') AS applications" in out
    assert "WHERE lgd_district = 'WEST KHASI HILLS'\nGROUP BY 1" in out


def test_sector_guard_ignores_queries_already_using_filter():
    sql = ("SELECT scheme_name, COALESCE(scheme_specific ->> 'sector_id', '(not recorded)') AS sector, "
           "COUNT(*) AS n, COUNT(*) FILTER (WHERE data_verified = 'On Hold') AS pending "
           "FROM curated.v_cm_elevate WHERE lgd_district = 'RI BHOI' GROUP BY scheme_name, sector")
    assert p._cm_elevate_sector_zero_groups(CME, sql) == sql


# ── KI-073: two-label answers are readable ───────────────────────────────────
def test_garbled_scheme_by_sector_answer_is_rebuilt():
    rows = [{"scheme_name": DAI, "sector": "(not recorded)", "applicant_count": 11, "pending_applications": 1},
            {"scheme_name": DAI, "sector": "Dairy", "applicant_count": 4, "pending_applications": 0},
            {"scheme_name": PIG, "sector": "(not recorded)", "applicant_count": 229, "pending_applications": 14},
            {"scheme_name": POU, "sector": "(not recorded)", "applicant_count": 72, "pending_applications": 5},
            {"scheme_name": POU, "sector": "Poultry", "applicant_count": 9, "pending_applications": 0},
            {"scheme_name": POU, "sector": "Piggery", "applicant_count": 3, "pending_applications": 0}]
    ans = ("The applicant counts are 11 and 4 for Dairy, 229 for Piggery (not recorded), 72 for Poultry "
           "(not recorded), and 9 and 3 for Poultry and Piggery respectively. The pending application "
           "statuses are 1, 0, 14, 5, 0, and 0 for each corresponding row.")
    out = p._cme_grouped_rows(ans, rows)
    assert "respectively" not in out
    assert "- Poultry Farming — total applicant count 84, pending applications 5." in out
    assert "Piggery: applicant count 3, pending applications 0" in out
    assert "- Dairy Development — total applicant count 15" in out


def test_readable_two_label_answer_is_kept():
    rows = [{"scheme_name": SEED, "sector": "(not recorded)", "applicant_count": 216, "pending_applications": 17},
            {"scheme_name": PIG, "sector": "(not recorded)", "applicant_count": 1944, "pending_applications": 400}]
    ans = ("PRIME SEED shows 216 applicants with 17 pending applications, while Meghalaya Piggery "
           "Development Scheme has 1,944 applicants and 400 pending applications.")
    assert p._cme_grouped_rows(ans, rows) == ans


# ── KI-074: "pending" = verification On Hold only (product decision 2026-09-28) ─
def test_pending_answer_has_no_file_status_second_reading(monkeypatch):
    async def _ro(sql):
        raise AssertionError("no re-query: the file_status reading was dropped")
    monkeypatch.setattr(p, "run_readonly", _ro)
    sql = ("SELECT COUNT(*) AS pending_applications FROM curated.v_cm_elevate WHERE scheme_name = "
           f"'{PIG}' AND lgd_district = 'RI BHOI' AND data_verified = 'On Hold'")
    out = run(p._cm_elevate_answer_guarantees("How many applications are pending under Piggery in Ri Bhoi?",
                                              sql, [{"pending_applications": 400}], "There are 400 pending.", {}))
    assert out == "There are 400 pending."
    assert not hasattr(p, "_cme_pending_file_status")


# ── KI-075: PRIME SEED names CM Elevate ──────────────────────────────────────
@pytest.mark.parametrize("q", ["How many applicants are there under PRIME SEED in West Garo Hills?",
                               "What is the status distribution for PRIME SEED?",
                               "applicants under the SEED scheme in Rongram block"])
def test_prime_seed_is_cm_elevate(q):
    assert p._infer_scheme_from_terms(q) == ["CM Elevate"]


def test_seed_money_is_not_cm_elevate():
    assert p._infer_scheme_from_terms("How much seed money went to producer groups?") != ["CM Elevate"]


# ── KI-076: an applicant noun scoped to a place is a DATA question ────────────
@pytest.mark.parametrize("q,want", [
    ("Applicants under Goat Farming and Warehouse in West Khasi Hills?", "DATA"),
    ("applicants under piggery in umling block?", "DATA"),
    ("CM Elevate applicants in Umshaken village", "DATA"),
    ("What documents do applicants in Ri Bhoi need?", "KNOWLEDGE"),
    ("Who is eligible to submit applications in Meghalaya?", "KNOWLEDGE"),
    ("What happens to applications in the verification stage?", "KNOWLEDGE"),
])
def test_applicant_place_cue_routes_counts_to_data(monkeypatch, q, want):
    async def _classifier(*a, **k):
        return '{"intent": "KNOWLEDGE"}'   # the flaky classifier answer the cue must override
    monkeypatch.setattr(p.llm, "call_classifier", _classifier)
    assert run(p.classify_intent(q)) == want


# ── KI-106: CM Elevate villages use the shared village guards ─────────────────
def test_cm_elevate_is_a_village_grained_scheme():
    assert p._village_scheme(["CM Elevate"]) == "CM Elevate"
    assert "CM Elevate" in p._VILLAGE_NARROW_SCHEMES


@pytest.mark.parametrize("sql,want", [
    # the block alone for a village (RONGAP SONGGITAL answered with Songsak's 47)
    ("SELECT COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate WHERE lgd_block = 'SONGSAK' LIMIT 1",
     "WHERE village_code = 275321 LIMIT 1"),
    # the village name in the district slot
    ("SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_district = 'MAWDEM DOMPHLANG' "
     "AND lgd_block = 'JIRANG' AND data_verified = 'On Hold' LIMIT 1",
     "WHERE village_code = 275321 AND data_verified = 'On Hold' LIMIT 1"),
    # a programme literal containing " and " survives the rebuild
    ("SELECT COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate WHERE lgd_block = 'SIJU' "
     f"AND scheme_name = '{SEED}' LIMIT 1",
     f"WHERE village_code = 275321 AND scheme_name = '{SEED}' LIMIT 1"),
])
def test_single_village_where_is_pinned(sql, want):
    out = p._focusplus_pin_village_where(CME, {"resolved": {"village_code": 275321}}, sql)
    assert out.endswith(want), out


def test_pin_keeps_count_filters_in_the_select_list():
    sql = ("SELECT COUNT(*) FILTER (WHERE data_verified = 'Valid') AS verified, "
           "COUNT(*) FILTER (WHERE data_verified IS DISTINCT FROM 'Valid') AS not_verified "
           "FROM curated.v_cm_elevate WHERE lgd_block = 'SAMANDA'")
    out = p._focusplus_pin_village_where(CME, {"resolved": {"village_code": 275365}}, sql)
    assert out.startswith("SELECT COUNT(*) FILTER (WHERE data_verified = 'Valid') AS verified, "
                          "COUNT(*) FILTER (WHERE data_verified IS DISTINCT FROM 'Valid') AS not_verified")
    assert out.endswith("WHERE village_code = 275365")


def test_top_level_where_ignores_where_inside_parentheses():
    sql = "SELECT COUNT(*) FILTER (WHERE a = 1) AS x FROM t GROUP BY y"
    assert p._top_level_where_span(sql) is None


# ── KI-107: blocks mapped to the spelling the data stores ─────────────────────
_BLOCKS = ["BAGHMARA", "BAGHMARA-MUNICIPAL BOARD", "JOWAI-MUNICIPAL BOARD", "RI MULIANG", "UMLING",
           "WILLIAM NAGAR-MUNICIPAL BOARD", "TURA MUNICIPAL BOARD-MUNICIPAL BOARD", "MAIRANG",
           "MAIRANG-TOWN COMMITTEE"]


@pytest.mark.parametrize("canon,q,want", [
    ("BAGHMARA", "How many applicants in Baghmara Municipal Board?", "BAGHMARA-MUNICIPAL BOARD"),
    ("BAGHMARA", "How many applicants in baghmara mb?", "BAGHMARA-MUNICIPAL BOARD"),
    ("BAGHMARA", "How many applicants in Baghmara block?", None),          # the rural block stays
    ("JOWAI", "How many applicants in Jowai block?", "JOWAI-MUNICIPAL BOARD"),  # no rural Jowai rows
    ("RI-MULIANG", "How many applicants in Ri Muliang block?", "RI MULIANG"),
    ("WILLIAMNAGAR", "Williamnagar applicants", "WILLIAM NAGAR-MUNICIPAL BOARD"),
    ("TURA", "Tura Municipal Board applicants", "TURA MUNICIPAL BOARD-MUNICIPAL BOARD"),
    ("MAIRANG", "Mairang Town Committee applicants", "MAIRANG-TOWN COMMITTEE"),
    ("MAIRANG", "Mairang block applicants", None),
    ("KHLIEHRIAT", "Khliehriat block applicants", None),                    # absent: left alone
])
def test_block_mapped_to_the_data_spelling(canon, q, want):
    assert p._cme_data_block(canon, q, _BLOCKS) == want


# ── one-figure rule and the Garo-range check now include CM Elevate ───────────
def test_one_figure_rule_covers_cm_elevate():
    assert ["CM Elevate"] in p._ONE_FIGURE_SCHEMES


def test_composer_one_for_twelve_is_retried(monkeypatch):
    calls = []

    async def _composer(prompt, *a, **k):
        calls.append(prompt)
        return "There is 1 pending CM ELEVATE application in Ranikor block." if len(calls) == 1 \
            else "There are 12 pending CM ELEVATE applications in Ranikor block."
    monkeypatch.setattr(p.llm, "call_response_composer", _composer)
    out = run(p.compose_response("How many CM ELEVATE applications are pending in Ranikor block?",
                                 "SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE lgd_block = 'RANIKOR'",
                                 [{"applications": 12}], schemes=CME))
    assert "12" in out and len(calls) == 2


# ── KI-107: urban bodies / mention-level block lookup ─────────────────────────
@pytest.mark.parametrize("mention,want", [
    ("Jowai-municipal Board", "JOWAI-MUNICIPAL BOARD"),
    ("Nongpoh TC", "NONGPOH-TOWN COMMITTEE"),
    ("Mawhati", "MAWHATI"),
    ("Guwahati", None),
])
def test_block_from_mention(monkeypatch, mention, want):
    monkeypatch.setattr(p, "_CME_BLOCKS", ["JOWAI-MUNICIPAL BOARD", "NONGPOH-TOWN COMMITTEE", "MAWHATI", "UMLING"])
    assert run(p._cm_elevate_block_from_mention(mention, CME)) == want
    assert run(p._cm_elevate_block_from_mention(mention, ["Focus Plus"])) is None


# ── KI-111: a sector question naming no programme covers every programme ─────
def test_sector_question_drops_the_invented_programme_filter():
    sql = ("SELECT scheme_name,\n       COALESCE(scheme_specific ->> 'sector_id', '(not recorded)') AS sector,\n"
           "       COUNT(*) AS applicants\nFROM curated.v_cm_elevate\nWHERE lgd_district = 'WEST KHASI HILLS'\n"
           f"  AND scheme_name IN ('{POU}', '{DAI}')\nGROUP BY scheme_name, sector\n"
           "ORDER BY scheme_name, applicants DESC\nLIMIT 100")
    out = p._cm_elevate_sector_all_programmes(
        "How many CM ELEVATE applicants are associated with each sector in West Khasi Hills?", CME, sql)
    assert "scheme_name" not in out and "GROUP BY sector" in out and "lgd_district = 'WEST KHASI HILLS'" in out


def test_sector_question_naming_a_programme_keeps_it():
    sql = (f"SELECT COALESCE(scheme_specific ->> 'sector_id', '(not recorded)') AS sector, COUNT(*) AS n "
           f"FROM curated.v_cm_elevate WHERE scheme_name = '{POU}' AND lgd_block = 'KHARKUTTA' GROUP BY 1")
    assert p._cm_elevate_sector_all_programmes("Poultry applicants in each sector in Kharkutta", CME, sql) == sql


# ── KI-112: mistyped literals snapped to stored names ─────────────────────────
@pytest.mark.parametrize("sql,want", [
    ("SELECT 1 FROM curated.v_cm_elevate WHERE scheme_name IN ('PRIME Tourism Vehicle', "
     f"'{SEED}')", "'PRIME Tourism Vehicle Scheme'"),
    ("SELECT 1 FROM curated.v_cm_elevate WHERE lgd_block = 'SHILLONG-MUNICIPAL_BOARD'", "'SHILLONG-MUNICIPAL BOARD'"),
    ("SELECT 1 FROM curated.v_cm_elevate WHERE village_code = 1 AND lgd_district = 'WEST JAINTEIA HILLS'",
     "'WEST JAINTIA HILLS'"),
    ("SELECT 1 FROM curated.v_cm_elevate WHERE lgd_district = 'RI-BHOI'", "'RI BHOI'"),
])
def test_mistyped_literals_are_snapped(monkeypatch, sql, want):
    monkeypatch.setattr(p, "_CME_BLOCKS", ["SHILLONG-MUNICIPAL BOARD", "MAWPAT"])
    assert want in run(p._cm_elevate_fix_literals(CME, sql))


def test_ambiguous_or_correct_literals_are_left(monkeypatch):
    monkeypatch.setattr(p, "_CME_BLOCKS", ["MAWPAT"])
    sql = f"SELECT 1 FROM curated.v_cm_elevate WHERE lgd_district = 'NORTH GARO' AND scheme_name = '{PIG}'"
    assert run(p._cm_elevate_fix_literals(CME, sql)) == sql


def test_verifier_check2_on_file_status_with_resolved_block_is_discarded():
    issue = ("Check 2: The RESOLVED ENTITIES block explicitly lists 'lgd_block = LASKEIN', but the SQL uses a "
             "JSON path extraction ('scheme_specific ->> file_status') instead of a direct string comparison")
    sql = ("SELECT COUNT(*) AS rejected FROM curated.v_cm_elevate WHERE lgd_block = 'LASKEIN' "
           "AND scheme_specific ->> 'file_status' = 'Rejected'")
    assert p._verifier_scheme_specific_complaint_is_false(issue, CME, sql, {"block": "LASKEIN"})
    assert not p._verifier_scheme_specific_complaint_is_false(issue, CME, sql, {"block": "UMLING"})


def test_village_beside_an_urban_block_is_the_village(monkeypatch):
    # "Umrangksai, Nongpoh-town Committee block" was answered for the whole urban body
    monkeypatch.setattr(p, "_CME_VILLAGE_NAMES", {
        "UMRANGKSAI": [{"village_code": 1298472, "name": "Umrangksai", "block": "NONGPOH-TOWN COMMITTEE",
                        "district": "RI BHOI"}]})
    got = run(p._pmay_village_beside_block(
        "What is the CM ELEVATE application status distribution in Umrangksai, Nongpoh-town Committee block?",
        {"block": "NONGPOH-TOWN COMMITTEE", "district": None}, "CM Elevate"))
    assert got and got["village_code"] == 1298472


def test_chip_pin_prefers_the_twin_the_scheme_holds(monkeypatch):
    async def fetch(sql, params=None):
        return [{"village_code": 272918, "lgd_village_name": "Bolbokgre", "lgd_block": "DEMDEMA",
                 "lgd_district": "WEST GARO HILLS"},
                {"village_code": 273107, "lgd_village_name": "Bolbokgre", "lgd_block": "DEMDEMA",
                 "lgd_district": "WEST GARO HILLS"}]
    monkeypatch.setattr(p, "fetch_rows", fetch)
    monkeypatch.setattr(p, "_CME_VILLAGE_NAMES", {"BOLBOKGRE": [
        {"village_code": 273107, "name": "Bolbokgre", "block": "DEMDEMA", "district": "WEST GARO HILLS"}]})
    q = "How many applicants are there under the PRIME SEED scheme in Bolbokgre village, DEMDEMA block, WEST GARO HILLS"
    assert run(p._mgnrega_village_chip_pin(q, "CM Elevate"))["village_code"] == 273107


# ── spelled-out figures become digits before the faithfulness checks ─────────
def test_number_word_becomes_digits_when_it_is_a_result_value():
    rows = [{"verified": 5, "not_verified": 3, "total_applications": 8}]
    out = p._cme_digits_for_number_words(
        "Five CM ELEVATE applicants in Sasatgre village have completed data verification, with 3 not verified.", rows)
    assert out.startswith("5 CM ELEVATE applicants")


def test_wrong_number_word_is_left_for_the_checks():
    out = p._cme_digits_for_number_words("Five applicants are pending.", [{"applications": 12}])
    assert out == "Five applicants are pending."


def test_composer_wrong_number_word_is_retried(monkeypatch):
    calls = []

    async def _composer(prompt, *a, **k):
        calls.append(prompt)
        return "Five applications are pending in Ranikor block." if len(calls) == 1 \
            else "There are 12 pending applications in Ranikor block."
    monkeypatch.setattr(p.llm, "call_response_composer", _composer)
    out = run(p.compose_response("How many CM ELEVATE applications are pending in Ranikor block?",
                                 "SELECT COUNT(*) AS applications FROM curated.v_cm_elevate", [{"applications": 12}],
                                 schemes=CME))
    assert "12" in out and len(calls) == 2


# ── KI-116: a programme filter the question never named is dropped ────────────
_EACH_SQL = ("SELECT scheme_name, COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate "
             f"WHERE village_code = 274564 AND scheme_name IN ('{PIG}', '{POU}') GROUP BY scheme_name")


def test_each_program_question_drops_the_invented_list():
    out = p._cm_elevate_unasked_programme_filter(
        "How many applicants are there under each CM ELEVATE program in MIKKA SIMDAM, Resubelpara block?", CME, _EACH_SQL)
    assert "scheme_name IN" not in out and "WHERE village_code = 274564 GROUP BY scheme_name" in out


@pytest.mark.parametrize("q", ["How many applicants are there in Umling block under Piggery and Poultry?",
                               "How many applications are under the livestock schemes in Umling block?"])
def test_named_programmes_or_a_family_keep_the_filter(q):
    assert p._cm_elevate_unasked_programme_filter(q, CME, _EACH_SQL) == _EACH_SQL


def test_verifier_check2_on_exact_programme_names_is_discarded():
    issue = ("Check 2: The RESOLVED ENTITIES block specifies 'PRIME Tourism Vehicle Scheme' (without the word "
             "'Scheme' at the end). However, the SQL filters on 'PRIME Tourism Vehicle Scheme'")
    sql = (f"SELECT scheme_name, COUNT(DISTINCT request_id) AS a FROM curated.v_cm_elevate WHERE scheme_name IN "
           f"('PRIME Tourism Vehicle Scheme', '{SEED}') AND lgd_block = 'MAWPAT' GROUP BY scheme_name")
    assert p._verifier_scheme_specific_complaint_is_false(issue, CME, sql, {"block": "MAWPAT"})
    assert not p._verifier_scheme_specific_complaint_is_false(issue, CME, sql.replace("'PRIME Tourism Vehicle Scheme'",
                                                                                      "'PRIME Tourism Vehicle'"),
                                                              {"block": "MAWPAT"})


# ── KI-119 / KI-120 / KI-118 (final pass) ─────────────────────────────────────
def test_invented_level_filter_is_dropped():
    sql = ("SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE village_code = 277650 "
           "AND current_level = 'quinary' AND data_verified = 'On Hold' LIMIT 1")
    out = p._cm_elevate_unasked_level_filter(
        "How many CM ELEVATE applications are pending in QUININE NONGLADEW, Umling block?", CME, sql)
    assert "current_level" not in out and "village_code = 277650 AND data_verified = 'On Hold'" in out
    assert p._cm_elevate_unasked_level_filter("How many applications are at level 2?", CME,
                                              sql.replace("'quinary'", "'level2'")) == sql.replace("'quinary'", "'level2'")


def test_cm_elevate_is_allowed_the_village_or_outside_place_check():
    import inspect
    assert '["CM Elevate"]' in inspect.getsource(p._mgnrega_village_not_out_of_area)


def test_plain_pending_with_a_two_digit_level_becomes_on_hold():
    sql = ("SELECT COUNT(*) AS applications FROM curated.v_cm_elevate WHERE LOWER(current_level) = 'level12' "
           "AND lgd_block = 'KHATARSHNONG LAITKROH' LIMIT 1")
    q = "How many CM ELEVATE applications are pending in 12TH MER, Khatarshnong Laitkroh block?"
    out = p._cm_elevate_unasked_level_filter(q, CME, p._cm_elevate_pending_without_level(q, CME, sql))
    assert "data_verified = 'On Hold'" in out and "current_level" not in out


def test_verifier_grain_complaint_on_the_view_is_discarded():
    issue = ("Check 3: Table/grain — The question asks for a total count ('How many...'), but the SQL uses "
             "COUNT(DISTINCT ...) on a view (curated.v_cm_elevate) which is a denormalized, wide table containing "
             "multiple rows per applicant (one per scheme/year). The SQL should aggregate the raw fact table")
    sql = "SELECT COUNT(DISTINCT request_id) AS applicants FROM curated.v_cm_elevate WHERE village_code = 277617"
    assert p._verifier_scheme_specific_complaint_is_false(issue, CME, sql, {"village_code": 277617})
    assert not p._verifier_scheme_specific_complaint_is_false(issue, ["Focus Plus"], sql, {})


def test_sector_answer_that_names_the_sector_as_a_programme_is_rebuilt():
    sql = (f"SELECT COUNT(*) AS applicants FROM curated.v_cm_elevate WHERE scheme_name = '{POU}' "
           "AND scheme_specific ->> 'sector_id' ILIKE 'piggery' LIMIT 1")
    ans = ("The Meghalaya Piggery Development Scheme had 5 applicants under the Meghalaya Poultry Farming Scheme. "
           "This figure applies to the entire state of Meghalaya.")
    out = p._cme_sector_answer_names_wrong_programme(ans, sql, [{"applicants": 5}])
    assert out == f"5 applicants under the {POU} have their sector recorded as Piggery."
    good = f"5 applicants under the {POU} belong to the Piggery sector."
    assert p._cme_sector_answer_names_wrong_programme(good, sql, [{"applicants": 5}]) == good


# ── 2026-09-28 product decision: CM Elevate "status" = current_file_status ────
@pytest.mark.parametrize("q", ["What is the current application status distribution across CM ELEVATE?",
                               "What is the status-wise applicant count for the Piggery scheme in Umshaken village?",
                               "What is the application status distribution in RI BHOI?"])
def test_status_groups_by_current_file_status(q):
    sql = ("SELECT COALESCE(data_verified, '(not recorded)') AS status, COUNT(*) AS applications "
           "FROM curated.v_cm_elevate GROUP BY 1 ORDER BY applications DESC")
    out = p._cm_elevate_status_is_file_status(q, CME, sql)
    assert "current_file_status" in out and "data_verified" not in out and "'Sent back'" in out


@pytest.mark.parametrize("q", ["What is the verification status breakdown?",
                               "How many applications are pending in each sector in RI BHOI?",
                               "How many applications under Piggery are currently On Hold in Ri Bhoi?"])
def test_verification_and_pending_keep_data_verified(q):
    sql = "SELECT data_verified, COUNT(*) FROM curated.v_cm_elevate GROUP BY 1"
    assert p._cm_elevate_status_is_file_status(q, CME, sql) == sql


def test_status_rule_and_vocab_say_current_file_status():
    from app import schema_context as sc
    src = open(sc.__file__, encoding="utf-8").read()
    assert "ALWAYS means current_file_status" in src and '"status" (default) -> data_verified' not in src
