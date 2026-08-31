"""
backend/prompt_builder.py — the single place the SQL-generation prompt is built.

Everything the generator sees about the database is composed here, in one order,
from four sources with a clear precedence:

  1. schema_context.build_schema_context(schemes)   — HAND-WRITTEN BACKBONE
        The curated/semantic object list per scheme, the money-unit facts, and
        the "wrong number if broken" hazard rules. Those rules are judgement, not
        structure — they can never be introspected, so they stay hand-maintained
        (see docs/DATA_MODEL.md).
  2. LIVE SCHEMA block                               — schema_introspect.live_columns()
        The columns that actually exist in megh_db right now, pulled from
        information_schema at startup, scheme-scoped. This is what catches
        schema_context.py drifting out of sync with the database. Silently
        omitted when the startup read didn't land (offline dev, catalog perms).
  3. LIVE CATALOG block                              — schema_introspect.catalog_block()
        SME-curated semantic.* descriptions, synonyms and metric formulas.
  4. PROHIBITED JOINS / VERIFIED EXAMPLES / RESOLVED ENTITIES
        annotations.py + entity_resolver output. Resolved entities go LAST, right
        before the question — a wrong-case district literal silently matches zero
        rows, so it is the single most load-bearing part of the prompt.

Public:
  build_sql_prompt(question, schemes, entity_result)      -> str   (first attempt)
  build_repair_prompt(question, schemes, entity_result,
                      failed_sql, error)                    -> str  (retry)

The repair prompt now carries the SAME schema + resolved-entities context as the
first attempt (plus the failure), instead of the thin schema-only prompt used
before — a repair most often needs exactly the entity block it was missing.
"""
from app import schema_introspect
from app.annotations import few_shot_examples, prohibited_joins_text
from app.schema_context import build_schema_context

# Bare table name -> owning scheme, so the LIVE SCHEMA block is scoped the same
# way schema_context.py scopes its hand-written one.
_SHARED_PREFIXES = ("dim_scheme", "dim_year", "dim_geography")
_CROSS_PREFIXES = ("v_cross_scheme",)
_MGNREGA_EXACT = {"v_employment", "v_expenditure", "v_district_year_summary"}


def _scheme_of(table: str) -> str:
    """'MGNREGA' | 'PMAY-G' | 'shared' | 'cross' for a bare (unqualified) table name."""
    t = table.lower()
    if t.startswith(_SHARED_PREFIXES):
        return "shared"
    if t.startswith(_CROSS_PREFIXES):
        return "cross"
    if "mgnrega" in t or t in _MGNREGA_EXACT:
        return "MGNREGA"
    if "pmay" in t:
        return "PMAY-G"
    return "shared"  # unknown object -> show it rather than hide it


def _live_schema_block(schemes: list[str]) -> str:
    """Real columns per table, scoped to the classified scheme(s). Empty string
    when live introspection is unavailable — the caller then relies on the
    hand-written backbone alone, exactly as before this layer existed."""
    live = schema_introspect.live_columns()
    if not live:
        return ""

    want = set(schemes)
    multi = len(schemes) > 1
    lines = [
        "LIVE SCHEMA — columns that exist in megh_db right now (information_schema, "
        "read at startup). If a column is not listed here it does not exist; do not use it.",
    ]
    for qualified, cols in sorted(live.items()):
        _, _, bare = qualified.partition(".")
        owner = _scheme_of(bare)
        if owner == "cross" and not multi:
            continue
        if owner in ("MGNREGA", "PMAY-G") and owner not in want:
            continue
        lines.append(f"  {qualified}({', '.join(c['column'] for c in cols)})")

    fks = schema_introspect.live_fks()
    scoped_fks = [
        fk for fk in fks
        if _scheme_of(fk["from"].partition(".")[2]) in ({"shared"} | want | ({"cross"} if multi else set()))
    ]
    if scoped_fks:
        lines.append("")
        lines.append("REAL FOREIGN KEYS (valid join paths — PROHIBITED JOINS below still overrides):")
        lines += [f"  {fk['from']}.{fk['from_column']} -> {fk['to']}.{fk['to_column']}"
                  for fk in scoped_fks]
    return "\n".join(lines) + "\n"


