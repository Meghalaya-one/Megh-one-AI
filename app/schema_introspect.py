"""
Live schema catalog — reads the SME-curated `semantic.*` tables in megh_db
(table_catalog, column_catalog, glossary, join_graph, metric_definitions) once
at startup and folds a compact view of them into the SQL-generation prompt,
alongside the hand-written rules in schema_context.py.

schema_context.py stays the source of truth for the *hazards* (which joins are
invalid, which columns are stocks) — the catalog can't encode those. This module
adds the *descriptions, synonyms and metric formulas* the SMEs maintain in the
database, so the generator sees the same vocabulary an analyst would.
"""
import logging

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

# subject_area token in semantic.* -> our scheme label
_SUBJECT_TO_SCHEME = {"mgnrega": "MGNREGA", "pmay": "PMAY-G", "pmayg": "PMAY-G",
                      "pmay-g": "PMAY-G", "housing": "PMAY-G", "employment": "MGNREGA"}

_cache: dict = {
    "loaded": False, "tables": [], "glossary": [], "metrics": [], "joins": [],
    # Live information_schema extract (structure, not SME prose). Populated by
    # _load_live_schema(); empty => the prompt falls back to the hand-written
    # schema_context.py backbone alone.
    "live_loaded": False, "live_columns": {}, "live_fks": [],
}

# The only schemas the read-only app role can see, and the only ones worth
# putting in front of the SQL generator.
_QUERYABLE_SCHEMAS = ("curated", "semantic")


async def load() -> None:
    if not settings.SCHEMA_CATALOG_ENABLED:
        return
    await _load_semantic_catalog()
    await _load_live_schema()


async def _load_semantic_catalog() -> None:
    try:
        _cache["tables"] = await db.fetch_rows(
            """SELECT table_schema, table_name, entity_role, grain, description,
                      subject_area, synonyms, row_count
               FROM semantic.table_catalog
               WHERE is_chatbot_visible IS NOT FALSE
               ORDER BY table_schema, table_name""")
        _cache["glossary"] = await db.fetch_rows(
            """SELECT term, definition, subject_area, synonyms, maps_to_object, maps_to_column
               FROM semantic.glossary ORDER BY term""")
        try:
            _cache["metrics"] = await db.fetch_rows("SELECT * FROM semantic.metric_definitions")
        except Exception:  # noqa: BLE001 — table shape varies; optional
            _cache["metrics"] = []
        try:
            _cache["joins"] = await db.fetch_rows("SELECT * FROM semantic.join_graph")
        except Exception:  # noqa: BLE001
            _cache["joins"] = []
        _cache["loaded"] = True
        logger.info("schema_introspect: catalog loaded — %d tables, %d glossary terms, "
                    "%d metrics", len(_cache["tables"]), len(_cache["glossary"]),
                    len(_cache["metrics"]))
    except Exception as e:  # noqa: BLE001 — never block startup on the catalog
        logger.warning("schema_introspect: catalog load failed (SQL prompt uses static "
                       "context only): %s", e)


