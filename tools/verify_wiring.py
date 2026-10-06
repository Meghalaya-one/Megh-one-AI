#!/usr/bin/env python
"""Registry completeness checker for scheme onboarding.

Schemes are hand-registered in ~14 registries (CLAUDE.md §4, SCHEMES.md
§Adding a scheme). Missing any one leaves a scheme HALF-WIRED: it may route but
not resolve places, answer data questions but not knowledge questions, or stay
invisible to some roles. Nothing raises; the scheme is just quietly wrong on
that path. This script mechanises the grep that finds those gaps -- step 27 of
the onboarding procedure.

READ-ONLY. It imports the app modules and inspects the live registry objects,
so it reports what the code actually holds, not what a doc claims.

Usage
-----
    .venv/Scripts/python.exe tools/verify_wiring.py                  # all schemes
    .venv/Scripts/python.exe tools/verify_wiring.py "Focus Legacy"   # one scheme
    .venv/Scripts/python.exe tools/verify_wiring.py --json           # machine-readable

Run from the repo root, because .env is read relative to the working directory.
Needs no VPN: every check below is offline except --check-kb-chunks, which
queries Qdrant and is skipped unless asked for.

Exit codes: 0 = every scheme fully wired; 1 = at least one gap found.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# A gap is one of:
#   MISSING  - the scheme is absent from a registry it must be in
#   SILENT   - present, but in a shape that fails without an error (the worst kind)
#   WARN     - probably intentional, but worth a human look
MISSING, SILENT, WARN = "MISSING", "SILENT", "WARN"


class Report:
    """Collects findings per scheme, grouped by the layer that owns them."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, scheme: str, layer: str, registry: str, kind: str,
            detail: str, fix: str = "") -> None:
        self.rows.append({"scheme": scheme, "layer": layer, "registry": registry,
                          "kind": kind, "detail": detail, "fix": fix})

    def ok(self, scheme: str, layer: str, registry: str) -> None:
        self.rows.append({"scheme": scheme, "layer": layer, "registry": registry,
                          "kind": "OK", "detail": "", "fix": ""})

    def gaps(self, scheme: str | None = None) -> list[dict]:
        return [r for r in self.rows
                if r["kind"] != "OK" and (scheme is None or r["scheme"] == scheme)]


# ---------------------------------------------------------------------------
# The registry map. Each entry: (layer, module, symbol, how to test membership)
#
# `kind` says how to look for the scheme inside the object:
#   "key"    - a dict keyed by canonical name
#   "member" - a set/tuple/list of canonical names
#   "text"   - a string blob that must mention the name
#   "custom" - handled by its own function below
# ---------------------------------------------------------------------------
_REGISTRIES = [
    # Layer 2 - annotation loader
    ("2 annotations", "app.annotations", "_SCHEME_DIRS", "key"),
    ("2 annotations", "app.annotations", "_FEW_SHOT_FILE", "key"),
    ("2 annotations", "app.annotations", "_FK_FILE", "key"),
    # Layer 3 - hand-written schema context (what the 30B SQL model reads)
    ("3 schema_context", "app.schema_context", "SCHEME_CATALOG", "key"),
    ("3 schema_context", "app.schema_context", "SCHEME_METRICS", "key"),
    ("3 schema_context", "app.schema_context", "_SCHEME_BLOCKS", "key"),
    # Layer 5 - entity resolver
    ("5 entity_resolver", "app.entity_resolver", "_RESOLVER_FILE", "key"),
    # _ACTIVITY_VIEWS is a flat tuple of view names, not keyed by scheme --
    # checked against the scheme's query surface in check_activity_views().
    # Layer 6 - routing and gates
    ("6 pipeline", "app.pipeline", "_SCHEME_NAME_PATTERN", "key"),
    ("6 pipeline", "app.pipeline", "_SCHEME_DATA_YEARS", "key"),
    ("6 pipeline", "app.pipeline", "_SCHEME_USER_SUMMARY", "key"),
    ("6 pipeline", "app.pipeline", "_SCHEME_FIT", "key"),
    # Layer 7 - edge layer
    ("7 edge", "app.edge", "_SCHEME_ALIASES", "key"),
    ("7 edge", "app.edge", "_SCHEME_CAPABILITY", "key"),
    ("7 edge", "app.edge", "_SCHEME_STARTERS", "key"),
    # Layer 8 - follow-ups
    ("8 followups", "app.followups", "_SCHEME_RX", "key"),
    # Layer 12 - KB / RAG
    ("12 rag", "app.rag", "_SCHEME_DOC_NAMES", "key"),
]


