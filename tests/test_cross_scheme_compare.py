"""
Cross-scheme use-case fixes (Cross Scheme Test Cases.csv, 2026-10-10).

The officers' 20 cross-scheme cases were re-run live against megh_db and the
gateway (docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md) and failed in the
ways KNOWN_ISSUES KI-213 … KI-228 record. Each test pins a fix to the real
production function:

  _cross_scheme_compare_plan / _cross_scheme_compare_answer
          the deterministic comparison (KI-218/220/221/222/223/224/226/227/228)
  _prefers_cm_elevate_legacy        "across all financial years" no longer moves
                                    CM-ELEVATE to CM Elevate Legacy (KI-225)
  _wants_cross_scheme_money_ranking "financial assistance", "higher: A or B" (KI-214)
  _year_clarification               each scheme's own years (KI-219)
  _cross_scheme_sql_issue           model-SQL guards (KI-213/218/220/223/227)
  _verifier_join_complaint_on_aggregates   verifier false positive (KI-221)
  _range_claim_misstated / _cross_unit_total_stated   composer checks (KI-226/220)
  classify_intent                   comparisons stay on DATA (KI-222)

No model or DB: query results are faked at fetch_rows / resolve_entities, and the
SQL each fix produces or rejects is asserted directly.
    python -m pytest tests/test_cross_scheme_compare.py -q
"""
import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import pipeline as p  # noqa: E402

ALL7 = ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy", "CM Elevate Legacy", "NRLM"]


def pinned(q: str) -> str:
    """The question as _run_pipeline hands it on: spelling, CM Elevate pin, bare Focus pin."""
    return p._pin_bare_focus(p._pin_cm_elevate_dataset(p._correct_scheme_spelling(q)))