def _fewshot_block(schemes: list[str], question: str = "") -> str:
    examples = few_shot_examples(schemes, question, top_k=4)
    if not examples:
        return ""
    parts = [f'Q: "{ex["question"]}"\nSQL: {ex["sql"]}' for ex in examples]
    return "\nVERIFIED EXAMPLES:\n" + "\n\n".join(parts) + "\n"


def _entities_block(entity_result: dict) -> str:
    resolved = entity_result.get("resolved", {})
    notes = entity_result.get("notes", [])
    lines: list[str] = []
    if resolved:
        lines.append(
            "RESOLVED ENTITIES — MANDATORY: the WHERE clause MUST use these exact "
            "values, not any name or spelling from the question text. These are already "
            "resolved against the database (correct case, correct code, correct year_key):"
        )
        for k, v in resolved.items():
            if k == "village_code":
                lines.append(f"  village_code = {v!r}   -- do NOT filter on lgd_village_name instead")
            elif k == "district":
                lines.append(f"  lgd_district = {v!r}   -- already uppercase, matches storage exactly")
            elif k == "block":
                lines.append(f"  lgd_block = {v!r}   -- already uppercase, matches storage exactly")
            elif k == "year_key":
                lines.append(f"  year_key = {v!r}")
            elif k == "house_status":
                vals = v if isinstance(v, list) else [v]
                quoted = ", ".join(f"'{s}'" for s in vals)
                if len(vals) == 1:
                    lines.append(f"  status_name = {quoted}   -- exact stored PMAY-G stage "
                                 "label; use it verbatim, do not paraphrase or re-case")
                else:
                    lines.append(f"  status_name IN ({quoted})   -- exact stored PMAY-G stage "
                                 "labels; match with IN / OR, never AND")
                if set(vals) & {"Proposed Site", "Existing site(Old House)"}:
                    lines.append(
                        "      NOTE: in curated.v_pmay EVERY 'Proposed Site' and 'Existing "
                        "site(Old House)' row has is_placeholder = TRUE (they are the "
                        "sanctioned_amount = 0 / year_key NULL bucket). For this status "
                        "count do NOT add 'AND NOT is_placeholder' — that guard would drop "
                        "the entire category and return a false 0. Count status_name over "
                        "all rows.")
            else:
                lines.append(f"  {k} = {v!r}")
    if notes:
        lines.append("NOT FOUND (do not filter on these — state plainly they're not in the data):")
        lines += [f"  {n}" for n in notes]
    return "\n" + "\n".join(lines) + "\n" if lines else ""


def _prohibited_block(schemes: list[str]) -> str:
    prohibited = prohibited_joins_text(schemes)
    return f"\nPROHIBITED JOINS:\n{prohibited}\n" if prohibited else ""


def build_sql_prompt(question: str, schemes: list[str], entity_result: dict) -> str:
    catalog = schema_introspect.catalog_block(schemes)
    return "".join([
        build_schema_context(schemes), "\n\n",
        _live_schema_block(schemes),
        (catalog + "\n") if catalog else "",
        _prohibited_block(schemes),
        _fewshot_block(schemes, question),
        _entities_block(entity_result),
        f"\nThe user's question is about: {', '.join(schemes)}.\n",
        f'\nQuestion: "{question}"\nSQL:',
    ])


def build_repair_prompt(question: str, schemes: list[str], entity_result: dict,
                        *, failed_sql: str, error: str, extra_hint: str | None = None) -> str:
    return "".join([
        build_schema_context(schemes), "\n\n",
        _live_schema_block(schemes),
        _prohibited_block(schemes),
        _entities_block(entity_result),
        "\nThe previous query FAILED and must be corrected.\n",
        f"Error: {error}\n",
        (f"Hint: {extra_hint}\n" if extra_hint else ""),
        f"Previous query:\n{failed_sql}\n",
        f'\nQuestion: "{question}"\n',
        "Return the corrected single read-only SELECT. SQL:",
    ])