def _load(module: str):
    import importlib
    return importlib.import_module(module)


def _contains(obj, scheme: str, kind: str) -> bool:
    if kind == "key":
        try:
            return scheme in obj
        except TypeError:
            return False
    if kind == "member":
        return scheme in tuple(obj)
    if kind == "text":
        return scheme in str(obj)
    return False


def check_flat_registries(schemes: list[str], rep: Report) -> None:
    """The mechanical half: is the canonical string present at all?"""
    for layer, module, symbol, kind in _REGISTRIES:
        try:
            obj = getattr(_load(module), symbol)
        except (ImportError, AttributeError) as exc:
            for s in schemes:
                rep.add(s, layer, symbol, MISSING,
                        f"could not read {module}.{symbol}: {exc}",
                        "the symbol may have been renamed; update this checker")
            continue
        for s in schemes:
            if _contains(obj, s, kind):
                rep.ok(s, layer, symbol)
            else:
                rep.add(s, layer, symbol, MISSING,
                        f"{s!r} absent from {module}.{symbol}",
                        f"add a {s!r} entry to {symbol}")


# ---------------------------------------------------------------------------
# The SILENT checks. These are the failures that produce no error at all --
# each one has actually shipped in this project.
# ---------------------------------------------------------------------------

def check_activity_views(schemes: list[str], rep: Report) -> None:
    """_ACTIVITY_VIEWS is a flat tuple of every scheme's primary data view. It
    breaks ties between duplicate dim_geography rows, so a scheme whose view is
    absent contributes nothing to the tie-break and its villages can resolve to
    another scheme's duplicate. The scheme's primary view is read out of its own
    schema_context block, so this needs no hardcoded map."""
    import app.entity_resolver as er
    import app.schema_context as sc

    views = tuple(getattr(er, "_ACTIVITY_VIEWS", ()))
    blocks = getattr(sc, "_SCHEME_BLOCKS", {})

    for s in schemes:
        block = blocks.get(s)
        if not block:
            continue  # already reported by the flat-registry check
        declared = set(re.findall(r"curated\.(v_[a-z0-9_]+)", str(block)))
        if not declared:
            rep.add(s, "5 entity_resolver", "_ACTIVITY_VIEWS", WARN,
                    f"no curated.v_* view found in {s}'s schema_context block",
                    "")
            continue
        listed = {v.split(".", 1)[-1] for v in views}
        if declared & listed:
            rep.ok(s, "5 entity_resolver", "_ACTIVITY_VIEWS")
        else:
            rep.add(s, "5 entity_resolver", "_ACTIVITY_VIEWS", SILENT,
                    f"none of {s}'s views ({', '.join(sorted(declared))}) is in "
                    "_ACTIVITY_VIEWS, so it never participates in the "
                    "duplicate-village tie-break",
                    "add the scheme's primary curated view to _ACTIVITY_VIEWS")


def check_refresh_years(schemes: list[str], rep: Report) -> None:
    """refresh_scheme_years() holds a PER-SCHEME sql_by_scheme map. A scheme
    absent from it throws nothing -- it silently keeps the hard-coded defaults
    in _SCHEME_DATA_YEARS while the year chips drift from the live data."""
    src = (_ROOT / "app" / "pipeline.py").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"async def refresh_scheme_years\(\).*?(?=\n(?:async )?def |\nclass )",
                  src, re.DOTALL)
    body = m.group(0) if m else ""
    if not body:
        for s in schemes:
            rep.add(s, "6 pipeline", "refresh_scheme_years", WARN,
                    "could not locate refresh_scheme_years() to inspect", "")
        return

    import app.pipeline as pipeline
    years = getattr(pipeline, "_SCHEME_DATA_YEARS", {})
    for s in schemes:
        no_time = s in getattr(pipeline, "_SCHEMES_WITHOUT_YEAR", set()) \
            or years.get(s) == []
        if f'"{s}"' in body or f"'{s}'" in body:
            rep.ok(s, "6 pipeline", "refresh_scheme_years")
        elif no_time:
            # [] is the permanent value for a scheme with no time dimension.
            rep.ok(s, "6 pipeline", "refresh_scheme_years")
        else:
            rep.add(s, "6 pipeline", "refresh_scheme_years", SILENT,
                    f"{s!r} has years {years.get(s)} but no entry in sql_by_scheme, "
                    "so live years are never loaded for it",
                    "add the scheme's DISTINCT year_key SQL to sql_by_scheme, "
                    "or set _SCHEME_DATA_YEARS[scheme] = [] if it has no time dimension")


