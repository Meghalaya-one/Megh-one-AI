"""
A SUPPORTED scheme must never be refused as "not one of the schemes I cover".

User report 2026-10-07 (screenshot): "List all SHGs registered under in Chokpot for
NRLM" was answered with *"NRLM isn't one of the schemes I cover…"*, and the reply
offered an **NRLM (Self Help Groups)** chip. Tapping it re-asked the same question
with "NRLM" still in it, which was refused again — an endless loop, with the real
answer (517 SHGs in Chokpot block) sitting in the database the whole time.

Cause: `pipeline._UNSUPPORTED_SCHEME` still listed `nrlm|day-nrlm|aajeevika|
ajeevika|livelihoods mission` from before NRLM was onboarded as the seventh scheme
on 2026-10-06. The chip could never escape the pattern that produced it.

The first test is structural: it walks `SCHEME_CATALOG` and every alias in
`_SCHEME_NAME_PATTERN`, so onboarding scheme eight and forgetting to clear its old
entry fails here instead of in front of an officer.

    python -m pytest tests/test_supported_scheme_not_refused.py -q
"""
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import edge, pipeline as p  # noqa: E402
from app.schema_context import SCHEME_CATALOG  # noqa: E402

# One natural phrasing per supported scheme, each naming the scheme outright.
NAMED_QUESTIONS = {
    "MGNREGA": "total person-days under MGNREGA in Chokpot",
    "PMAY-G": "houses completed under PMAY-G in Chokpot",
    "Focus Plus": "Focus Plus disbursements in Chokpot",
    "CM Elevate": "CM Elevate applications in Chokpot",
    "Focus Legacy": "Focus Legacy producer group payments in Chokpot",
    "CM Elevate Legacy": "CM Elevate Legacy sanctions in Chokpot",
    "NRLM": "List all SHGs registered under in Chokpot for NRLM",
}


def test_every_supported_scheme_has_a_probe():
    """If a scheme is added, this file must grow with it."""
    assert set(NAMED_QUESTIONS) == set(SCHEME_CATALOG)


@pytest.mark.parametrize("scheme", sorted(SCHEME_CATALOG))
def test_a_supported_scheme_is_never_called_unsupported(scheme):
    assert p._unsupported_scheme_named(NAMED_QUESTIONS[scheme]) is None


def test_no_supported_scheme_alias_matches_the_unsupported_pattern():
    """The structural guard: no alias of a loaded scheme may sit in the
    unsupported list. This is what would have caught the NRLM regression at
    onboarding time instead of in production."""
    offenders = []
    for scheme, pattern in p._SCHEME_NAME_PATTERN.items():
        # Each alternative in the scheme's own name pattern, as plain text where
        # it is plain text (skip the ones carrying regex syntax).
        for alt in pattern.pattern.split("|"):
            alt = alt.replace(r"\b", "").replace("?", "").strip()
            if not alt or re.search(r"[(\[\\^$*+{}]", alt):
                continue
            if p._UNSUPPORTED_SCHEME.search(alt):
                offenders.append((scheme, alt))
    assert not offenders, (
        "these aliases of SUPPORTED schemes are still listed in "
        "_UNSUPPORTED_SCHEME, so the bot will refuse its own scheme: %r" % offenders)


@pytest.mark.parametrize("q", [
    "List all SHGs registered under in Chokpot for NRLM",
    "How many SHGs under aajeevika in Chokpot",
    "day-nrlm SHGs in Ri Bhoi",
    "national rural livelihoods mission groups in Meghalaya",
    "msrls SHG count",
    "DAY-NRLM revolving fund by district",
])
def test_nrlm_phrasings_reach_the_pipeline(q):
    """Every NRLM alias that used to be refused."""
    assert p._unsupported_scheme_named(q) is None


@pytest.mark.parametrize("q,expected", [
    ("How many beneficiaries under PM-KISAN", "PM-KISAN"),
    ("ujjwala connections in Ri Bhoi", "ujjwala"),
    ("jal jeevan mission coverage", "jal jeevan mission"),
    ("PMAY-U houses sanctioned", "PMAY-U"),
    ("old age pension beneficiaries", "old age pension"),
])
def test_a_genuinely_unsupported_scheme_is_still_refused(q, expected):
    """The refusal must keep working for schemes we really do not hold."""
    assert (p._unsupported_scheme_named(q) or "").lower() == expected.lower()


def test_the_refusal_chip_cannot_loop():
    """Each chip the refusal offers must resume into a question that is NOT
    refused again — the exact failure the user hit."""
    cn = p._unsupported_scheme_clarification(
        "How many beneficiaries under PM-KISAN in Chokpot", "PM-KISAN")
    assert cn.rule == "scheme-not-available"
    assert cn.options, "the refusal must offer a way forward"
    for opt in cn.options:
        resumed = opt["question"]
        assert p._unsupported_scheme_named(resumed) is None, (
            "chip %r resumes into %r, which is refused again -> loop"
            % (opt["label"], resumed))


def test_the_refusal_names_every_supported_scheme():
    """The prose listed only six schemes while offering a seventh chip, which is
    how the contradiction reached the user in the first place."""
    cn = p._unsupported_scheme_clarification("ujjwala connections", "ujjwala")
    for scheme in SCHEME_CATALOG:
        assert scheme in cn.question, "%s missing from the refusal text" % scheme
    labels = " ".join(o["label"] for o in cn.options)
    for scheme in SCHEME_CATALOG:
        assert scheme in labels, "%s missing from the refusal chips" % scheme


@pytest.mark.parametrize("key", ["greeting", "identity"])
def test_the_edge_replies_name_every_supported_scheme(key):
    """The greeting and the "what can you do" reply are the other places a user
    is told which schemes exist; both still said six."""
    text = edge._RESPONSES[key]
    for scheme in SCHEME_CATALOG:
        assert scheme in text, "%s missing from the %s reply" % (scheme, key)


def test_the_out_of_scope_reply_names_every_supported_scheme():
    for scheme in SCHEME_CATALOG:
        assert scheme in edge._OUT_OF_SCOPE_REPLY, (
            "%s missing from the out-of-scope reply" % scheme)
