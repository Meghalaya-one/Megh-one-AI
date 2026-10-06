"""
NRLM onboarding regression tests (scheme 7, onboarded 2026-10-06).

Every test calls the REAL function or reads the REAL registry — never a
re-implementation — per CLAUDE.md §7. Pure Python: no model, no DB, no KB.

    python -m pytest tests/test_nrlm_onboarding.py -q

The tests are grouped by the risk each one protects:

  1. registry completeness   — all 14 touchpoints actually carry NRLM
  2. routing                 — NRLM vocabulary reaches NRLM, and nothing else
                               is dragged to it (especially the SHG word, which
                               CM Elevate also uses as an applicant category)
  3. the money/year trap     — the scheme's single highest-risk wrong number:
                               RF and CIF are cumulative with no release date,
                               so nothing may offer or imply money-by-year
  4. the grain trap          — one row is one SHG, never one person
  5. no regression           — the six existing schemes are unchanged
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import (annotations, auth, edge, entity_resolver, followups,  # noqa: E402
                 kb_ingest, pipeline as p, prompt_builder, rag, schema_context,
                 schema_introspect)
from app.config import settings  # noqa: E402

SIX_OLD = ["MGNREGA", "PMAY-G", "Focus Plus", "CM Elevate", "Focus Legacy",
           "CM Elevate Legacy"]


# ───────────────────────── 1. registry completeness ─────────────────────────
def test_every_scheme_registry_carries_nrlm():
    """The 14 registries SCHEMES.md enumerates. Missing one leaves the scheme
    half-wired, which is why this is a single assert-everything test."""
    assert "NRLM" in annotations._SCHEME_DIRS
    assert annotations._FEW_SHOT_FILE["NRLM"] == "nrlm_few_shot.yaml"
    assert annotations._FK_FILE["NRLM"] == "nrlm_foreign_key_augmentation.yaml"
    assert "NRLM" in entity_resolver._RESOLVER_FILE
    assert "NRLM" in schema_context.SCHEME_CATALOG
    assert "NRLM" in schema_context.SCHEME_METRICS
    assert "NRLM" in schema_context._SCHEME_BLOCKS
    assert "NRLM" in p._SCHEME_NAME_PATTERN
    assert "NRLM" in p._SCHEME_FUZZY_ALIASES
    assert p._SCHEME_CANONICAL_SPELLING["NRLM"] == "NRLM"
    assert "NRLM" in p._SCHEME_DATA_YEARS
    assert "NRLM" in p._SCHEME_FIT
    assert "NRLM" in p._AC_CAPABLE_SCHEMES
    assert "NRLM" in followups._SCHEME_RX
    assert "NRLM" in followups._KNOWLEDGE_LADDER
    assert "NRLM" in edge._SCHEME_ALIASES
    assert "NRLM" in edge._SCHEME_CAPABILITY
    assert "NRLM" in edge._SCHEME_STARTERS
    assert "NRLM" in rag._SCHEME_DOC_NAMES
    assert "NRLM" in schema_introspect._SUBJECT_TO_SCHEME.values()
    assert "NRLM" in schema_introspect._TABLE_NAME_TO_SCHEME.values()


def test_the_scheme_files_all_exist_on_disk():
    folder = annotations._SCHEME_DIRS["NRLM"]
    assert folder.is_dir(), folder
    for name in ("nrlm_schema_partitions.yaml", "nrlm_classification_rules.yaml",
                 "nrlm_default_rules.yaml", "nrlm_entity_resolver.yaml",
                 "nrlm_few_shot.yaml", "nrlm_foreign_key_augmentation.yaml",
                 "nrlm_response_template.yaml", "README.md"):
        assert (folder / name).is_file(), name


def test_both_kb_docs_are_registered_and_name_the_scheme():
    """The composer answers "not covered" when the scheme LABEL is absent from
    its own reference docs, so the label presence is part of the contract."""
    nrlm_docs = [f for f, s in kb_ingest._SOURCES if s == "NRLM"]
    assert len(nrlm_docs) == 2, nrlm_docs
    root = Path(__file__).resolve().parents[1] / "data"
    for rel in nrlm_docs:
        path = root / rel
        assert path.is_file(), path
        assert "NRLM" in path.read_text(encoding="utf-8")


def test_nrlm_has_its_own_knowledge_base_not_a_shared_one():
    """Unlike CM Elevate Legacy (which shares CM Elevate's docs), NRLM is its
    own programme and must keep its own KB tag."""
    assert rag.kb_scheme("NRLM") == "NRLM"
    assert "NRLM" not in rag._KB_SCHEME_ALIAS


def test_every_role_can_see_nrlm():
    for role, perms in auth.ROLE_PERMISSIONS.items():
        assert "NRLM" in perms["schemes"], role


def test_asr_prompt_names_the_scheme_and_its_vocabulary():
    assert "NRLM" in settings.ASR_PROMPT
    assert "SHG" in settings.ASR_PROMPT


def test_the_resolver_loads_nrlms_own_dimension_names():
    """nrlm_entity_resolver.yaml calls them `constituency` and `formation_year`;
    the module reads `assembly_constituency` and `year`. Without the alias the
    55 constituencies resolve to nothing and an AC question silently falls back
    to the BLOCK of the same name, which covers a different area."""
    entity_resolver.load_all()
    cat = entity_resolver._catalog["NRLM"]
    assert len(cat["district"]) == 12
    assert len(cat["block"]) == 56
    assert len(cat["assembly_constituency"]) == 55
    assert len(cat["year"]) == 30


def test_nrlm_reaches_constituencies_without_a_geography_join():
    """v_nrlm carries the constituency on the row, so unlike Focus Legacy and
    CM Elevate Legacy its AC query needs no dim_geography join."""
    sql = entity_resolver._AC_CONTENTS_SQL["NRLM"]
    assert "curated.v_nrlm" in sql
    assert "dim_geography" not in sql
    assert "constituency_name_raw" in sql
    assert "entity_type <> 'Unresolved'" in sql   # villages only, per the village rule
    assert "NRLM" in entity_resolver.AC_CONTENTS_SOURCE


# ───────────────────────────── 2. routing ───────────────────────────────────
@pytest.mark.parametrize("q", [
    "How many SHGs are there in East Khasi Hills?",
    "How many self help groups are active?",
    "What is the total Revolving Fund received by SHGs?",
    "total CIF by district",
    "How many SHGs were formed in 2020-21?",
    "SHGs in Mawkyrwat constituency",
    "how many shg members are there",
    "community investment fund in Ri Bhoi",
    "how many revived SHGs are active?",
])
def test_nrlm_vocabulary_routes_to_nrlm(q):
    assert p._infer_scheme_from_terms(q) == ["NRLM"]


@pytest.mark.parametrize("q", [
    "How many NRLM shgs are there?",
    "aajeevika groups in Ri Bhoi",
    "DAY-NRLM self help groups",
    "msrls shg register",
    "How many pre-NRLM SHGs are there?",
])
def test_the_scheme_name_is_recognised(q):
    assert "NRLM" in p._named_schemes(q)


@pytest.mark.parametrize("q,expected", [
    ("How many PMAY-G houses were sanctioned?", "PMAY-G"),
    ("total person-days in 2024-25", "MGNREGA"),
    ("How many CM Elevate applications were received?", "CM Elevate"),
    ("producer groups paid under Focus Legacy", "Focus Legacy"),
])
def test_the_other_schemes_are_not_dragged_to_nrlm(q, expected):
    assert p._infer_scheme_from_terms(q) == [expected]


@pytest.mark.parametrize("q", [
    "How many CM Elevate applications came from SHGs?",
    "CM Elevate applications by applicant category SHG",
    "Focus Plus disbursements to SHG members",
])
def test_the_shg_word_does_not_hijack_another_schemes_question(q):
    """"SHG" is NRLM's unit but also a CM Elevate applicant category and a Focus
    Plus member description. A question that NAMES its scheme must keep it."""
    named = p._named_schemes(q)
    assert named and named != ["NRLM"]
    assert p._infer_scheme_from_terms(q) != ["NRLM"]


def test_an_shg_question_is_not_bounced_as_off_topic():
    """Without NRLM's words in _DOMAIN_WORDS the edge layer rejects this before
    routing ever runs."""
    for q in ("how many shgs are there?", "what is nrlm?",
              "total revolving fund by district", "self help groups formed in 2020-21"):
        assert edge.has_domain_vocabulary(q), q
    assert not edge.has_domain_vocabulary("tell me a joke")


def test_an_nrlm_data_question_passes_through_the_edge_layer():
    assert edge.detect_edge_case("how many shgs are there in East Khasi Hills?") is None


def test_the_scheme_ask_offers_nrlm():
    opts = [o["label"].split(" (")[0]
            for o in p._scheme_clarification("total disbursed in 2024").options]
    assert "NRLM" in opts


def test_an_assembly_constituency_question_can_reach_nrlm():
    opts = [o["label"].split(" (")[0] for o in p._scheme_clarification(
        "What is the total amount disbursed for Baghmara assembly constituency?").options]
    assert "NRLM" in opts


def test_the_tables_belong_to_nrlm():
    assert prompt_builder._scheme_of("v_nrlm") == "NRLM"
    assert prompt_builder._scheme_of("fact_nrlm_shg") == "NRLM"


# ──────────────────── 3. the money / year trap (highest risk) ───────────────
def test_the_sql_prompt_forbids_putting_money_in_a_year():
    """RF and CIF are cumulative per SHG with no release date. Filtering them by
    the FORMATION year returns a different figure that looks exactly like the
    one asked for, so the rule has to be in the prompt deterministically."""
    rules = schema_context._NRLM_RULES
    assert "CUMULATIVE AND UNDATED" in rules
    assert "NEVER compute a year-on-year difference or growth rate of money" in rules
    assert "_held_cr" in rules                      # the one allowed cohort form
    for phrase in ("no reporting year", "NO release date"):
        assert phrase.lower() in (schema_context._NRLM_RULES
                                  + schema_context._NRLM_TABLES
                                  + schema_context.SCHEME_CATALOG["NRLM"]).lower(), phrase


def test_the_catalog_and_metrics_say_the_funds_have_no_year():
    cat = schema_context.SCHEME_CATALOG["NRLM"]
    assert "CUMULATIVE" in cat.upper() and "NEVER" in cat.upper()
    metrics = " | ".join(schema_context.SCHEME_METRICS["NRLM"]).lower()
    assert "cumulative per shg" in metrics
    assert "no fund release date" in metrics        # so no trend / utilisation


def test_no_followup_offers_money_in_a_year():
    """A follow-up chip is a question the user will actually send, so offering
    "RF in FY 2021-22" would advertise the one thing the scheme must refuse."""
    ents = {"district": "EAST KHASI HILLS", "year": "2021-22"}
    for q in ("What is the total Revolving Fund received by SHGs?",
              "total CIF by district", "How many SHGs are there?"):
        for o in followups.build_followups("data", q, ["NRLM"], ents):
            text = o["question"].lower()
            money = any(w in text for w in ("revolving", "cif", "fund", "amount"))
            if money:
                assert "2021-22" not in text and " fy" not in text, o["question"]
                assert "trend" not in text and "growth" not in text, o["question"]


def test_no_starter_or_card_chip_offers_money_in_a_year():
    for chip in edge._SCHEME_STARTERS["NRLM"]:
        low = chip.lower()
        if any(w in low for w in ("revolving", "cif", "fund")):
            assert "financial year" not in low and "fy " not in low, chip
            assert "trend" not in low and "growth" not in low, chip


def test_the_cross_scheme_money_row_is_labelled_as_a_stock():
    """Every other scheme's figure is money moved in a period; NRLM's is money
    held to date. The ranking answer must not read as "NRLM spent this much"."""
    assert "curated.v_nrlm" in p._CROSS_SCHEME_MONEY_SQL
    assert "revolving_fund_amount + cif_amount" in p._CROSS_SCHEME_MONEY_SQL
    assert "cumulative with no year" in p._CROSS_SCHEME_MONEY_SQL
    assert "cumulative" in p._MEASURE_PLAIN["NRLM"].lower()
    assert p._SCHEME_DISPLAY_NAME["NRLM"] == "NRLM"


def test_the_formation_year_span_is_complete_so_the_guard_cannot_misfire():
    """_SCHEME_DATA_YEARS feeds BOTH the year chips and the out-of-range guard.
    A truncated list would make the guard refuse "SHGs formed in 2008-09", a
    real question with a real answer."""
    years = p._SCHEME_DATA_YEARS["NRLM"]
    assert years[0] == "1984-85" and years[-1] == "2022-23"
    for y in ("2008-09", "2011-12", "2014-15", "2022-23"):
        assert y in years, y


# ───────────────────────── 4. the grain trap ────────────────────────────────
def test_the_sql_prompt_says_count_is_groups_never_people():
    rules = schema_context._NRLM_RULES
    assert "One row = one SHG" in rules
    assert "SUM(total_members)" in rules
    vocab = schema_context._NRLM_VOCAB
    assert "NEVER COUNT(*)" in vocab              # beside the members entry
    assert "COUNT(DISTINCT shg_name)" in rules    # the ~32% undercount trap


def test_the_village_and_unresolved_rules_are_present():
    rules = schema_context._NRLM_RULES
    assert "entity_type <> 'Unresolved'" in rules
    assert "COUNT(DISTINCT village_code)" in rules
    assert "38,597" in rules                      # constituency total != state total


def test_the_not_held_list_forbids_calling_the_funds_savings_or_loans():
    text = schema_context._NRLM_RULES + " ".join(schema_context.SCHEME_METRICS["NRLM"])
    assert "savings" in text.lower() and "loans" in text.lower()
    assert "never" in text.lower()


# ───────────────────────── 5. no regression ─────────────────────────────────
def test_the_nrlm_prompt_block_is_scoped_to_nrlm_only():
    """An NRLM question must get the shared blocks plus NRLM's own, and no other
    scheme's RULES block. (The preamble names curated.v_pmay as a generic
    schema-qualification example for every scheme, so the check is on the
    section HEADERS, not on a bare table name appearing anywhere.)"""
    ctx = schema_context.build_schema_context(["NRLM"])
    assert "curated.v_nrlm" in ctx
    assert "NRLM RULES" in ctx and "NRLM BUSINESS VOCABULARY" in ctx
    for other in ("PMAY-G RULES", "MGNREGA RULES", "FOCUS PLUS RULES",
                  "CM ELEVATE RULES", "CM ELEVATE LEGACY RULES", "FOCUS LEGACY RULES"):
        assert other not in ctx.upper(), other


@pytest.mark.parametrize("scheme", SIX_OLD)
def test_the_existing_schemes_do_not_gain_nrlm_text(scheme):
    assert "v_nrlm" not in schema_context.build_schema_context([scheme])


def test_cm_elevate_legacy_still_collapses_into_one_programme():
    """NRLM must be a separate pickable programme without disturbing the
    CM Elevate pair, which deliberately shares one knowledge base."""
    pick = p._pickable_schemes()
    assert "NRLM" in pick
    assert "CM Elevate Legacy" not in pick