def check_prefix_collisions(schemes: list[str], rep: Report) -> None:
    """If one canonical name contains another, every regex needs a lookaround
    and every table matcher needs the specific name FIRST. The CM Elevate /
    CM Elevate Legacy pair cost a sweep of every registry."""
    import app.pipeline as pipeline
    import app.followups as followups
    import app.edge as edge

    pairs = [(a, b) for a in schemes for b in schemes
             if a != b and a.lower() in b.lower()]
    if not pairs:
        return

    for shorter, longer in pairs:
        # The shorter name's pattern must not also match the longer one.
        for mod, sym in ((pipeline, "_SCHEME_NAME_PATTERN"),
                         (followups, "_SCHEME_RX"),
                         (edge, "_SCHEME_ALIASES")):
            table = getattr(mod, sym, None)
            if not isinstance(table, dict) or shorter not in table:
                continue
            pats = table[shorter]
            pats = pats if isinstance(pats, (list, tuple)) else [pats]
            leaks = False
            for p in pats:
                rx = p if hasattr(p, "search") else None
                if rx is None:
                    try:
                        rx = re.compile(str(p), re.IGNORECASE)
                    except re.error:
                        continue
                if rx.search(longer):
                    leaks = True
            if leaks:
                rep.add(shorter, f"collision", f"{mod.__name__}.{sym}", SILENT,
                        f"the {shorter!r} pattern also matches {longer!r}, so "
                        f"{longer} questions can be read as {shorter}",
                        "add a negative lookbehind/lookahead for the longer name")
            else:
                rep.ok(shorter, "collision", f"{mod.__name__}.{sym}")

        # Table matchers: the longer name's tables must resolve to the longer scheme.
        try:
            from app.prompt_builder import _scheme_of
            import app.annotations as ann
            longer_dir = ann._SCHEME_DIRS.get(longer)
            if longer_dir:
                rep.ok(longer, "collision", "prompt_builder._scheme_of")
        except Exception as exc:  # pragma: no cover - defensive
            rep.add(longer, "collision", "prompt_builder._scheme_of", WARN,
                    f"could not check table mapping: {exc}", "")


def check_year_holes(schemes: list[str], rep: Report) -> None:
    """A hole year (absent, not zero) must be kept OUT of _SCHEME_DATA_YEARS so
    no chip ever offers it -- D-018, the Focus Legacy FY 2023-24 case. We can
    only flag a suspicious gap in an otherwise contiguous run for a human."""
    import app.pipeline as pipeline
    years = getattr(pipeline, "_SCHEME_DATA_YEARS", {})
    for s in schemes:
        ys = years.get(s) or []
        starts = sorted(int(y.split("-")[0]) for y in ys if re.match(r"^\d{4}-\d{2}$", y))
        if len(starts) < 2:
            continue
        gaps = [y for y in range(starts[0], starts[-1]) if y not in starts]
        if gaps:
            rep.add(s, "6 pipeline", "_SCHEME_DATA_YEARS", WARN,
                    f"{s} skips FY {', '.join(f'{g}-{str(g + 1)[2:]}' for g in gaps)} "
                    "inside its range",
                    "intended if that year is a data HOLE (a gap, not a zero, D-018); "
                    "confirm _apply_year_gap covers it")
        else:
            rep.ok(s, "6 pipeline", "_SCHEME_DATA_YEARS")


def check_roles(schemes: list[str], rep: Report) -> None:
    """A scheme missing from a role's `schemes` list is invisible to that role,
    with no error anywhere."""
    from app.auth import ROLE_PERMISSIONS
    for s in schemes:
        missing = [role for role, perms in ROLE_PERMISSIONS.items()
                   if s not in (perms.get("schemes") or [])]
        if missing:
            rep.add(s, "13 auth", "ROLE_PERMISSIONS", MISSING,
                    f"{s!r} missing from {len(missing)} role(s): {', '.join(sorted(missing))}",
                    "add the scheme to every role's `schemes` list, "
                    "or users with that role cannot see it")
        else:
            rep.ok(s, "13 auth", "ROLE_PERMISSIONS")