# ── 1. Which questions the deterministic comparison takes ────────────────────
OFFICER_CASES = {
    "CROSS-1": ("What is the beneficiary count across MGNREGA, PMAY-G, Focus+, and FOCUS?",
                ["MGNREGA", "PMAY-G", "Focus Plus", "Focus Legacy"], ["count"]),
    "CROSS-2": ("Compare the number of beneficiaries in Focus+ and FOCUS.",
                ["Focus Plus", "Focus Legacy"], ["count"]),
    "CROSS-3": ("Compare the performance of Focus+ and CM-ELEVATE across districts based on "
                "beneficiary/application counts.", ["Focus Plus", "CM Elevate"], ["count"]),
    "CROSS-8": ("Compare Focus+, FOCUS, and CM-ELEVATE performance in East Khasi Hills.",
                ["Focus Plus", "CM Elevate", "Focus Legacy"], ["count", "money"]),
    "CROSS-9": ("Compare the coverage of MGNREGA, PMAY-G, and CM-ELEVATE in Mawphlang.",
                ["MGNREGA", "PMAY-G", "CM Elevate"], ["coverage"]),
    "CROSS-11": ("How much financial assistance or expenditure was recorded under each scheme in "
                 "East Khasi Hills?", ALL7, ["money"]),
    "CROSS-13": ("Which scheme has the widest coverage across districts of Meghalaya?", ALL7, ["coverage"]),
    "CROSS-14": ("What is the performance of MGNREGA, PMAY-G, Focus+, and CM-ELEVATE in East Khasi Hills?",
                 ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate"], ["count", "money"]),
    "CROSS-15": ("Which districts have the highest concentration of beneficiaries or applications across "
                 "the schemes?", ALL7, ["count"]),
    "CROSS-17": ("Give me a district-wise summary comparing MGNREGA, PMAY-G, Focus+, FOCUS, and CM-ELEVATE.",
                 ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy"], ["count", "money"]),
    "CROSS-18": ("For each district, identify which scheme has the highest beneficiary/application count.",
                 ALL7, ["count"]),
    "CROSS-20": ("Give me an overall performance comparison of MGNREGA, PMAY-G, Focus+, FOCUS, and "
                 "CM-ELEVATE based on coverage and financial performance.",
                 ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy"], ["money", "coverage"]),
}


@pytest.mark.parametrize("case", sorted(OFFICER_CASES))
def test_officer_cross_scheme_cases_take_the_deterministic_comparison(case):
    q, schemes, families = OFFICER_CASES[case]
    plan = p._cross_scheme_compare_plan(pinned(q))
    assert plan is not None, case
    assert plan["schemes"] == schemes
    assert plan["families"] == families


def test_typed_all_years_keeps_the_comparison_and_the_applications_dataset():
    # The user's own phrasing in the UI (2026-10-10 screenshot) and its CM Elevate form.
    q = "Compare Focus+ and CM-ELEVATE beneficiary/application counts across all financial years"
    plan = p._cross_scheme_compare_plan(pinned(q))
    assert plan is not None and plan["schemes"] == ["Focus Plus", "CM Elevate"]


def test_district_level_flags():
    plan = p._cross_scheme_compare_plan(pinned(OFFICER_CASES["CROSS-18"][0]))
    assert plan["by_district"] and plan["rank"] and not plan["top_district"]
    plan = p._cross_scheme_compare_plan(pinned(OFFICER_CASES["CROSS-15"][0]))
    assert plan["by_district"] and plan["top_district"] and not plan["rank"]


@pytest.mark.parametrize("q", [
    "Compare MGNREGA and PMAY-G spending in Meghalaya",               # the two-view schemes: own path
    "How many MGNREGA person-days and PMAY-G houses in 2023-24?",
    "Compare Focus Plus and Focus Legacy beneficiaries in FY 2022-23",  # a specific year
    "Compare Focus Plus and CM Elevate eligibility criteria",           # KNOWLEDGE
    "Which villages have both MGNREGA and PMAY-G activity?",            # set question
    "How many common districts in both schemes?",
    "Compare beneficiaries in both schemes",                            # older two-scheme phrasing
    "Compare piggery applications in CM Elevate and Focus Plus beneficiaries",   # sub-scheme
    "Compare women beneficiaries of Focus Plus and MGNREGA",            # one scheme's measure
    "Focus Plus beneficiaries by district",                             # one scheme
    "which scheme should I apply for as a farmer?",                     # recommender
    "which scheme is best for women?",
    "Compare Focus Plus and NRLM SHG members",
    "Compare tranche 2 payments of Focus Plus with Focus Legacy",
    "what is focus",
    "Compare MGNREGA and Focus Plus beneficiaries in each village of Mawphlang",
    "CM Elevate Legacy records by scheme",
])
def test_everything_else_keeps_its_current_path(q):
    assert p._cross_scheme_compare_plan(pinned(q)) is None


# ── 2. KI-225: an all-years scope is not a CM Elevate Legacy cue ─────────────
@pytest.mark.parametrize("q", [
    "Compare Focus+ and CM-ELEVATE beneficiary counts across all financial years.",
    "How many CM Elevate applications for all financial years combined",   # the year chip's own text
])
def test_all_financial_years_keeps_cm_elevate_on_the_applications_data(q):
    assert p._pin_cm_elevate_dataset(q) == q


@pytest.mark.parametrize("q", [
    "how many CM Elevate records in FY 2024-25",
    "CM Elevate records in each financial year",
    "what is the total amount disbursed under CM Elevate",
])
def test_a_specific_year_a_breakdown_and_money_still_pin_legacy(q):
    assert "CM Elevate Legacy" in p._pin_cm_elevate_dataset(q)


# ── 3. KI-214: the deterministic money ranking catches its own wordings ──────
def test_financial_assistance_and_higher_a_or_b_are_money_rankings():
    assert p._wants_cross_scheme_money_ranking(
        "Which scheme provided the highest total financial assistance across Meghalaya?")
    assert p._wants_cross_scheme_money_ranking(
        "Which scheme has a higher total financial disbursement: Focus+ or CM-ELEVATE?")
    for q in ("which scheme has the most applications?", "which scheme covers the most villages?",
              "compare MGNREGA and PMAY-G spending", "which district spent the most?"):
        assert not p._wants_cross_scheme_money_ranking(q), q


def test_unnamed_money_ranking_still_uses_the_existing_answer(monkeypatch):
    called = {}

    async def fake_rank(question):
        called["q"] = question
        return {"route": "data", "answer": "ranked"}
    monkeypatch.setattr(p, "_cross_scheme_money_answer", fake_rank)
    out = asyncio.run(p._answer_data(
        "Which scheme provided the highest total financial assistance across Meghalaya?"))
    assert out["answer"] == "ranked" and called


# ── 4. KI-219: the multi-scheme year pause states each scheme's own years ────
def test_year_pause_names_each_schemes_years_when_they_differ():
    e = p._year_clarification("What is the beneficiary count across MGNREGA and Focus Plus",
                              ["MGNREGA", "Focus Plus"])
    assert "MGNREGA: FY 2022-23 to FY 2025-26" in e.question
    assert "Focus Plus: FY 2022-23 and FY 2025-26" in e.question
    assert "compares only the schemes that have data for it" in e.question
    assert [o["label"] for o in e.options][-1] == "All financial years combined"


def test_year_pause_for_one_scheme_is_unchanged():
    e = p._year_clarification("total CM Elevate Legacy disbursement", ["CM Elevate Legacy"])
    assert e.question.startswith("CM Elevate Legacy data is available for FY 2024-25 and FY 2025-26.")


def test_scheme_years_text():
    assert p._scheme_years_text("Focus Legacy") == "FY 2021-22, FY 2022-23, FY 2024-25 and FY 2025-26"
    assert p._scheme_years_text("CM Elevate") == "no year recorded"
    assert p._scheme_years_text("NRLM").startswith("the SHG register to date")


# ── 5. The deterministic answer: SQL and wording ─────────────────────────────
# Live figures (megh_db, 2026-10-10) so the wording checks read real numbers.
_FAKE = {
    ("v_employment", "households_employed"): (365556, None),
    ("v_expenditure", "total_exp"): (3628.67, None),
    ("v_pmay", "COUNT(*)"): (170981, None),
    ("v_pmay", "amount_released"): (2185.26, None),
    ("v_focus_plus", "beneficiary_key"): (105813, None),
    ("v_focus_plus", "amount_disbursed"): (119.74, None),
    ("v_cm_elevate", "request_id"): (8600, None),
    ("v_focus_legacy", "no_of_pg_members"): (102021, 11906),
    ("v_focus_legacy", "amount_disbursed"): (51.01, None),
    ("v_cm_elevate_disbursement", "COUNT(*)"): (2823, None),
    ("v_cm_elevate_disbursement", "total_disbursement"): (82.90, None),
    ("v_nrlm", "total_members"): (300000, 40629),       # below MGNREGA here, to test a mixed lead
    ("v_nrlm", "revolving_fund_amount"): (95.0, None),
}
_VILLAGES = {"v_employment": 4673, "v_pmay": 5120, "v_focus_plus": 3523, "v_cm_elevate": 2087,
             "v_focus_legacy": 3430, "v_cm_elevate_disbursement": 900, "v_nrlm": 4500}
# Per district: MGNREGA households lead everywhere except Ri Bhoi, where NRLM's members do.
_DISTRICTS = {"EAST KHASI HILLS": 1.0, "RI BHOI": 0.5, "WEST GARO HILLS": 1.2}


def _fake_value(sql: str):
    view = re.search(r"FROM curated\.(\w+)", sql).group(1)
    if "village_code" in sql.split("FROM")[0]:
        return _VILLAGES[view], 12
    for (v, key), val in _FAKE.items():
        if v == view and key in sql.split("FROM")[0]:
            return val
    raise AssertionError(f"unexpected query {sql}")


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, sql, params=None):
        self.calls.append((sql, params))
        value, extra = _fake_value(sql)
        def row(v, x):
            r = {"value": v}
            if " AS extra" in sql:
                r["extra"] = x
            return r
        if "GROUP BY lgd_district" in sql:
            out = []
            for d, f in _DISTRICTS.items():
                v = value * f
                if "households_employed" in sql and d == "RI BHOI":
                    v = 10000                                  # NRLM leads in Ri Bhoi
                out.append({"district": d, **row(v, x=(extra or 0))})
            return out
        return [row(value, extra)]


def _answer(q, monkeypatch, resolved=None, display=None):
    rec = _Recorder()
    monkeypatch.setattr(p, "fetch_rows", rec)
    plan = p._cross_scheme_compare_plan(pinned(q))
    er = {"resolved": resolved or {}, "display": display or {}, "notes": []}
    out = asyncio.run(p._cross_scheme_compare_answer(pinned(q), plan, er))
    return out, rec.calls


def test_beneficiary_count_states_each_schemes_own_measure_and_no_total(monkeypatch):
    out, calls = _answer(OFFICER_CASES["CROSS-1"][0], monkeypatch)
    a = out["answer"]
    assert "365,556 households given work in FY 2025-26" in a
    assert "170,981 houses sanctioned" in a
    assert "105,813 beneficiaries" in a
    # KI-228: Focus Legacy names its unit — memberships in producer groups, no people
    assert "102,021 memberships paid for, in 11,906 producer groups" in a
    assert not re.search(r"\btotal\b[^.]*\d", a, re.IGNORECASE)
    assert "never added together" in a
    sqls = " ".join(s for s, _ in calls)
    assert "COUNT(DISTINCT beneficiary_key)" in sqls                    # KI-213
    assert re.search(r"FROM curated\.v_pmay\s+WHERE NOT is_placeholder", sqls)
    assert "Unresolved" not in sqls                                     # totals keep the placeholder rows
    assert out["rows"] and {r["Scheme"] for r in out["rows"]} == set(OFFICER_CASES["CROSS-1"][1])


def test_money_comparison_never_reports_a_cm_elevate_amount(monkeypatch):
    out, calls = _answer("Compare the total expenditure or disbursement under MGNREGA, PMAY-G, Focus+, and "
                         "CM-ELEVATE across all financial years", monkeypatch)
    a = out["answer"]
    assert "₹3,628.67 crore" in a and "₹2,185.26 crore" in a and "₹119.74 crore" in a
    if "CM Elevate Legacy" not in out["schemes"]:
        assert "CM Elevate** — no money recorded" in a
        assert not any("v_cm_elevate\n" in s or s.endswith("v_cm_elevate") for s, _ in calls)
    assert not any(r.get("Scheme") == "CM Elevate" and r.get("Measure") == "₹ crore" for r in out["rows"])
    assert len(out["rows"]) == len([s for s in out["schemes"] if s != "CM Elevate"])   # KI-223: no row flood


def test_a_district_is_bound_into_every_schemes_query(monkeypatch):
    out, calls = _answer(OFFICER_CASES["CROSS-14"][0], monkeypatch,
                         resolved={"district": "EAST KHASI HILLS"})
    assert calls and all(params == [["EAST KHASI HILLS"]] for _s, params in calls)   # KI-227
    assert all("lgd_district = ANY($1::text[])" in s for s, _ in calls)
    assert out["answer"].startswith("In East Khasi Hills, each scheme's own figure:")


def test_a_block_uses_block_name_raw_for_focus_plus_only(monkeypatch):
    q = "Compare the coverage of MGNREGA, PMAY-G, Focus+ and CM-ELEVATE in Mawphlang block"
    out, calls = _answer(q, monkeypatch, resolved={"block": "MAWPHLANG"})
    for sql, params in calls:
        assert params == [["MAWPHLANG"]]
        if "v_focus_plus" in sql:
            assert "UPPER(block_name_raw) = ANY($1::text[])" in sql
        else:
            assert "lgd_block = ANY($1::text[])" in sql
    # village counts drop the synthetic Unresolved placeholder; PMAY-G has none
    assert all(("Unresolved" in s) == ("v_pmay" not in s) for s, _ in calls)


def test_out_of_scope_resolution_falls_back(monkeypatch):
    for extra in ({"village_code": 273107}, {"year_key": 2023}, {"cm_scheme": "Piggery"},
                  {"assembly_constituency": "MAWLAI"}, {"tranche_label": "Tranch 2"}):
        out, calls = _answer(OFFICER_CASES["CROSS-1"][0], monkeypatch, resolved=extra)
        assert out is None and not calls, extra


def test_each_district_leader_is_stated(monkeypatch):
    out, _ = _answer(OFFICER_CASES["CROSS-18"][0], monkeypatch)
    a = out["answer"]
    assert "Largest figure by district:" in a
    assert re.search(r"\*\*MGNREGA\*\* — 2 districts \(East Khasi Hills, West Garo Hills\)", a)
    assert re.search(r"\*\*NRLM\*\* — 1 district \(Ri Bhoi\)", a)
    assert [r["District"] for r in out["rows"]] == ["East Khasi Hills", "Ri Bhoi", "West Garo Hills"]


def test_district_list_is_compared_district_by_district(monkeypatch):
    q = ("Compare Focus+ and CM-ELEVATE beneficiary/application counts across East Khasi Hills, "
         "West Garo Hills, and Ri Bhoi.")
    out, calls = _answer(q, monkeypatch,
                         resolved={"district_list": ["EAST KHASI HILLS", "WEST GARO HILLS", "RI BHOI"]})
    assert [r["District"] for r in out["rows"]] == ["East Khasi Hills", "West Garo Hills", "Ri Bhoi"]
    assert {"Focus Plus beneficiaries", "CM Elevate applicants"} <= set(out["rows"][0])


def test_widest_coverage_names_the_leader(monkeypatch):
    out, _ = _answer(OFFICER_CASES["CROSS-13"][0], monkeypatch)
    assert "Widest coverage: **PMAY-G** — 5,120 villages across 12 districts." in out["answer"]


def test_answer_data_answers_before_any_pause(monkeypatch):
    """KI-224/KI-219: a named multi-scheme comparison with no year is answered over
    each scheme's whole window — no year / scope / scheme pause first."""
    async def fake_resolve(question, schemes, prior_resolved=None, village_hint=None):
        return {"resolved": {}, "display": {}, "notes": []}
    monkeypatch.setattr(p, "resolve_entities", fake_resolve)
    monkeypatch.setattr(p, "fetch_rows", _Recorder())
    out = asyncio.run(p._answer_data(pinned(OFFICER_CASES["CROSS-1"][0])))
    assert out["route"] == "data" and "365,556 households" in out["answer"]


def test_cross_scheme_comparison_is_a_data_question():
    """KI-222: "What is the performance of …" opened with "what is" and went to the KB."""
    assert asyncio.run(p.classify_intent(pinned(OFFICER_CASES["CROSS-14"][0]))) == "DATA"
    assert asyncio.run(p.classify_intent(pinned(OFFICER_CASES["CROSS-13"][0]))) == "DATA"


# ── 6. Guards on model SQL (every other wording) ─────────────────────────────
# The exact SQL the generator produced in the live retest (2026-10-10).
SQL_FAKE_FOCUS_CODE = """SELECT 'Focus Plus' AS scheme, COUNT(DISTINCT beneficiary_key) AS beneficiaries
FROM curated.v_focus_plus
UNION ALL
SELECT 'FOCUS' AS scheme, 0 AS beneficiaries
FROM curated.v_cross_scheme_money_district_year
WHERE scheme_code = 'FOCUS'
LIMIT 2"""
SQL_ZERO_CRORE = """SELECT
    COALESCE(SUM(CASE WHEN scheme_code = 'Focus Plus' THEN amount_crore ELSE 0 END), 0) AS focus_plus_amount_crore
FROM curated.v_cross_scheme_money_district_year
WHERE scheme_code IN ('Focus Plus', 'Focus Legacy', 'CM Elevate Legacy')
LIMIT 1"""
SQL_ROW_FLOOD = """SELECT 'MGNREGA' AS scheme, ROUND(SUM(total_exp) / 100.0, 2) AS total_expenditure_crore
FROM curated.v_expenditure
UNION ALL
SELECT 'Focus Plus' AS scheme, ROUND(SUM(amount_disbursed) / 10000000.0, 2) AS total_disbursement_crore
FROM curated.v_focus_plus
UNION ALL
SELECT 'CM Elevate' AS scheme, NULL AS total_disbursement_crore
FROM curated.v_cm_elevate"""
SQL_UNAGGREGATED_BRANCH = """SELECT 'Focus Plus' AS scheme, COUNT(DISTINCT beneficiary_key) AS n
FROM curated.v_focus_plus
UNION ALL
SELECT 'CM Elevate' AS scheme, request_id AS n
FROM curated.v_cm_elevate"""
SQL_BRANCH_WITHOUT_DISTRICT = """SELECT 'MGNREGA' AS scheme, ROUND(SUM(total_exp) / 100.0, 2) AS amount_crore
FROM curated.v_expenditure
WHERE lgd_district = 'EAST KHASI HILLS'
UNION ALL
SELECT 'Focus Plus' AS scheme, ROUND(SUM(amount_disbursed) / 10000000.0, 2) AS amount_crore
FROM curated.v_focus_plus
UNION ALL
SELECT 'NRLM' AS scheme, ROUND(SUM(revolving_fund_amount + cif_amount) / 10000000.0, 2) AS amount_crore
FROM curated.v_nrlm
WHERE lgd_district = 'EAST KHASI HILLS'"""
SQL_FAKE_CME_ZERO = """SELECT 'MGNREGA' AS scheme, ROUND(SUM(total_exp) / 100.0, 2) AS amount_crore
FROM curated.v_expenditure WHERE lgd_district = 'EAST KHASI HILLS'
UNION ALL
SELECT 'CM Elevate' AS scheme, 0.00 AS amount_crore FROM (SELECT 1) AS dummy"""
SQL_GOOD_CROSS_JOIN = """SELECT m.mgnrega_beneficiaries, p.pmay_beneficiaries, f.focus_plus_beneficiaries
FROM (SELECT SUM(households_employed) AS mgnrega_beneficiaries
      FROM curated.v_employment
      WHERE year_key = (SELECT MAX(year_key) FROM curated.v_employment)) m
CROSS JOIN (SELECT COUNT(*) AS pmay_beneficiaries FROM curated.v_pmay WHERE NOT is_placeholder) p
CROSS JOIN (SELECT COUNT(DISTINCT beneficiary_key) AS focus_plus_beneficiaries FROM curated.v_focus_plus) f
LIMIT 1"""
SQL_DISTRICT_KEY_LIST = """SELECT d.lgd_district, COALESCE(f.n, 0) AS fp, COALESCE(c.n, 0) AS cme
FROM (SELECT DISTINCT lgd_district FROM curated.v_focus_plus
      UNION SELECT DISTINCT lgd_district FROM curated.v_cm_elevate) d
LEFT JOIN (SELECT lgd_district, COUNT(DISTINCT beneficiary_key) n FROM curated.v_focus_plus GROUP BY 1) f
  ON f.lgd_district = d.lgd_district
LEFT JOIN (SELECT lgd_district, COUNT(DISTINCT request_id) n FROM curated.v_cm_elevate GROUP BY 1) c
  ON c.lgd_district = d.lgd_district"""
EKH = {"resolved": {"district": "EAST KHASI HILLS"}}
NONE = {"resolved": {}}
TWO = ["Focus Plus", "Focus Legacy"]


@pytest.mark.parametrize("sql,er,needle", [
    (SQL_FAKE_FOCUS_CODE, NONE, "holds only scheme_code 'MGNREGA' and 'PMAY'"),       # KI-218
    (SQL_ZERO_CRORE, NONE, "'CM Elevate Legacy', 'Focus Legacy', 'Focus Plus'"),       # KI-218
    (SQL_ROW_FLOOD, NONE, "CM Elevate out of the money query"),                       # KI-223 / KI-220
    (SQL_UNAGGREGATED_BRANCH, NONE, "reads curated.v_cm_elevate has no aggregate"),   # KI-223
    (SQL_BRANCH_WITHOUT_DISTRICT, EKH, "curated.v_focus_plus do not filter on it"),    # KI-227
    (SQL_FAKE_CME_ZERO, EKH, "constant 0 / NULL amount for CM Elevate"),              # KI-220
])
def test_wrong_multi_scheme_shapes_are_sent_back_for_repair(sql, er, needle):
    issue = p._cross_scheme_sql_issue("Compare the schemes in East Khasi Hills", TWO, er, sql)
    assert issue and needle in issue


def test_focus_plus_payments_are_not_beneficiaries():
    sql = ("SELECT 'Focus Plus' s, COUNT(*) AS beneficiaries FROM curated.v_focus_plus "
           "UNION ALL SELECT 'Focus Legacy', COUNT(DISTINCT pg_id) FROM curated.v_focus_legacy")
    assert "COUNT(DISTINCT beneficiary_key)" in p._cross_scheme_sql_issue(
        "Compare beneficiaries in Focus Plus and Focus Legacy", TWO, NONE, sql)                # KI-213
    assert p._cross_scheme_sql_issue("Compare payments in Focus Plus and Focus Legacy", TWO, NONE, sql) is None


@pytest.mark.parametrize("sql", [SQL_GOOD_CROSS_JOIN, SQL_DISTRICT_KEY_LIST])
def test_correct_multi_scheme_shapes_pass(sql):
    assert p._cross_scheme_sql_issue("Compare beneficiaries across the schemes", TWO, NONE, sql) is None


def test_single_scheme_sql_is_never_checked():
    assert p._cross_scheme_sql_issue("x", ["Focus Plus"], EKH, SQL_BRANCH_WITHOUT_DISTRICT) is None


def test_verifier_join_complaint_on_aggregated_subqueries_is_discarded():
    c = ("Check 1: The SQL joins curated.v_employment with curated.v_pmay via CROSS JOIN. This violates "
         "the prohibition: 'NEVER join curated.v_pmay -> curated.fact_mgnrega_employment directly.'")
    assert p._verifier_join_complaint_on_aggregates(c, SQL_GOOD_CROSS_JOIN)                     # KI-221
    row_join = ("SELECT e.lgd_district, COUNT(*) FROM curated.v_employment e JOIN curated.v_pmay p "
                "ON e.village_code = p.village_code GROUP BY 1")
    assert not p._verifier_join_complaint_on_aggregates(c, row_join)
    mixed = "SELECT * FROM curated.v_employment e CROSS JOIN (SELECT COUNT(*) n FROM curated.v_pmay) p"
    assert not p._verifier_join_complaint_on_aggregates(c, mixed)
    assert not p._verifier_join_complaint_on_aggregates("Check 4: wrong metric", SQL_GOOD_CROSS_JOIN)


# ── 7. Composer checks ───────────────────────────────────────────────────────
_D = ["EASTERN WEST KHASI HILLS", "EAST KHASI HILLS", "WEST GARO HILLS", "RI BHOI"]
_RANGE_ROWS = ([{"scheme": "MGNREGA", "lgd_district": d, "beneficiary_count": v}
                for d, v in zip(_D, [11147, 56936, 65886, 26631])] +
               [{"scheme": "PMAY-G", "lgd_district": d, "beneficiary_count": v}
                for d, v in zip(_D, [5494, 23921, 33143, 17525])])


def test_a_range_that_is_not_the_range_is_caught():
    bad = ("MGNREGA has the highest beneficiary count in every district shown, with values ranging from "
           "11,147 in EASTERN WEST KHASI HILLS to 56,936 in EAST KHASI HILLS.")
    assert p._range_claim_misstated(bad, _RANGE_ROWS)                                            # KI-226
    good = "MGNREGA leads, with values ranging from 11,147 to 65,886; PMAY-G counts between 5,494 and 33,143."
    assert not p._range_claim_misstated(good, _RANGE_ROWS)
    assert not p._range_claim_misstated("The remaining districts range from 11,147 to 56,936.", _RANGE_ROWS)
    assert not p._range_claim_misstated("MGNREGA rose from 11,147 to 56,936.", _RANGE_ROWS)


def test_a_total_across_different_schemes_is_caught():
    rows = [{"scheme": "Focus Plus", "beneficiaries": 105813}, {"scheme": "Focus Legacy", "beneficiaries": 11906}]
    bad = "Focus Legacy had 11,906 beneficiaries while Focus Plus had 105,813. The total across both is 117,719."
    assert p._cross_unit_total_stated(bad, rows, TWO)                                             # KI-220
    assert not p._cross_unit_total_stated("Focus Plus has 105,813; Focus Legacy 11,906 groups.", rows, TWO)
    mp = [{"scheme": "MGNREGA", "cr": 3628.67}, {"scheme": "PMAY", "cr": 2185.26}]
    assert not p._cross_unit_total_stated("Together they total 5,813.93 crore.", mp, ["MGNREGA", "PMAY-G"])


# ── 8. User report 2026-10-10: a seven-scheme money question with "status" ──
# "How much has been disbursed to beneficiaries with status across MGNREGA, PMAY-G,
# Focus Plus, CM Elevate, Focus, NRLM and CM Elevate Legacy …" — "status" keeps it on
# the model path; the rejected total fell back to a bare row dump and CM Elevate was
# never mentioned.
_STATUS_ROWS = [
    {"scheme": "CM Elevate Legacy", "amount_crore": 82.90}, {"scheme": "MGNREGA", "amount_crore": 3628.67},
    {"scheme": "NRLM", "amount_crore": 149.34}, {"scheme": "Focus Legacy", "amount_crore": 51.01},
    {"scheme": "PMAY-G", "amount_crore": 2185.26}, {"scheme": "Focus Plus", "amount_crore": 119.74},
]
_STATUS_Q = ("How much has been disbursed to beneficiaries with status across MGNREGA, PMAY-G, Focus Plus, "
             "CM Elevate, Focus Legacy, NRLM and CM Elevate Legacy for all of Meghalaya across all financial years")


def test_status_question_stays_on_the_model_path():
    assert p._cross_scheme_compare_plan(_STATUS_Q) is None


def test_rejected_cross_scheme_total_falls_back_to_a_labelled_side_by_side():
    out = p._cross_scheme_side_by_side(_STATUS_ROWS)
    assert "- **MGNREGA** — ₹3,628.67 crore — expenditure actually incurred" in out
    assert "- **Focus Legacy** — ₹51.01 crore — cash remitted to producer groups" in out
    assert "not added together" in out
    assert p._cross_scheme_side_by_side([{"district": "RI BHOI", "n": 3}, {"district": "X", "n": 4}]) is None


def test_a_named_cm_elevate_with_no_money_is_explained():
    all7 = list(p.SCHEME_CATALOG)
    out = p._cm_elevate_no_money_note(_STATUS_Q, all7, _STATUS_ROWS, "Each scheme's own figure: …")
    assert "CM Elevate has no figure here" in out
    # not when CM Elevate is already in the answer, not for a count, not for one scheme
    said = "… CM Elevate — no money recorded."
    assert p._cm_elevate_no_money_note(_STATUS_Q, all7, _STATUS_ROWS, said) == said
    assert p._cm_elevate_no_money_note("How many applicants under Focus Plus and CM Elevate", ["Focus Plus", "CM Elevate"],
                                       [], "x") == "x"
    assert p._cm_elevate_no_money_note(_STATUS_Q, ["CM Elevate"], [], "x") == "x"
    assert p._cm_elevate_no_money_note(_STATUS_Q, ["MGNREGA", "CM Elevate Legacy"], _STATUS_ROWS, "x") == "x"
