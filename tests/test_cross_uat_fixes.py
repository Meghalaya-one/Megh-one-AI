"""
Cross-scheme UAT fixes (Megh_One_AI_Cross_UAT_Test_Report.xlsx, testers' run 2026-10-15;
analysis docs/Cross_UAT_Failure_Analysis_2026-10-16.md, KI-233 … KI-242).

Each test calls the real production function:

  _excluded_schemes / _named_schemes    "excluding X" / "leaving out X" (KI-234)
  _pin_bare_focus                       "the FOCUS scheme", "FOCUS on …" (KI-235)
  _cross_scheme_compare_plan            cues: doing, bigger, rank, which of, top N,
                                        second after X, A more than B, combined, one FY
  _cross_scheme_compare_answer          PMAY-G every record (KI-233), combined totals and
                                        district ranking (KI-236), answer shapes (KI-237),
                                        a named FY (KI-238)
  _CROSS_SCHEME_MONEY_SQL / _cross_scheme_money_answer   base views, default schemes (KI-239)
  _cross_scheme_sql_issue               Focus Legacy groups are not beneficiaries (KI-238)

No model or DB: fetch_rows is faked with the live figures of 2026-10-16.
    python -m pytest tests/test_cross_uat_fixes.py -q
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402
from app import schema_context  # noqa: E402

DEFAULT4 = ["MGNREGA", "PMAY-G", "Focus Plus", "Focus Legacy"]


def pinned(q: str) -> str:
    return p._pin_bare_focus(p._pin_cm_elevate_dataset(p._correct_scheme_spelling(q)))


def plan_of(q: str) -> dict:
    return p._cross_scheme_compare_plan(pinned(q))


# ── Live per-district figures (megh_db 2026-10-16) for four districts ────────
# (count value, extra) per scheme; Focus Legacy extra = producer groups.
_D = {
    "EAST KHASI HILLS": {"MGNREGA": 224663, "PMAY-G": 23952, "Focus Plus": 13608, "Focus Legacy": (10841, 909)},
    "RI BHOI": {"MGNREGA": 112602, "PMAY-G": 17565, "Focus Plus": 419, "Focus Legacy": (9858, 955)},
    "SOUTH WEST GARO HILLS": {"MGNREGA": 164976, "PMAY-G": 11947, "Focus Plus": 15294, "Focus Legacy": (6558, 794)},
    "WEST GARO HILLS": {"MGNREGA": 261666, "PMAY-G": 33147, "Focus Plus": 35039, "Focus Legacy": (21867, 2701)},
}
_STATE = {"MGNREGA": 1494437, "PMAY-G": 171107, "Focus Plus": 105813, "Focus Legacy": (102021, 11906)}
_MONEY = {"MGNREGA": 3628.67, "PMAY-G": 2185.26, "Focus Plus": 119.74, "Focus Legacy": 51.01}
_VIEW = {"v_employment": "MGNREGA", "v_expenditure": "MGNREGA", "v_pmay": "PMAY-G",
         "v_focus_plus": "Focus Plus", "v_focus_legacy": "Focus Legacy"}
# FY 2025-26 (year_key 2025)
_FY2025 = {"MGNREGA": 365556, "PMAY-G": 0, "Focus Plus": 105813, "Focus Legacy": (37018, 8806)}


class _Fetch:
    def __init__(self):
        self.calls = []

    async def __call__(self, sql, params=None):
        self.calls.append((sql, params))
        scheme = _VIEW[re.search(r"FROM curated\.(\w+)", sql).group(1)]
        money = "/ 100.0" in sql or "/ 1e7" in sql
        year = "year_key = $" in sql

        def val(src):
            v = src[scheme]
            return v if isinstance(v, tuple) else (v, None)

        def row(v, x):
            r = {"value": v}
            if " AS extra" in sql:
                r["extra"] = x
            return r
        if "GROUP BY lgd_district" in sql:
            return [{"district": d, **row(*(((_MONEY[scheme] / 10, None)) if money else val(f)))}
                    for d, f in _D.items()]
        if money:
            return [row(_MONEY[scheme], None)]
        return [row(*val(_FY2025 if year else _STATE))]


def answer(q, monkeypatch, resolved=None):
    rec = _Fetch()
    monkeypatch.setattr(p, "fetch_rows", rec)
    plan = plan_of(q)
    assert plan is not None, q
    er = {"resolved": resolved or {}, "display": {}, "notes": []}
    out = asyncio.run(p._cross_scheme_compare_answer(pinned(q), plan, er))
    return out, rec.calls


# ── KI-234: exclusions ───────────────────────────────────────────────────────
@pytest.mark.parametrize("q,excluded", [
    ("Leaving out MGNREGA and CM-ELEVATE, which scheme has the most beneficiaries in each district?",
     {"MGNREGA", "CM Elevate", "CM Elevate Legacy"}),
    ("Excluding MGNREGA, which districts have the most beneficiaries across PMAY-G, Focus+ and FOCUS?",
     {"MGNREGA"}),
    ("What is the scheme-wise spending in South Garo Hills, excluding CM-ELEVATE?",
     {"CM Elevate", "CM Elevate Legacy"}),
    ("total MGNREGA expenditure excluding West Garo Hills", set()),
])
def test_excluded_schemes(q, excluded):
    assert p._excluded_schemes(pinned(q)) == excluded


def test_an_excluded_scheme_is_not_named():
    q = "Excluding MGNREGA, which districts have the most beneficiaries across PMAY-G, Focus+ and FOCUS?"
    assert p._named_schemes(pinned(q)) == ["PMAY-G", "Focus Plus", "Focus Legacy"]
    # a plain question is unchanged
    assert p._named_schemes("Compare MGNREGA and PMAY-G spending") == ["MGNREGA", "PMAY-G"]


@pytest.mark.parametrize("q,schemes", [
    ("Leaving out MGNREGA and CM-ELEVATE, which scheme has the most beneficiaries in each district?",
     ["PMAY-G", "Focus Plus", "Focus Legacy"]),                                                # T07.2
    ("What is the scheme-wise spending in South Garo Hills, excluding CM-ELEVATE?", DEFAULT4),   # T11.2
    ("Leaving out CM-ELEVATE, which scheme has spent the most money in Meghalaya so far?", DEFAULT4),  # T12.2
    ("Excluding MGNREGA, which districts have the most beneficiaries across PMAY-G, Focus+ and FOCUS?",
     ["PMAY-G", "Focus Plus", "Focus Legacy"]),                                                # T15.3
])
def test_exclusions_shape_the_comparison(q, schemes):
    assert plan_of(q)["schemes"] == schemes


def test_t11_2_is_a_data_question():
    q = pinned("What is the scheme-wise spending in South Garo Hills, excluding CM-ELEVATE?")
    assert asyncio.run(p.classify_intent(q)) == "DATA"


# ── KI-235: FOCUS listed beside another scheme ──────────────────────────────
@pytest.mark.parametrize("q", [
    "Give me scheme-wise beneficiary numbers for NREGA, PMAY Gramin, Focus Plus and the FOCUS scheme.",
    "Rank MGNREGA, PMAY-G, Focus+ and FOCUS on coverage, beneficiaries and money spent.",
])
def test_focus_beside_another_scheme_is_focus_legacy(q):
    assert "Focus Legacy" in plan_of(q)["schemes"]


@pytest.mark.parametrize("q", [
    "What is the main focus of MGNREGA?",
    "MGNREGA and the focus on women",
    "it focuses on women",
])
def test_focus_as_an_ordinary_word_is_unchanged(q):
    assert p._pin_bare_focus(q) == q


# ── Plan cues (KI-237 / KI-238) ──────────────────────────────────────────────
def test_cues():
    assert plan_of("How are Focus Plus, FOCUS and PMAY-G doing in West Garo Hills?")["families"] == \
        ["count", "money"]                                                                     # T08.2
    pl = plan_of("For each district, which is bigger: Focus+ or FOCUS?")                       # T18.3
    assert pl["families"] == ["count"] and pl["rank"] and pl["by_district"]
    assert plan_of("Among MGNREGA, PMAY-G, Focus+ and FOCUS, which reaches the most beneficiaries "
                   "in Ri-Bhoi?")["rank"]                                                      # T07.3
    pl = plan_of("Top 3 districts by total beneficiaries across MGNREGA, PMAY-G, Focus+ and FOCUS?")
    assert pl["by_district"] and pl["top_district"] and pl["top_n"] == 3                       # T15.2
    pl = plan_of("For each district, which of PMAY-G, Focus+ and FOCUS comes second after MGNREGA "
                 "in beneficiary count?")                                                     # T18.2
    assert pl["place"] == 2 and pl["after"] == "MGNREGA"
    pl = plan_of("In which districts does Focus+ have more beneficiaries than PMAY-G?")       # T03.3
    assert pl["pair"] == ("Focus Plus", "PMAY-G", False)
    pl = plan_of("How many beneficiaries did MGNREGA, PMAY-G, Focus+ and FOCUS each cover in FY 2025-26?")
    assert pl["year_key"] == 2025 and pl["families"] == ["count"]                              # T01.3
    assert plan_of("What is the total beneficiary count across MGNREGA, PMAY-G, Focus+ and FOCUS?")["combined"]
    # "highest total" in a ranking is each scheme's total, not their sum
    assert not plan_of("Which scheme has a higher total financial disbursement: Focus+ or FOCUS?")["combined"]


def test_one_fy_money_and_the_other_follow_up_wordings():
    pl = plan_of("How much was disbursed under Focus+ and FOCUS in FY 2025-26?")              # T04.3
    assert pl["families"] == ["money"] and pl["year_key"] == 2025
    pl = plan_of("Excluding CM-ELEVATE, which scheme reaches the most villages?")             # T13.2
    assert pl["schemes"] == DEFAULT4 and pl["families"] == ["coverage"] and pl["rank"]
    pl = plan_of("How do Focus Plus beneficiary numbers compare with the old FOCUS producer-group scheme?")
    assert pl["schemes"] == ["Focus Plus", "Focus Legacy"] and pl["families"] == ["count"]     # T02.2
    assert plan_of("How does Focus Plus eligibility compare with Focus Legacy?") is None       # still KNOWLEDGE


def test_verifier_join_complaint_on_a_union_of_views_is_discarded():
    """T19.2: a UNION ALL of two single-view selects was rejected as joining
    v_focus_legacy to curated.fact_focus_legacy_disbursement, which it never reads."""
    sql = ("SELECT 'Focus+' s, COUNT(DISTINCT beneficiary_key) b FROM curated.v_focus_plus UNION ALL "
           "SELECT 'Focus Legacy', SUM(no_of_pg_members) FROM curated.v_focus_legacy")
    issue = ("Check 1: The SQL joins curated.v_focus_legacy directly to curated.fact_focus_legacy_disbursement "
             "(via no_of_pg_members). The schema rules explicitly prohibit it.")
    assert p._verifier_join_complaint_is_false(issue, sql)
    # a privacy fact table read in a subquery is still flagged
    leaky = sql + " WHERE 1 = 1 AND 'x' IN (SELECT pg_name FROM curated.fact_focus_legacy_disbursement)"
    assert not p._verifier_join_complaint_is_false(issue, leaky)
    # a real join is still flagged
    joined = ("SELECT COUNT(*) FROM curated.v_employment e JOIN curated.v_pmay p ON e.village_code = p.village_code")
    assert not p._verifier_join_complaint_is_false(
        "PROHIBITED join of curated.v_employment and curated.v_pmay", joined)


def test_dated_shapes_still_fall_back():
    assert plan_of("Compare Focus Plus and Focus Legacy coverage in FY 2025-26") is None       # coverage + year
    assert plan_of("Compare Focus Plus and NRLM beneficiaries in FY 2022-23") is None          # NRLM has no FY
    assert plan_of("Compare Focus Plus and Focus Legacy beneficiaries in FY 2022-23 and FY 2025-26") is None


# ── KI-233: PMAY-G counts every house record ─────────────────────────────────
def test_pmay_count_and_coverage_have_no_placeholder_filter():
    sql, _ = p._xs_query("PMAY-G", "count", None, False)
    assert "is_placeholder" not in sql
    sql, _ = p._xs_query("PMAY-G", "coverage", None, False)
    assert "is_placeholder" not in sql
    sql, _ = p._xs_query("PMAY-G", "money", None, False)
    assert "NOT is_placeholder" in sql                                    # money unchanged


def test_t01_1_states_171107_and_a_labelled_combined_total(monkeypatch):
    out, _ = answer("What is the total beneficiary count across MGNREGA, PMAY-G, Focus+ and FOCUS?", monkeypatch)
    a = out["answer"]
    assert "171,107 houses" in a and "1,494,437 households" in a
    assert "102,021 memberships paid for, in 11,906 producer groups" in a
    assert "- **Combined** — 1,873,378 (adds households + houses + farmers + memberships, so it is " \
           "indicative only)" in a


def test_t04_2_combined_money(monkeypatch):
    out, _ = answer("What is the total money released under Focus Plus, FOCUS and PMAY Gramin combined?", monkeypatch)
    assert "- **Combined** — ₹2,356.01 crore" in out["answer"]


def test_no_combined_figure_unless_asked(monkeypatch):
    out, _ = answer("Compare the number of beneficiaries in Focus+ and FOCUS.", monkeypatch)
    assert "Combined" not in out["answer"] and "never added together" in out["answer"]


# ── KI-237: answer shapes ────────────────────────────────────────────────────
def test_t03_3_districts_where_a_exceeds_b(monkeypatch):
    out, _ = answer("In which districts does Focus+ have more beneficiaries than PMAY-G?", monkeypatch)
    a = out["answer"]
    assert "Focus Plus has more beneficiaries than PMAY-G in 2 districts:" in a
    assert "- South West Garo Hills (Focus Plus 15,294 vs PMAY-G 11,947)" in a
    assert "- West Garo Hills (Focus Plus 35,039 vs PMAY-G 33,147)" in a


def test_t18_2_second_after_mgnrega(monkeypatch):
    out, _ = answer("For each district, which of PMAY-G, Focus+ and FOCUS comes second after MGNREGA "
                    "in beneficiary count?", monkeypatch)
    a = out["answer"]
    assert "Second-largest figure by district (after MGNREGA):" in a
    assert "- East Khasi Hills: **PMAY-G** (23,952)" in a
    assert "- West Garo Hills: **Focus Plus** (35,039)" in a
    assert "In all: PMAY-G in 2 districts; Focus Plus in 2 districts." in a


def test_t18_3_members_not_groups_per_district(monkeypatch):
    out, _ = answer("For each district, which is bigger: Focus+ or FOCUS?", monkeypatch)
    a = out["answer"]
    assert "**Focus Legacy** — 1 district (Ri Bhoi)" in a                  # 9,858 members > 419
    assert "**Focus Plus** — 3 districts" in a


def test_t15_2_top_districts_by_combined_count(monkeypatch):
    out, _ = answer("Top 3 districts by total beneficiaries across MGNREGA, PMAY-G, Focus+ and FOCUS?", monkeypatch)
    a = out["answer"]
    assert "Top 3 districts by combined beneficiaries" in a
    assert "1. West Garo Hills: 351,719" in a
    assert "2. East Khasi Hills: 273,064" in a
    assert out["rows"][0]["Combined (indicative)"] == 273064             # rows stay in district order


def test_t07_3_and_t20_2_name_the_leader_and_order(monkeypatch):
    out, _ = answer("Among MGNREGA, PMAY-G, Focus+ and FOCUS, which reaches the most beneficiaries in Ri-Bhoi?",
                    monkeypatch, resolved={"district": "RI BHOI"})
    assert "Largest figure: **MGNREGA**" in out["answer"]
    out, _ = answer("Rank MGNREGA, PMAY-G, Focus+ and FOCUS on coverage, beneficiaries and money spent.", monkeypatch)
    a = out["answer"]
    assert "Order by beneficiaries: MGNREGA (1,494,437) > PMAY-G (171,107) > Focus Plus (105,813) > " \
           "Focus Legacy (102,021)." in a
    assert "Order by money: MGNREGA (₹3,628.67 crore) > PMAY-G" in a


# ── KI-238: one named FY ─────────────────────────────────────────────────────
def test_t01_3_one_fy_binds_the_year_and_counts_members(monkeypatch):
    out, calls = answer("How many beneficiaries did MGNREGA, PMAY-G, Focus+ and FOCUS each cover in FY 2025-26?",
                        monkeypatch)
    a = out["answer"]
    assert a.startswith("Across Meghalaya in FY 2025-26, each scheme's own figure:")
    assert "365,556 households given work in FY 2025-26" in a
    assert "37,018 memberships paid for, in 8,806 producer groups" in a
    assert "**PMAY-G** — none — no PMAY-G records in FY 2025-26" in a
    assert all(params == [2025] and "year_key = $1" in sql for sql, params in calls)


def test_a_place_and_a_year_bind_both():
    sql, params = p._xs_query("Focus Legacy", "count", ("district", ["RI BHOI"]), False, 2025)
    assert "lgd_district = ANY($1::text[])" in sql and "year_key = $2" in sql and params == [["RI BHOI"], 2025]
    assert "year_key = 2025" in p._xs_shown(sql, params) and "ARRAY['RI BHOI']" in p._xs_shown(sql, params)


def test_model_sql_counting_focus_legacy_groups_as_beneficiaries_is_sent_back():
    sql = ("SELECT 'MGNREGA' s, SUM(households_employed) AS beneficiaries FROM curated.v_employment "
           "UNION ALL SELECT 'Focus Legacy', COUNT(DISTINCT pg_id) AS beneficiaries FROM curated.v_focus_legacy")
    two = ["MGNREGA", "Focus Legacy"]
    issue = p._cross_scheme_sql_issue("How many beneficiaries did MGNREGA and FOCUS cover?", two, {"resolved": {}}, sql)
    assert issue and "SUM(no_of_pg_members)" in issue
    assert p._cross_scheme_sql_issue("How many producer groups under MGNREGA and FOCUS?", two,
                                     {"resolved": {}}, sql) is None
    ok = sql.replace("COUNT(DISTINCT pg_id) AS beneficiaries", "SUM(no_of_pg_members) AS beneficiaries")
    assert p._cross_scheme_sql_issue("How many beneficiaries did MGNREGA and FOCUS cover?", two,
                                     {"resolved": {}}, ok) is None


# ── KI-239: the money ranking ────────────────────────────────────────────────
def test_money_ranking_reads_base_views_and_keeps_default_schemes(monkeypatch):
    sql = p._CROSS_SCHEME_MONEY_SQL
    assert "v_cross_scheme_money_district_year" not in sql and "FROM curated.v_expenditure" in sql

    async def fake_run(q):
        return [{"scheme": s, "amount_crore": v, "measure_semantics": "x"} for s, v in
                (("MGNREGA", 3628.67), ("PMAY", 2185.26), ("NRLM", 149.34), ("Focus Plus", 119.74),
                 ("CM Elevate Legacy", 82.90), ("Focus Legacy", 51.01))]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    out = asyncio.run(p._cross_scheme_money_answer("which scheme paid out the most money?"))
    assert out["schemes"] == ["MGNREGA", "PMAY-G", "Focus Plus", "Focus Legacy"]
    out = asyncio.run(p._cross_scheme_money_answer("which scheme paid out the most money, NRLM or Focus Plus?"))
    assert out["schemes"] == ["NRLM", "Focus Plus"]


# ── KI-242: the model prompt follows the same rules ──────────────────────────
def test_family_c_prompt_rules():
    src = Path(schema_context.__file__).read_text(encoding="utf-8")
    assert "SUM(households_employed) over EVERY year" in src
    assert "a PMAY-G\n           beneficiary / house COUNT is COUNT(*) of EVERY record" in src
    assert "NEVER COUNT(DISTINCT pg_id), which is producer groups" in src