def check_kb_docs(schemes: list[str], rep: Report) -> None:
    """Three separate KB failures, all silent:
      1. no doc registered in kb_ingest._SOURCES;
      2. the doc's <!-- scheme: --> tag not matching the canonical name;
      3. the label absent from the doc text -- the composer fails on this, which
         is why the procedure says to run grep -ic "<label>" on the docs.
    A scheme with no KB of its own must instead have a _KB_SCHEME_ALIAS entry."""
    import app.kb_ingest as kb
    import app.rag as rag
    alias = getattr(rag, "_KB_SCHEME_ALIAS", {})
    canon = getattr(kb, "_CANONICAL_SCHEME", {})
    doc_names = getattr(rag, "_SCHEME_DOC_NAMES", {})

    sources = getattr(kb, "_SOURCES", [])
    ref_dir = _ROOT / "data" / "reference"

    for s in schemes:
        if s in alias:
            rep.ok(s, "12 kb_ingest", "_SOURCES (aliased)")
            continue

        # _SOURCES is a list of (relative path, scheme) tuples: the scheme is
        # stated in the registry, so there is no tag to parse here.
        own: list[str] = []
        for src in sources:
            if isinstance(src, (tuple, list)) and len(src) >= 2:
                rel, tagged = str(src[0]), str(src[1]).strip()
            else:
                rel, tagged = str(src), ""
            if tagged != s and canon.get(tagged) != s:
                continue
            path = _ROOT / "data" / rel
            if path.exists():
                own.append(path.name)
            else:
                rep.add(s, "12 kb_ingest", "_SOURCES", SILENT,
                        f"registered doc {rel!r} does not exist on disk, so the "
                        "scheme ingests with fewer chunks than intended",
                        "fix the path in kb_ingest._SOURCES (casing is significant "
                        "-- the SME filenames are kept verbatim)")

        if not own:
            rep.add(s, "12 kb_ingest", "_SOURCES", MISSING,
                    f"no registered doc in kb_ingest._SOURCES is tagged {s!r}",
                    "register the doc in kb_ingest._SOURCES, "
                    "or add a rag._KB_SCHEME_ALIAS entry if it has no KB of its own")
            continue

        rep.ok(s, "12 kb_ingest", "_SOURCES")

        # The composer-fails check: the label the docs use must appear in them.
        # _SCHEME_DOC_NAMES maps a scheme to (doc_name, description) -- the first
        # element is the spelling the documents actually use ("FOCUS",
        # "CM-ELEVATE"), which is the one to grep for, NOT the canonical name.
        entry = doc_names.get(s)
        if isinstance(entry, (tuple, list)) and entry:
            label = str(entry[0])
        elif isinstance(entry, str):
            label = entry
        else:
            label = s
        hits = 0
        for fname in own:
            text = (ref_dir / fname).read_text(encoding="utf-8", errors="replace")
            hits += len(re.findall(re.escape(label), text, re.IGNORECASE))
        if hits == 0:
            rep.add(s, "12 rag", "_SCHEME_DOC_NAMES", SILENT,
                    f"the label {label!r} never appears in {', '.join(own)}; "
                    "the composer fails when the label is absent from the docs",
                    "set rag._SCHEME_DOC_NAMES to the name the docs actually use "
                    "(Focus Legacy's docs say 'FOCUS', never 'Focus Legacy')")
        else:
            rep.ok(s, "12 rag", "_SCHEME_DOC_NAMES")


def check_data_folder(schemes: list[str], rep: Report) -> None:
    """The 7 SME YAMLs + README. Also: the few-shot bank actually registered
    must exist, and if a second bank sits beside it that is a review hazard
    (CM Elevate Legacy's v1 marks negatives `status: REFUSED`, which the loader
    does not read as a negative example)."""
    import app.annotations as ann
    dirs = getattr(ann, "_SCHEME_DIRS", {})
    few = getattr(ann, "_FEW_SHOT_FILE", {})

    wanted = ["schema_partitions", "classification_rules", "default_rules",
              "entity_resolver", "foreign_key_augmentation", "response_template"]

    for s in schemes:
        d = dirs.get(s)
        if not d or not Path(d).is_dir():
            rep.add(s, "1 SME contract", "data/<scheme>/", MISSING,
                    f"data folder for {s!r} not found ({d})",
                    "create the folder with the 7 SME YAMLs + README")
            continue
        names = [p.name for p in Path(d).iterdir() if p.is_file()]
        joined = " ".join(names).lower()
        absent = [w for w in wanted if w not in joined]
        if absent:
            rep.add(s, "1 SME contract", "data/<scheme>/", MISSING,
                    f"{s}: no YAML matching {', '.join(absent)}",
                    "the SME contract is incomplete; check against the live view columns")
        else:
            rep.ok(s, "1 SME contract", "data/<scheme>/")

        if not (Path(d) / "README.md").exists():
            rep.add(s, "1 SME contract", "README.md", WARN,
                    f"{s}: no README.md recording the data traps",
                    "the README is authoritative for that scheme's traps")

        bank = few.get(s)
        if bank:
            if not (Path(d) / bank).exists():
                rep.add(s, "2 annotations", "_FEW_SHOT_FILE", MISSING,
                        f"registered bank {bank!r} does not exist in {Path(d).name}/",
                        "fix the _FEW_SHOT_FILE entry")
            else:
                rep.ok(s, "2 annotations", "_FEW_SHOT_FILE file")
            others = [n for n in names
                      if ("few_shot" in n or "few_shots" in n) and n != bank]
            if others:
                rep.add(s, "2 annotations", "_FEW_SHOT_FILE", WARN,
                        f"{s}: {len(others)} other few-shot file(s) beside the "
                        f"registered {bank!r}: {', '.join(others)}",
                        "confirm the registered one is intended; an unregistered bank "
                        "may use a `status:` value the loader does not understand")