async def _load_live_schema() -> None:
    """Read the real structure of curated/semantic straight from the catalog —
    so the SQL prompt lists the columns that exist today, not a hand-maintained
    copy that drifts. Best-effort: any failure leaves live_columns empty and the
    prompt falls back to schema_context.py alone."""
    try:
        col_rows = await db.fetch_rows(
            """SELECT table_schema, table_name, column_name, data_type, is_nullable
               FROM information_schema.columns
               WHERE table_schema = ANY($1::text[])
               ORDER BY table_schema, table_name, ordinal_position""",
            [list(_QUERYABLE_SCHEMAS)],
        )
        by_table: dict[str, list[dict]] = {}
        for c in col_rows:
            key = f"{c['table_schema']}.{c['table_name']}"
            by_table.setdefault(key, []).append({
                "column": c["column_name"],
                "type": c["data_type"],
                "nullable": c["is_nullable"] == "YES",
            })
        _cache["live_columns"] = by_table

        # pg_catalog (not information_schema.constraint_column_usage) — the latter
        # returns nothing for a role that doesn't own the tables; pg_constraint is
        # world-readable and handles composite keys.
        fk_rows = await db.fetch_rows(
            """SELECT ns.nspname   AS from_schema, cl.relname   AS from_table,
                      att.attname  AS from_column,
                      fns.nspname  AS to_schema,  fcl.relname  AS to_table,
                      fatt.attname AS to_column
               FROM pg_constraint con
               JOIN pg_class cl       ON cl.oid  = con.conrelid
               JOIN pg_namespace ns   ON ns.oid  = cl.relnamespace
               JOIN pg_class fcl      ON fcl.oid = con.confrelid
               JOIN pg_namespace fns  ON fns.oid = fcl.relnamespace
               JOIN unnest(con.conkey)  WITH ORDINALITY AS k(attnum, ord)  ON TRUE
               JOIN unnest(con.confkey) WITH ORDINALITY AS fk(attnum, ord) ON fk.ord = k.ord
               JOIN pg_attribute att   ON att.attrelid  = cl.oid  AND att.attnum  = k.attnum
               JOIN pg_attribute fatt  ON fatt.attrelid = fcl.oid AND fatt.attnum = fk.attnum
               WHERE con.contype = 'f'
                 AND ns.nspname = ANY($1::text[])
               ORDER BY 1, 2, 3""",
            [list(_QUERYABLE_SCHEMAS)],
        )
        _cache["live_fks"] = [
            {"from": f"{r['from_schema']}.{r['from_table']}", "from_column": r["from_column"],
             "to": f"{r['to_schema']}.{r['to_table']}", "to_column": r["to_column"]}
            for r in fk_rows
        ]
        _cache["live_loaded"] = True
        logger.info("schema_introspect: live schema — %d tables, %d FK edges",
                    len(by_table), len(_cache["live_fks"]))
    except Exception as e:  # noqa: BLE001 — never block startup on introspection
        logger.warning("schema_introspect: live schema read failed (prompt uses "
                       "hand-written schema_context only): %s", e)


def live_columns() -> dict:
    """{'curated.fact_pmay_house': [{'column','type','nullable'}, ...], ...} or {}."""
    return _cache["live_columns"]


def live_fks() -> list:
    """[{'from','from_column','to','to_column'}, ...] or []."""
    return _cache["live_fks"]


def _matches_scheme(subject_area: str | None, schemes: list[str]) -> bool:
    if not subject_area:
        return True  # shared objects (geography, year) apply to every scheme
    mapped = _SUBJECT_TO_SCHEME.get(str(subject_area).strip().lower())
    return mapped is None or mapped in schemes


def catalog_block(schemes: list[str], *, max_terms: int = 24) -> str:
    """A compact 'LIVE CATALOG' block scoped to the classified scheme(s).
    Empty string when the catalog didn't load."""
    if not _cache["loaded"]:
        return ""
    lines: list[str] = []

    tbls = [t for t in _cache["tables"] if _matches_scheme(t.get("subject_area"), schemes)
            and t["table_schema"] in ("curated", "semantic")]
    if tbls:
        lines.append("LIVE CATALOG — table notes (from semantic.table_catalog):")
        for t in tbls[:30]:
            desc = (t.get("description") or t.get("grain") or "").strip().replace("\n", " ")
            if desc:
                lines.append(f"  {t['table_schema']}.{t['table_name']}: {desc[:240]}")

    gloss = [g for g in _cache["glossary"] if _matches_scheme(g.get("subject_area"), schemes)]
    if gloss:
        lines.append("\nLIVE CATALOG — business terms (from semantic.glossary):")
        for g in gloss[:max_terms]:
            tgt = g.get("maps_to_column") or g.get("maps_to_object") or ""
            tail = f"  -> {tgt}" if tgt else ""
            lines.append(f"  \"{g['term']}\": {(g.get('definition') or '')[:180]}{tail}")

    if _cache["metrics"]:
        lines.append("\nLIVE CATALOG — metric definitions (from semantic.metric_definitions):")
        for m in _cache["metrics"][:20]:
            name = m.get("metric") or m.get("name") or m.get("metric_name")
            formula = m.get("formula") or m.get("expression") or m.get("definition")
            if name and formula:
                lines.append(f"  {name} = {str(formula)[:200]}")

    return ("\n".join(lines) + "\n") if lines else ""


def snapshot() -> dict:
    return {
        "loaded": _cache["loaded"],
        "tables": _cache["tables"],
        "glossary": _cache["glossary"],
        "metrics": _cache["metrics"],
        "join_graph": _cache["joins"],
        "live_loaded": _cache["live_loaded"],
        "live_columns": _cache["live_columns"],
        "live_fks": _cache["live_fks"],
    }
