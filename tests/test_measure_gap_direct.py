"""A direct question that names ONE scheme and asks it for a measure only
another scheme records ("total person days in Meghalaya under CM Elevate",
UI report 2026-10-10) must say which scheme holds the measure and offer it,
before any model call — never "couldn't build a working query".

Calls the real `pipeline._measure_gap_answer`, and the real `_answer_data`
with only the DB-backed village masking stubbed, so the wiring (before
`resolve_entities`, before the model) is what is tested.
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import entity_resolver, pipeline as p  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _catalogue():
    entity_resolver.load_all()


# ── The helper ───────────────────────────────────────────────────────────────

def test_reported_question_person_days_under_cm_elevate():
    q = "So what is the total person days in Meghalaya under CM Elevate?"
    gap = p._measure_gap_answer(q, ["CM Elevate"])
    assert gap is not None and gap.rule == "measure-unavailable"
    assert "CM Elevate doesn't record person-days" in gap.question
    assert "only MGNREGA does" in gap.question
    # first chip: the same question under the scheme that holds the measure,
    # in the user's own words, without the spoken lead-in
    first = gap.options[0]
    assert first["question"] == "What is the total person days in Meghalaya under MGNREGA?"
    assert "MGNREGA" in first["label"]
    # then CM Elevate's own measures, with no year (CM Elevate records none)
    labels = [o["label"] for o in gap.options[1:]]
    assert labels == [lab for lab, _ in p._SCHEME_HEADLINE_OFFERS["CM Elevate"]]
    assert all("FY" not in o["question"] for o in gap.options[1:])
    # "in Meghalaya" is carried as the statewide scope the area pause knows
    assert all(o["question"].endswith(" for all of Meghalaya?") for o in gap.options[1:])
    assert "for CM Elevate for all of Meghalaya" in gap.question


@pytest.mark.parametrize("question, scheme, label, owner", [
    ("How many job cards were issued under PMAY-G in 2023-24?", "PMAY-G", "job cards", "MGNREGA"),
    ("Total wages paid under Focus Plus in Mawkyrwat block?", "Focus Plus", "wage expenditure", "MGNREGA"),
    ("How many households were employed under CM Elevate Legacy in FY 2024-25?",
     "CM Elevate Legacy", "employment", "MGNREGA"),
    ("How many houses were completed under MGNREGA in West Garo Hills?", "MGNREGA", "houses", "PMAY-G"),
    ("Material expenditure under Focus Legacy", "Focus Legacy", "material expenditure", "MGNREGA"),
])
def test_each_scheme_specific_measure_names_its_owner(question, scheme, label, owner):
    gap = p._measure_gap_answer(question, [scheme])
    assert gap is not None and gap.rule == "measure-unavailable"
    assert f"{scheme} doesn't record {label}" in gap.question
    assert f"only {owner} does" in gap.question
    assert gap.options[0]["question"].count(owner) == 1
    assert scheme not in gap.options[0]["question"]


def test_scope_is_carried_onto_the_chips_from_the_question_words():
    q = "How many job cards were issued under PMAY-G in East Khasi Hills in 2023-24?"
    gap = p._measure_gap_answer(q, ["PMAY-G"])
    assert "for PMAY-G in East Khasi Hills in FY 2023-24" in gap.question
    assert gap.options[0]["question"] == \
        "How many job cards were issued under MGNREGA in East Khasi Hills in 2023-24?"
    for opt in gap.options[1:]:
        assert opt["question"].endswith("in East Khasi Hills in FY 2023-24?")


def test_block_wins_over_district_and_acronym_is_understood():
    gap = p._measure_gap_answer("Total person-days under CM Elevate in EKH", ["CM Elevate"])
    assert "for CM Elevate in East Khasi Hills" in gap.question
    gap = p._measure_gap_answer("Total wages under Focus Plus in Mawkyrwat block", ["Focus Plus"])
    assert gap.options[1]["question"].endswith("in Mawkyrwat block?")


@pytest.mark.parametrize("question, schemes", [
    # the scheme that owns the measure: a normal data question
    ("Total person-days in Meghalaya under MGNREGA", ["MGNREGA"]),
    ("How many PMAY-G houses were sanctioned in 2023-24?", ["PMAY-G"]),
    # a fuzzy-spelled owner is still the owner
    ("Total person days under manrega", ["MGNREGA"]),
    # a comparison between schemes has its own path
    ("Compare person-days under MGNREGA and CM Elevate", ["MGNREGA", "CM Elevate"]),
    # money and counts exist in every scheme: left to the generator
    ("Total expenditure under CM Elevate", ["CM Elevate"]),
    ("How many CM Elevate applications were received?", ["CM Elevate"]),
    # CM Elevate's own self-employment vocabulary is not MGNREGA employment
    ("How many self-employment applications under CM Elevate?", ["CM Elevate"]),
    ("How many self employment units were set up under CM Elevate?", ["CM Elevate"]),
    # no scheme named: a classifier guess is never told "you asked for X"
    ("Total person-days in Meghalaya", ["CM Elevate"]),
    # the classifier returned several schemes
    ("Total person-days under CM Elevate", ["CM Elevate", "MGNREGA"]),
    ("Total person-days under CM Elevate", []),
])
def test_does_not_fire_outside_its_case(question, schemes):
    assert p._measure_gap_answer(question, schemes) is None


def test_fuzzy_named_target_falls_back_to_the_owner_form(monkeypatch):
    # the exact pattern cannot swap a misspelled name out of the user's words,
    # so the first chip is the owner's own form for the measure
    monkeypatch.setattr(p, "_named_schemes", lambda q: ["PMAY-G"])
    gap = p._measure_gap_answer("Total person days under pamay in 2023-24", ["PMAY-G"])
    assert gap is not None
    assert gap.options[0]["question"] == "Total MGNREGA person-days in FY 2023-24"


# ── The wiring: _answer_data pauses before resolve_entities / the model ─────

def test_answer_data_raises_the_pause_before_any_model_call(monkeypatch):
    async def _no_mask(q):
        return q

    async def _boom(*a, **k):
        raise AssertionError("resolve_entities must not be reached")

    monkeypatch.setattr(p, "_mask_scheme_words_in_village_name", _no_mask)
    monkeypatch.setattr(p, "resolve_entities", _boom)
    with pytest.raises(p.ClarificationNeeded) as ei:
        asyncio.run(p._answer_data("So what is the total person days in Meghalaya under CM Elevate?"))
    assert ei.value.rule == "measure-unavailable"
    assert ei.value.options[0]["question"] == \
        "What is the total person days in Meghalaya under MGNREGA?"


def test_router_remembers_the_pause_so_a_typed_pick_resumes_it():
    from app.routers import query as router
    from app.session_store import Session
    gap = p._measure_gap_answer("Total person days under CM Elevate", ["CM Elevate"])
    session = Session(session_id="t", user_id="u", created=0.0, last_seen=0.0)
    router.remember_pause(session, "Total person days under CM Elevate", gap)
    assert session.pending_scope_rule == "measure-unavailable"
    assert session.pending_scope_options == gap.options
    # a typed "MGNREGA" / "applications" lands on one chip (KI-181 contract)
    assert p._resume_option_pause("the first one", gap.options) == gap.options[0]["question"]
    assert p._resume_option_pause("applications", gap.options) == gap.options[1]["question"]
