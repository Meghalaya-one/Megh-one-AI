"""
CM Elevate Legacy — the sanction-and-disbursement dataset (DB: CM Elevate
Disbursement, curated.v_cm_elevate_disbursement), wired as its own scheme beside
the CM Elevate APPLICATIONS dataset it shares a name (and no key) with.

Covers the prompt-layer v2 bank (data/cm_elevate_legacy/
cmelevatelegacy_prompt_few_shots.yaml), which dataset a "CM Elevate" question
lands on, the resolver, the not-held gate, the prompt assembly and the
registries a new scheme must appear in. Pure Python — no model or DB.
    python -m pytest tests/test_cm_elevate_legacy.py -q
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (annotations, auth, edge, entity_resolver, followups,  # noqa: E402
                 pipeline as p, prompt_builder as pb, schema_context, schema_introspect)

LEGACY = "CM Elevate Legacy"
_DATA = Path(__file__).resolve().parents[1] / "data" / "cm_elevate_legacy"
_BANK = yaml.safe_load((_DATA / "cmelevatelegacy_prompt_few_shots.yaml").read_text(encoding="utf-8"))
_VIEW_COLS = {c["name"] for c in yaml.safe_load(
    (_DATA / "cmelevatelegacy_schema_partitions.yaml").read_text(encoding="utf-8")
)["datasets"]["v_cm_elevate_disbursement"]["columns"]}


_AC_SHOTS = {"X07", "X08"}   # constituency through dim_geography


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    annotations.load_all()
    entity_resolver.load_all()


def _ids(question: str, k: int = 5) -> list[str]:
    by_q = {e["question"]: e["id"] for e in annotations._few_shot_cache[LEGACY]}
    return [by_q[x["question"]] for x in annotations.few_shot_examples([LEGACY], question, top_k=k)]


# ── 1. The bank matches the reviewed document ───────────────────────────────
def test_bank_counts_match_the_document():
    pool = _BANK["sql_generation_examples"]
    # X01 / X02 / X07 / X08 became SQL shots on 2026-09-25 (sanction share,
    # not-sanctioned count, constituency via dim_geography) - 110+4 / 22-4.
    assert sum(e.get("status") != "UNANSWERABLE" for e in pool) == 114
    assert sum(e.get("status") == "UNANSWERABLE" for e in pool) == 18
    assert len(_BANK["clarification_shots"]) == 8
    assert len(_BANK["common_mistakes"]) == 14
    assert len(_BANK["answer_shots"]) == 8


def test_bank_sql_parses_and_reads_only_the_view():
    sqlglot = pytest.importorskip("sqlglot")
    exp = sqlglot.exp
    for e in _BANK["sql_generation_examples"]:
        for key in ("sql", "caveat_sql"):
            if not e.get(key):
                continue
            tree = sqlglot.parse_one(e[key], read="postgres")
            aliases = {a.alias for a in tree.find_all(exp.Alias)}
            tables = {t.name for t in tree.find_all(exp.Table)}
            # The one permitted extra: the declared geography_key -> dim_geography
            # FK, used only by the constituency shots for ac_name.
            allowed = {"v_cm_elevate_disbursement"} | (
                {"dim_geography"} if e["id"] in _AC_SHOTS else set())
            assert tables == allowed, e["id"]
            extra = {"ac_name"} if e["id"] in _AC_SHOTS else set()
            unknown = {c.name for c in tree.find_all(exp.Column)} - _VIEW_COLS - aliases - extra
            assert not unknown, (e["id"], unknown)


def test_policy_gated_clarify_shots_stay_out_of_the_sql_pool():
    pool_ids = {e["id"] for e in annotations._few_shot_cache[LEGACY]}
    assert {"M01", "R01"} <= pool_ids          # default policy: answer with the split
    assert not pool_ids & {"K01", "K02", "K06"}


# ── 2. Which CM Elevate dataset a question lands on ─────────────────────────
@pytest.mark.parametrize("question", [
    "how many CM Elevate applications are on hold",
    "CM Elevate applications by district",
    "what is the gender split of CM Elevate applicants",
    "how many piggery applications are pending",   # "pending" = on hold there
])
def test_application_questions_stay_on_cm_elevate(question):
    q = p._pin_cm_elevate_dataset(question)
    assert q == question
    assert p._shortcut_scheme(q) == ["CM Elevate"]


@pytest.mark.parametrize("question", [
    "what is the total amount disbursed under CM Elevate",
    "CM-ELEVATE loans by lender",
    "how many CM Elevate records in FY 2024-25",
])
def test_money_year_lender_questions_move_to_legacy(question):
    q = p._pin_cm_elevate_dataset(question)
    assert "CM Elevate Legacy" in q
    assert p._shortcut_scheme(q) == [LEGACY]


@pytest.mark.parametrize("question", [
    "CM Elevate Legacy disbursement by district",
    "piggery disbursement in Ri Bhoi",
    "how many LIFCOM loans in Garo Hills",
])
def test_named_or_vocabulary_pins_legacy(question):
    assert p._shortcut_scheme(p._pin_cm_elevate_dataset(question)) == [LEGACY]


def test_legacy_name_is_not_also_read_as_the_applications_dataset():
    assert p._named_schemes("CM Elevate Legacy disbursement by district") == [LEGACY]
    assert p._named_schemes("CM Elevate Disbursement in Ri Bhoi") == [LEGACY]
    # the lone word "Elevate" must not be "corrected" into a second "CM"
    assert p._correct_scheme_spelling("CM Elevate Legacy disbursement") == \
        "CM Elevate Legacy disbursement"


def test_scheme_pause_offers_legacy():
    labels = [o["label"] for o in p._scheme_clarification("how many records").options]
    assert any(LEGACY in lbl for lbl in labels)


# ── 3. Resolver ──────────────────────────────────────────────────────────────
def test_resolver_loads_the_legacy_catalogue():
    cat = entity_resolver._catalog[LEGACY]
    assert len(cat["district"]) == 12 and len(cat["cm_scheme"]) == 13 and cat["block"]


@pytest.mark.parametrize("text,want", [
    ("sericulture spinning", ["Meghalaya Sericulture & Weaving Scheme (spinning)"]),
    ("weaving", ["Meghalaya Sericulture & Weaving Scheme(weaving)"]),
    ("compare piggery and poultry", ["Meghalaya Piggery Development Scheme",
                                     "Meghalaya Poultry Farming Scheme"]),
    ("PTV records", ["Prime Tourism Vehicle Scheme"]),
])
def test_legacy_scheme_literals(text, want):
    assert entity_resolver.resolve_cm_scheme(text, LEGACY).values == want


def test_bare_sericulture_is_asked_and_both_is_taken_at_its_word():
    assert p._cm_legacy_sericulture_choice("sericulture records by district") == "ask"
    both = p._cm_legacy_sericulture_choice("both sericulture schemes by district")
    assert sorted(both) == sorted(p._CM_LEGACY_SERICULTURE)
    assert p._cm_legacy_sericulture_choice("sericulture spinning records") is None


# ── 4. Not-held gate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("question", [
    "names of piggery beneficiaries in Umling",
    "who got the maximum money",
    "month wise disbursement for 2024-25",
    "how many women entrepreneurs got support",
    "how many SHG group applicants",
    "loan repayment status",
    "what percentage did the bank contribute",
    "how many jobs created by poultry units",
])
def test_not_held_questions_are_refused(question):
    assert p._cm_legacy_not_held(question) is not None


@pytest.mark.parametrize("question", [
    "how many beneficiaries who took a loan in poultry",        # relative "who"
    "list individual records of goat farming in Ri Bhoi",       # "individual"
    "give me an overview of disbursement by district",          # "overview"
    "we want to focus on piggery - how many records in each district",
    "how much subsidy was paid between April and December 2024",
])
def test_answerable_questions_are_not_refused(question):
    assert p._cm_legacy_not_held(question) is None


def test_lender_categories_are_queryable_but_branches_are_not():
    assert p._bank_clarification("CM Elevate Legacy loans by Bank and LIFCOM") is None
    assert p._bank_clarification("which bank branch gave most CM Elevate Legacy loans") is not None


# ── 5. Prompt assembly ───────────────────────────────────────────────────────
def test_legacy_prompt_is_scoped_and_carries_the_prompt_layer():
    prompt = pb.build_sql_prompt("total disbursement by district", [LEGACY],
                                 {"resolved": {}, "notes": [], "display": {}})
    assert "CM ELEVATE LEGACY RULES" in prompt and "COMMON MISTAKES" in prompt
    assert "Plan:" in prompt
    assert "CM ELEVATE RULES (" not in prompt and "FOCUS LEGACY RULES" not in prompt


def test_other_schemes_prompts_carry_no_legacy_text():
    for scheme in ("MGNREGA", "CM Elevate", "Focus Legacy"):
        prompt = pb.build_sql_prompt("total", [scheme], {"resolved": {}, "notes": [], "display": {}})
        assert "CM ELEVATE LEGACY" not in prompt and "COMMON MISTAKES" not in prompt


def test_legacy_tables_are_not_filed_under_cm_elevate():
    assert pb._scheme_of("v_cm_elevate_disbursement") == LEGACY
    assert pb._scheme_of("dim_cm_elevate_disb_scheme") == LEGACY
    assert pb._scheme_of("v_cm_elevate") == "CM Elevate"
    assert schema_introspect._table_matches_scheme("v_cm_elevate_disbursement", [LEGACY])
    assert not schema_introspect._table_matches_scheme("v_cm_elevate_disbursement", ["CM Elevate"])


def test_retrieval_ignores_the_scheme_name_and_caps_refusals():
    ids = _ids("What is the total amount disbursed under CM Elevate Legacy by district?")
    assert ids[0] == "M10"
    assert sum(i.startswith("X") for i in ids) <= 1
    assert _ids("kitne records hai piggery me")[0] == "C02"      # via a variant
    assert "X09" in _ids("who received the largest disbursement")


def test_other_schemes_keep_the_neutral_refusal_policy():
    for scheme in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy"):
        assert annotations._refusal_policy[scheme] == (1.0, None)
        assert not annotations._common_mistakes[scheme]


# ── 6. Caveats routed by code ────────────────────────────────────────────────
def test_caveat_notes_follow_the_sql():
    sql = ("SELECT COALESCE(financial_year_short, '(no financial year)') AS financial_year, "
           "ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
           "FROM curated.v_cm_elevate_disbursement GROUP BY financial_year_short")
    notes = " ".join(p._cm_legacy_answer_notes(sql, [{"financial_year": "2024-25",
                                                      "total_disbursed_cr": 1.0}]))
    assert "crore" in notes and "subsidy and loan" in notes and "Sericulture" in notes
    assert not p._cm_legacy_answer_notes("SELECT COUNT(*) AS records FROM x", [{"records": 1}])


# ── 7. Registries ────────────────────────────────────────────────────────────
def test_registered_everywhere_a_scheme_must_be():
    assert LEGACY in schema_context.SCHEME_CATALOG and LEGACY in schema_context.SCHEME_METRICS
    assert p._SCHEME_DATA_YEARS[LEGACY] == ["2024-25", "2025-26"]
    assert all(LEGACY in r["schemes"] for r in auth.ROLE_PERMISSIONS.values())
    assert edge._named_scheme("can I get CM Elevate Legacy data?") == LEGACY
    assert edge._named_scheme("can I get CM Elevate data?") == "CM Elevate"
    opts = followups.build_followups("data", "CM Elevate Legacy disbursement", [LEGACY], {})
    assert opts and all("CM Elevate Legacy" in o["question"] or "Break that down" in o["question"]
                        for o in opts)
    assert "curated.v_cm_elevate_disbursement" in p._CROSS_SCHEME_MONEY_SQL


# ── 8. Knowledge (RAG): one knowledge base for both CM Elevate datasets ─────
# CM Elevate and CM Elevate Legacy are two DATASETS of one programme; its
# eligibility / benefits / process docs are the same for both.
def test_legacy_reads_the_cm_elevate_knowledge_base_and_others_are_unchanged():
    from app import rag
    assert rag.kb_scheme(LEGACY) == "CM Elevate"
    for s in ("MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy", None):
        assert rag.kb_scheme(s) == s


def test_retrieval_filters_on_the_shared_tag(monkeypatch):
    import asyncio
    from app import rag
    seen = []

    async def fake_embed(q):
        return [[0.0]]

    async def fake_search(vec, top_k, scheme=None):
        seen.append(scheme)
        return []

    monkeypatch.setattr(rag.llm, "call_embedding", fake_embed)
    monkeypatch.setattr(rag.vectorstore, "search", fake_search)
    asyncio.run(rag.retrieve("who is eligible", scheme=LEGACY))
    asyncio.run(rag.retrieve("who is eligible", scheme="Focus Legacy"))
    assert seen == ["CM Elevate", "Focus Legacy"]


def test_naming_both_retrieves_the_shared_docs_once(monkeypatch):
    import asyncio
    from app import rag
    calls = []

    async def fake_retrieve(q, scheme=None):
        calls.append(scheme)
        return [{"text": f"{scheme} passage", "doc": "d", "heading": "h", "score": 0.9}]

    async def fake_compose(prompt):
        return "- **CM Elevate** — answer"

    monkeypatch.setattr(rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(rag.llm, "call_response_composer", fake_compose)
    out = asyncio.run(rag.answer_from_kb_multi("how to apply", ["CM Elevate", LEGACY, "MGNREGA"]))
    assert calls == ["CM Elevate", "MGNREGA"]          # shared KB searched once
    assert out and "I don't have reference material" not in out["answer"]


def test_knowledge_question_is_not_shown_a_legacy_rewrite():
    q = "how much subsidy does CM Elevate provide"
    pinned = p._pin_cm_elevate_dataset(q)
    assert pinned != q                                    # the DATA-side decision
    assert p._unpin_cm_elevate(pinned) == q               # undone on the KNOWLEDGE route


def test_legacy_tagged_docs_fold_into_the_shared_tag():
    from app import kb_ingest
    for tag in ("cm elevate legacy", "cmelevatelegacy", "cm elevate disbursement"):
        assert kb_ingest._CANONICAL_SCHEME[tag] == "CM Elevate"


def test_not_covered_reply_names_the_material_actually_searched():
    assert "CM Elevate reference material" in p._knowledge_not_covered_answer("x", LEGACY)
    assert "Focus Legacy reference material" in p._knowledge_not_covered_answer("x", "Focus Legacy")


# ── 9. "by scheme" inside CM Elevate Legacy means its own 13 sub-schemes ─────
@pytest.mark.parametrize("question", [
    "How many CM Elevate Legacy records are there, by scheme in Meghalaya?",
    "CM Elevate Legacy disbursement scheme-wise",
])
def test_by_scheme_stays_inside_legacy_and_offers_only_its_two_years(question):
    assert p._shortcut_scheme(question) == [LEGACY]
    labels = [o["label"] for o in p._year_clarification(question, [LEGACY]).options]
    assert labels == ["FY 2024-25", "FY 2025-26", "All financial years combined"]


def test_by_scheme_without_legacy_is_unchanged():
    assert p._shortcut_scheme("total disbursement across all schemes") == \
        list(schema_context.SCHEME_CATALOG)


# ── 10. "What schemes do you have?" — one programme, two datasets ───────────
def test_scheme_listing_shows_cm_elevate_once_and_counts_correctly():
    ans = p._scheme_listing_answer("what schemes do you have?")["answer"]
    bullets = [line for line in ans.splitlines() if line.startswith("- **")]
    assert ans.startswith(f"I cover {p._count_word(len(bullets))} ")
    assert len(bullets) == 5
    assert not any(b.startswith("- **CM Elevate Legacy**") for b in bullets)
    cm = next(b for b in bullets if b.startswith("- **CM Elevate**"))
    assert "CM Elevate Legacy" in cm and "applications" in cm
    # every other scheme's line is exactly its summary
    for name in ("MGNREGA", "PMAY-G", "Focus Plus", "Focus Legacy"):
        assert f"- **{name}** — {p._SCHEME_USER_SUMMARY[name]}" in bullets


# ── 2026-09-25 use-case QA fixes ─────────────────────────────────────────────
@pytest.mark.parametrize("question", [
    "What percentage of applications have been sanctioned in CM Elevate Legacy?",
    "How many applications have not been sanctioned in CM Elevate Legacy?",
    "How many CM Elevate Legacy applications are mapped to Mairang constituency?",
    "What is the total disbursement amount for Mairang constituency in CM Elevate Legacy?",
])
def test_answerable_questions_are_no_longer_refused(question):
    assert p._cm_legacy_not_held(question) is None


def test_constituency_is_available_for_cm_elevate_legacy():
    q = "What is the total disbursement amount for Mairang constituency?"
    assert p._ac_dimension_available(q, [LEGACY]) is True


def test_unresolved_filter_is_dropped_from_a_district_total():
    sql = ("SELECT lgd_district, ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
           "FROM curated.v_cm_elevate_disbursement WHERE entity_type <> 'Unresolved' "
           "GROUP BY lgd_district")
    out = p._cm_legacy_keep_unresolved_off_village(
        "What is the total disbursement amount for each district?", [LEGACY], sql)
    assert "Unresolved" not in out and "GROUP BY lgd_district" in out


def test_unresolved_filter_is_kept_on_a_village_answer():
    sql = ("SELECT lgd_village_name, village_code, COUNT(*) AS records FROM "
           "curated.v_cm_elevate_disbursement WHERE lgd_block = 'TIKRIKILLA' AND "
           "entity_type <> 'Unresolved' GROUP BY lgd_village_name, village_code")
    assert p._cm_legacy_keep_unresolved_off_village("records per village", [LEGACY], sql) == sql


@pytest.mark.parametrize("question", [
    "Who are the intended beneficiaries of CM-ELEVATE?",
    "What is the target number of entrepreneurs under CM-ELEVATE?",
])
def test_programme_design_questions_route_to_knowledge(question):
    assert p._PROGRAMME_DESIGN_CUE.search(question)


@pytest.mark.parametrize("question", [
    "How many beneficiaries in West Garo Hills?",
    "who are the beneficiaries of piggery scheme in Umling",
    "what is the total number of beneficiaries",
])
def test_beneficiary_counts_stay_off_the_design_cue(question):
    assert not p._PROGRAMME_DESIGN_CUE.search(question)


def test_sanctioned_shots_count_sanctioned_amount_not_rows():
    by_id = {e["id"]: e for e in _BANK["sql_generation_examples"]}
    for sid in ("C06", "C07", "C08", "G08"):
        assert "COUNT(sanctioned_amount)" in by_id[sid]["sql"], sid


def test_shared_geo_columns_are_qualified_on_the_dim_geography_join():
    sql = ("SELECT g.ac_name, lgd_district, COUNT(*) AS records "
           "FROM curated.v_cm_elevate_disbursement v "
           "JOIN curated.dim_geography g ON g.geography_key = v.geography_key "
           "WHERE UPPER(g.ac_name) = UPPER('Mairang') GROUP BY g.ac_name, lgd_district")
    out = p._cm_legacy_qualify_shared_geo_cols([LEGACY], sql)
    assert "v.lgd_district" in out and " lgd_district," not in out
    assert p._cm_legacy_qualify_shared_geo_cols(["Focus Legacy"], sql) == sql


def test_output_alias_is_not_qualified():
    sql = ("SELECT v.lgd_district AS lgd_district FROM curated.v_cm_elevate_disbursement v "
           "JOIN curated.dim_geography g ON g.geography_key = v.geography_key")
    assert "AS lgd_district" in p._cm_legacy_qualify_shared_geo_cols([LEGACY], sql)


def test_multi_row_fallback_is_readable_and_says_what_it_cut():
    rows = [{"lgd_district": f"D{i}", "records": i, "total_disbursed_cr": 1.5} for i in range(20)]
    out = p._deterministic_answer(rows)
    assert out.startswith("Here are the 20 results:")
    assert "- D0 — records: 0, total disbursed (₹ crore): 1.50" in out
    assert "…and 5 more rows in the table." in out


@pytest.mark.parametrize("question", [
    "What was the total disbursement in each financial year in CM Elevate Legacy?",
    "How many applications are recorded for each financial year in CM Elevate Legacy?",
    "What is the total disbursement made through each loan entity in CM Elevate Legacy?",
])
def test_per_group_questions_do_not_pause_for_scope(question):
    assert not p._needs_scope_clarification(question, {})


def test_a_bare_total_still_pauses_for_scope():
    assert p._needs_scope_clarification("How many total applications are recorded under CM Elevate Legacy?", {})


@pytest.mark.parametrize("question,expected", [
    ("applications mapped to Mairang constituency", None),
    ("applications in Mairang", None),
    ("records in EWKH", "EASTERN WEST KHASI HILLS"),
    ("records in Eastern West Khasi Hills district", "EASTERN WEST KHASI HILLS"),
    ("total in South West Garo Hills", "SOUTH WEST GARO HILLS"),
])
def test_hq_town_alias_does_not_scan_as_its_district(question, expected):
    r = entity_resolver.scan_dimension(question, LEGACY, "district")
    assert (r.canonical.upper() if r else None) == expected


_DISTRICT_SQL = (
    "SELECT lgd_district, COUNT(*) AS records,\n"
    "       ROUND(SUM(sanctioned_amount) / 1e7, 2) AS sanctioned_cr,\n"
    "       ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr\n"
    "FROM curated.v_cm_elevate_disbursement\n"
    "WHERE financial_year = '2024-25'\n"
    "GROUP BY lgd_district\nORDER BY total_disbursed_cr DESC\nLIMIT 100")
_TWO_ROWS = [{"lgd_district": "A", "total_disbursed_cr": 1.0},
             {"lgd_district": "B", "total_disbursed_cr": 2.0}]


def test_exact_total_is_requeried_without_the_group_by(monkeypatch):
    import asyncio
    sent = []

    async def fake_run(sql):
        sent.append(sql)
        return [{"sanctioned_cr": 142.74, "total_disbursed_cr": 82.9}]
    monkeypatch.setattr(p, "run_readonly", fake_run)
    notes = asyncio.run(p._cm_legacy_exact_totals(_DISTRICT_SQL, _TWO_ROWS))
    assert sent == ["SELECT ROUND(SUM(sanctioned_amount) / 1e7, 2) AS sanctioned_cr, "
                    "ROUND(SUM(total_disbursement) / 1e7, 2) AS total_disbursed_cr "
                    "FROM curated.v_cm_elevate_disbursement\nWHERE financial_year = '2024-25'"]
    assert "total_disbursed_cr = 82.90" in notes[0]


@pytest.mark.parametrize("sql,rows", [
    (_DISTRICT_SQL.replace("GROUP BY lgd_district", "GROUP BY ROLLUP(lgd_district)"), _TWO_ROWS),
    (_DISTRICT_SQL, _TWO_ROWS[:1]),
    (_DISTRICT_SQL, _TWO_ROWS + [{"lgd_district": "ALL DISTRICTS", "total_disbursed_cr": 3.0}]),
    ("SELECT COUNT(*) AS records FROM curated.v_cm_elevate_disbursement GROUP BY lgd_district", _TWO_ROWS),
])
def test_exact_total_skipped_when_not_needed(monkeypatch, sql, rows):
    import asyncio

    async def boom(sql):
        raise AssertionError("should not query")
    monkeypatch.setattr(p, "run_readonly", boom)
    assert asyncio.run(p._cm_legacy_exact_totals(sql, rows)) == []


def test_zero_crore_value_is_described_as_under_one_lakh():
    notes = p._cm_legacy_small_money_notes([{"scheme_name": "Spinning", "total_disbursed_cr": 0.0}])
    assert "under ₹0.01 crore" in notes[0]
    assert p._cm_legacy_small_money_notes([{"scheme_name": "X", "total_disbursed_cr": 0.5}]) == []