def check_privacy(schemes: list[str], rep: Report) -> None:
    """Privacy tables must never reach a prompt. Protection is prompt-only today
    (KNOWN_ISSUES KI-004), so the least we can do is assert the prohibition text
    is still present for the schemes that own those tables."""
    never = {
        "Focus Legacy": ["fact_focus_legacy_disbursement", "bridge_pg_bank_history"],
        "CM Elevate Legacy": ["fact_cm_elevate_disbursement"],
    }
    src = (_ROOT / "app" / "schema_context.py").read_text(encoding="utf-8",
                                                          errors="replace")
    for s in schemes:
        for table in never.get(s, []):
            if table in src:
                rep.ok(s, "privacy", table)
            else:
                rep.add(s, "privacy", table, SILENT,
                        f"{table} is a privacy table but is not mentioned in "
                        "schema_context.py, so nothing warns the model off it",
                        "restore the never-query instruction (KI-004: the prompt "
                        "is the only protection today)")


_CHECKS = [
    check_flat_registries,
    check_data_folder,
    check_activity_views,
    check_refresh_years,
    check_year_holes,
    check_prefix_collisions,
    check_roles,
    check_kb_docs,
    check_privacy,
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scheme", nargs="*", help="canonical name(s); default: all")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--quiet", action="store_true", help="only show gaps")
    args = ap.parse_args()

    try:
        import app.schema_context as sc
        known = list(sc.SCHEME_CATALOG)
    except Exception as exc:
        print(f"could not import the app: {exc}", file=sys.stderr)
        print("run from the repo root with .venv/Scripts/python.exe", file=sys.stderr)
        return 2

    schemes = args.scheme or known
    unknown = [s for s in schemes if s not in known]
    if unknown and args.scheme:
        # A name not in SCHEME_CATALOG is itself the first finding: that registry
        # is where a new scheme's identity starts.
        print(f"NOTE: not in SCHEME_CATALOG (expected for a scheme mid-onboarding): "
              f"{', '.join(unknown)}\n")

    rep = Report()
    for check in _CHECKS:
        try:
            check(schemes, rep)
        except Exception as exc:
            for s in schemes:
                rep.add(s, "checker", check.__name__, WARN,
                        f"check raised {type(exc).__name__}: {exc}", "")

    if args.json:
        print(json.dumps({"schemes": schemes, "findings": rep.rows}, indent=2))
        return 1 if rep.gaps() else 0

    total_gaps = 0
    for s in schemes:
        gaps = rep.gaps(s)
        checked = len([r for r in rep.rows if r["scheme"] == s])
        total_gaps += len(gaps)
        if not gaps:
            if not args.quiet:
                print(f"[OK]  {s}: {checked} checks, fully wired")
            continue
        print(f"\n[{len(gaps)} GAP(S)]  {s}  ({checked} checks)")
        for g in sorted(gaps, key=lambda r: (r["kind"] != SILENT, r["layer"])):
            print(f"  {g['kind']:<8} {g['layer']} :: {g['registry']}")
            print(f"           {g['detail']}")
            if g["fix"]:
                print(f"           fix: {g['fix']}")

    print(f"\n{'-' * 70}")
    print(f"{len(schemes)} scheme(s), {len(rep.rows)} checks, {total_gaps} gap(s)")
    if total_gaps:
        print("SILENT gaps fail without raising anything -- fix those first.")
    return 1 if total_gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
