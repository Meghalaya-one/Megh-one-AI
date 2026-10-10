# Technical Decisions

*Read this before proposing an architectural replacement. It lists only decisions traceable to
code comments, commits or project docs; the source is cited on each entry. Dates are those
recorded in the source. "≤ date" means the decision predates that commit, but its exact date is
not recorded.*

*Status: **Active** · **Superseded** · **Accepted risk**.*

---

### D-001 — One FastAPI service, scheme selection inside the pipeline
- **Date:** ≤ 2026-08-31 (README; the earliest commit is `e143c1c`).
- **Decision:** a single service (`app.main:app`) handles every scheme. The earlier per-scheme
  services (`unified-data`, `cm-elevate`, `focus`) and the gateway were removed.
- **Reason:** a single deploy unit, and schemes are chosen per question (including
  cross-scheme questions), not per URL.
- **Alternatives:** per-scheme microservices behind a gateway (the predecessor design).
- **Consequences:** schemes are hand-registered in about 14 places (see SCHEMES.md), and
  `pipeline.py` has grown to about 8,400 lines.
- **Status:** Active.

### D-002 — No LLM or ORM framework
- **Date:** ≤ 2026-08-31 (code; TECHNICAL_BRIEF §0).
- **Decision:** raw `httpx` calls to an OpenAI-compatible API, raw `asyncpg`, and
  `qdrant-client`. No LangChain, LlamaIndex or SQLAlchemy.
- **Reason (INFERRED):** full control over prompts and the vLLM-specific fields
  (`guided_json`, `guided_regex`, `enable_thinking`).
- **Consequences:** every model call and every query is hand-written.
- **Status:** Active.

### D-003 — Model role split
- **Date:** 2026-09-10 (`config.py` comments; `docs/INFERENCE_REQUIREMENTS.md` Task 6).
- **Decision:**
  - `qwen-model` (30B coder): SQL generation **only**;
  - `qwen4-deploy` (Qwen3-4B): classifier (intent, scheme, entity spans, follow-up rewrite)
    **and** SQL verifier;
  - `qwen35-9b`: composition only.
- **Reason:** keep the scarce 30B's batch free for SQL, and stop composition queueing behind
  the frequent small classify calls.
- **Alternatives:**
  - the classifier on the 9B (until 2026-09-10);
  - the classifier on the 30B (before that).
- **Consequences:** the 4B carries both classify and verify traffic, so its queue depth needs
  watching. Classifier accuracy against the 9B baseline was to be validated; whether it was is
  UNKNOWN.
- **Status:** Active.

### D-004 — Local CPU embeddings (fastembed bge-small)
- **Date:** ≤ 2026-08-31 (present in the first commit `e143c1c`; the exact date is not recorded).
- **Decision:** `EMBEDDING_PROVIDER=local` (`BAAI/bge-small-en-v1.5`, 384-dim), for the KB, the
  semantic cache and conversation memory.
- **Reason:** the gateway had no embedding model deployed.
- **Consequences:** CPU load on the app workers. RAG thresholds are tuned to the bge-small
  cosine scale (HIGH 0.84, MEDIUM 0.55).
- **Status:** Active. Switch to `gateway` once `qwen3-embedding` is serving; this needs a KB
  rebuild and re-tuned thresholds.

### D-005 — Reranker disabled
- **Date:** 2026-08-29 (`config.py`).
- **Decision:** `RERANKER_ENABLED=False`.
- **Reason:** the deployed `qwen3-reranker` inverted relevance on this KB. For example, it ranked
  "where to find info" above "application process" for a "what documents" query.
- **Status:** Active until the reranker deployment is fixed.

### D-006 — LLM semantic verifier after the regex guards, failure-open
- **Date:** 2026-09-10/11 (commit `7ad882f`; `config.py`).
- **Decision:** a 4B "does this SQL answer the question?" check runs after the free regex guards
  and before execution. Any verifier failure means "no issue".
- **Reason:** it catches wrong-but-valid SQL in shapes no guard knows yet. A verifier outage
  must never block an answer.
- **Consequences:** false positives (KI-002), handled with calibration examples and 8
  suppression filters.
- **Status:** Active.

### D-007 — Prose rule + few-shot + deterministic guard for every wrong-number bug
- **Date:** convention visible in many incident comments, 2026-09-10 → 2026-09-25 (INFERRED).
- **Decision:** a known bad SQL or answer shape is caught **in code**:
  - `execute_with_repair` guards;
  - `compose_response` faithfulness checks;
  - exact re-queries (`_cm_legacy_exact_totals`);
  - deterministic answers.

  A prompt rule is never relied on alone.
- **Reason:** prose rules repeatedly failed under sampling. Examples:
  - the MGNREGA join fan-out;
  - the AC + invented block filter;
  - CM Elevate Legacy rounded sums, which failed 3 times.
- **Status:** Active.

### D-008 — Ask, don't guess
- **Date:** ≤ 2026-08-31 (`SCOPE_CLARIFY_ENABLED` etc. are in the first commit `e143c1c`).
- **Decision:** pause with one-tap chips when the scheme, top-N, scope, year, tranche or place
  level is missing or ambiguous.
- **Reason:** a silent default ("both schemes", "top 10", "all years") gives confident answers
  to the wrong question.
- **Consequences:** a high clarification rate, and loop risk (KI-001). Each gate has an off
  switch.
- **Status:** Active.

### D-009 — Scheme-name collisions: ask for Focus, pin for CM Elevate
- **Date:** Focus 2026-09-22; CM Elevate 2026-09-24 (commit `7064ab6`).
- **Evidence:** the code comments at `_is_ambiguous_focus` / `_pin_cm_elevate_dataset`, and `focuslegacy_entity_resolver.yaml` `scheme.disambiguation`. The user's confirmation of the CM Elevate choice comes from a session note and is **not recorded in the repo**.
- **Decision:**
  - A bare "Focus" → a two-way ask.
  - A bare "CM Elevate" → pinned by money/FY/lender words to Legacy, otherwise CM Elevate.
- **Reason:**
  - Focus Plus and Focus Legacy share no key and have different grains, so guessing answers from
    a partition that cannot answer.
  - For CM Elevate, pinning keeps the existing flows unchanged.
- **Status:** Active.

### D-010 — Gateway TLS: CA bundle if present, else `verify=False`
- **Date:** 2026-09-13 (commit `5c5a100`).
- **Decision:** `llm._get_ssl_verify` falls back to `verify=False` when no bundle is found.
- **Reason:** the internal gateway uses a self-signed "Enlight AIOps" CA.
- **Status:** Accepted risk (KI-015).

### D-011 — Live schema introspection alongside hand-written rules
- **Date:** 2026-08-29, per the earlier `DATA_MODEL.md` ("Since 2026-08-29"). `_live_schema_block` is in the first commit `e143c1c` (2026-08-31).
- **Decision:** the SQL prompt carries the real columns and FKs read at startup
  (`schema_introspect`). The hand-written `schema_context.py` stays the source of the **hazard
  rules**, which cannot be introspected.
- **Consequences:** if the startup read fails, the prompt silently falls back to the hand-written
  text only.
- **Status:** Active.

### D-012 — Few-shot ranking: lexical IDF, per scheme
- **Date:** 2026-09-23 (`annotations.py`; `test_fewshot_ranking.py`).
- **Decision:**
  - IDF-weighted token overlap within each scheme's pool;
  - tokens in ≥25% of the pool weighted 0.05;
  - top 5 per scheme.
- **Reason:** raw overlap ranked the scheme's own name highest, so breakdown questions retrieved
  bare-SUM examples (8 of 12 breakdown questions had no right-shaped exemplar before the change,
  0 of 12 after).
- **Alternatives:** embedding similarity (not adopted).
- **Status:** Active.

### D-013 — CM Elevate Legacy shares CM Elevate's knowledge base
- **Date:** 2026-09-24.
- **Evidence:** `rag._KB_SCHEME_ALIAS` and its comment. The user's confirmation comes from a session note and is not recorded in the repo.
- **Reason:** they are one programme, and Legacy has no reference docs of its own.
- **Status:** Active.

### D-014 — Stateless JWT auth; department = tenant
- **Date:** ≤ 2026-08-31 (`security.py`, the earlier ARCHITECTURE doc).
- **Decision:**
  - HS256 self-contained JWTs (12 h);
  - PBKDF2 passwords;
  - authorization checks run on the **generated SQL** before execution;
  - admin roles bypass the scope checks but stay tenant-scoped.
- **Consequences:** no token revocation (KI-016); regex-based SQL inspection (KI-007).
- **Status:** Active.

### D-015 — The service boots degraded rather than failing
- **Date:** ≤ 2026-08-31 (`main._try`, `db.init_pool`).
- **Decision:** DB, Qdrant, schema-catalogue and KB failures at startup are logged, and the app
  still serves traffic (`/health` reports `degraded`).
- **Status:** Active.

### D-016 — Data-load defects are fixed by the ingestion team, not patched in the bot
- **Date:** 2026-09-25.
- **Evidence:** `docs/CM_Elevate_Legacy_DB_Issues.md` ("Fixing both in the load makes all 16 match the raw file with no change to the bot"), `docs/Focus_Legacy_DB_Issues.md` and `docs/Focus_Legacy_Fix_Verification.md`. The user's explicit instruction to that effect is from a session note and is **not recorded in the repo**.
- **Decision:** when the bot's SQL is right but `curated` data is wrong, write a
  `docs/<Scheme>_DB_Issues.md` for `megh-ingestion` and do not work around it in prompts.
- **Exception:** column mappings made necessary by a DB change (e.g. `_focus_legacy_geo_columns`).
- **Status:** Active.

### D-017 — The composer may only state numbers present in the result
- **Date:** ≤ 2026-08-31 (`_answer_numbers_faithful` is in the first commit `e143c1c`).
- **Decision:** a numeric-faithfulness check, a metric-coverage check and a hedge check. On
  failure: one strict retry, then a deterministic sentence.
- **Reason:** a misquoted number on a government dashboard is unacceptable.
- **Status:** Active.

### D-018 — Year gaps are reported alongside the answer, not turned into a pause
- **Date:** 2026-09-23 (`_apply_year_gap`; `test_year_gap_*.py`).
- **Decision:**
  - A question naming an absent year plus a valid one is answered for the valid year, with a
    note.
  - A comparison substitutes the nearest year that has data.
  - Only questions naming absent years alone get the pause.
- **Reason:** the SME response contract says so (`fy_gap_note`).
- **Status:** Active.

### D-019 — Voice input: vocabulary prompt on, echo and no-speech guard, system default mic
- **Date:** 2026-09-25 (`config.ASR_PROMPT`, `asr_guard.py`).
- **Decision:**
  - send a short vocabulary prompt (schemes + 12 districts + a digits example);
  - treat a verbatim prompt echo or a stock phrase ("Okay.") as no speech;
  - do not gate on an energy VAD;
  - no in-page mic picker (the user rejected it; from a session note, not recorded in the repo);
  - refuse clips shorter than 1 s client-side.
- **Reason:** measured transcription errors on district names, and hallucinated text on silent
  clips.
- **Status:** Active.

### D-020 — CSP keeps `'unsafe-inline'`
- **Date:** ≤ 2026-08-31 (`config.CSP_ENFORCE` is in the first commit `e143c1c`; `docs/SECURITY.md`).
- **Reason:** the served HTML uses inline scripts, styles and about 30 `onclick` handlers.
  Tightening needs the JS externalised first.
- **Status:** Accepted risk.

### D-021 — Vanilla HTML/JS UI served by the API
- **Date:** ≤ 2026-08-31 (`web/`, `main.py`).
- **Decision:** there is no frontend build. `web/*.html` is served by FastAPI with
  `Cache-Control: no-cache`, because a stale cached page once shipped old voice-input code.
- **Status:** Active.

### D-022 — Follow-up context from structured state, not an answer slice
- **Date:** 2026-09-26 (`context_manager.build_rewrite_evidence`, `pipeline._build_rewrite_prompt`,
  `pipeline._rewrite_provenance_violation`, `tests/test_context_semantic_state.py`).
- **Decision:**
  - The follow-up rewrite gets the previous turn as **tiers**:
    - `PREVIOUS filters`, the schemes plus resolved entities, always;
    - `PREVIOUS result`, a deterministic row summary, only when the follow-up points into the
      result;
    - a sentence-bounded answer excerpt, only when it points into a knowledge answer.
    These replace the fixed `prev.answer[:300]`.
  - A rewrite that names a scheme, district or block that no permitted source contains is
    discarded, and the original fragment goes on.
  - `update_state` does not commit a failed DATA turn.
  - Prompt budgets are **log-and-warn only**.
- **Reason:**
  - The slice carried names the follow-up never referred to. Measured: a long answer's slice
    put 4 unrelated block names into a metric-only follow-up, and the prompt allowed the model
    to use them.
  - The slice also cut off anything past character 300.
  - The previous turn's scope already exists as structured data.
  - Pruning SQL-prompt sections to fit a budget would trade a visible failure for a silent
    wrong number.
- **Alternatives rejected:**
  - A larger slice or a larger context window: more contamination, not less.
  - An LLM summary of the answer: an extra 4B call per turn, and critical fields would depend
    on generated text.
  - Retrying the rewrite on a provenance violation: an extra model call. The fragment plus
    `prior_resolved` already carries the scope deterministically.
- **Consequences:**
  - Follow-ups that point into the result ("the top one") depend on the cue regex
    `references_previous_result`.
  - Villages and years are not provenance-checked (KI-029).
- **Status:** Active.

### D-023 — Shared conversation state in Postgres, not Redis
- **Date:** 2026-09-26 (`app/session_sync.py`, `conversation_store.save_session_state`,
  `Session.to_snapshot/apply_snapshot`).
- **Decision:** every request reads, and after answering writes, a versioned snapshot in the
  existing `app.conversations.context_state` JSONB column. The write is awaited, with a 1 s
  bound. A stale concurrent write is refused rather than overwriting a newer one.
- **Reason:**
  - The deploy has 4 workers and no stickiness, so the per-worker `SessionStore` lost follow-up
    antecedents and pending clarifications (KI-028, KI-001).
  - `app.conversations` already exists wherever the app can answer at all.
- **Alternatives rejected:**
  - Redis: optional and best-effort in this codebase, unset in dev, and a second store to keep
    consistent.
  - Sticky sessions in nginx: fixes routing, not restarts.
  - Reloading only when the local copy was empty (the old rule): a worker with an older copy
    kept using it.
- **Consequences:**
  - +1 SELECT and +1 UPSERT per request.
  - With the DB down, a request degrades to the local copy.
  - `maybe_update_summary` no longer writes by itself.
- **Status:** Active (offline- and live-verified 2026-09-26; DB sync measured at ~50 ms p50 each way).

### D-024 — Field-level merge policy and provenance for follow-ups
- **Date:** 2026-09-26 (`app/context_policy.py`).
- **Decision:**
  - Each field (scheme, district, block, village, year, category, metric, group_by) gets
    KEEP / REPLACE / CLEAR / REQUIRE_CLARIFICATION from deterministic scanners.
  - The follow-up kind picks the rewrite's context layers.
  - `prior_resolved` loses every field that is replaced, cleared or not pinned.
  - Every committed value carries a provenance source, and `model_inference` values are not
    inherited.
  - The rewrite is checked field by field.
- **Reason:**
  - A blanket "inherit what the question doesn't mention" kept a district under "by district"
    and a block under a new district.
  - One rewrite rule for all fields could not tell a user's explicit year change from a
    contaminated one.
- **Alternatives rejected:**
  - An LLM classifier for the follow-up kind: an extra call per turn, for signals the existing
    vocabularies already give.
  - Per-scheme policies: they would have to grow with every scheme. The policy is keyed by field.
- **Status:** Active. Live 2026-09-26: 43/43 scenario checks, against 39/43 for the old path.

### D-025 — MGNREGA: report admin expenditure with a caveat; build the two fixed-shape queries in code
- **Date:** 2026-09-26 (`app/pipeline.py`; MGNREGA use-case QA, KI-041 to KI-048).
- **Decision:**
  - **Administrative expenditure (MGNREGA only):** answer with the recorded
    `SUM(admin_total_exp)` for the asked area and year. The "never populated at source" caveat
    and the live statewide non-zero count are **always** attached. PMAY-G and the multi-scheme
    wording still refuse.
  - **"Spent AND person-days/employment" for one area (MGNREGA only):** the SQL is built by
    `_mgnrega_combined_facts_query` from the resolved filters and runs with parameters bound.
    There is no 30B call, no repair and no verifier.
  - **Scope:** every change is gated on MGNREGA. The user asked that other schemes stay
    unchanged. That includes the negated-level fix (KI-041), although the same chip text
    affects the other schemes.
- **Reason:**
  - Use case DATA-013 expects the figure. The 2026-09-17 refusal existed because a bare
    "0.00" reads as a measured finding, and the caveat addresses exactly that.
  - The combined query has one correct shape. The model path failed 4 of 5 times, and the 4B
    verifier falsely flagged the correct CTE rewrite as a prohibited join.
- **Alternatives rejected:**
  - Keep refusing admin expenditure: it fails the SME use case.
  - Another few-shot plus a verifier filter for the combined query: that is still sampled,
    and CLAUDE.md §5 says prose rules have repeatedly failed.
- **Status:** Active. Live 2026-09-26: 43/43 MGNREGA use-case queries.

### D-026 — Focus Plus: deterministic answer guarantees; three general fixes applied to every scheme
- **Date:** 2026-09-27 (`app/pipeline.py`, `app/routers/query.py`; Focus Plus use-case QA,
  KI-060 to KI-064, KI-066, KI-067).
- **Decision:**
  - **Focus Plus only** (`schemes == ["Focus Plus"]`): `_focusplus_answer_guarantees` runs after
    the composer. It adds shares, the comparison difference and higher side, a complete named
    list, and rupee formatting (AI_PIPELINE §2.9, step 3).
  - A single-count share is computed by a **parameter-bound recount**. It is stated only when
    the recount reproduces the bot's own figure, so it cannot describe a different population
    than the number beside it.
  - **General, all schemes** (each changes only something that was plainly broken):
    1. `_fix_digit_grouping` re-groups a mis-grouped figure whose digits are a result value;
    2. numbers inside the SQL's own quoted literals are not misquotes;
    3. a pause reply that pauses again is remembered as the full resumed question
       (`pause_question`).

    The bank pause gains chips, and the Focus Legacy bank `KeyError` is fixed.
- **Reason:**
  - The composer stated every figure correctly but omitted what the use cases require.
    CLAUDE.md §5: prose rules fail under sampling, so the guarantee is deterministic.
  - The general fixes are not scheme behaviour: a garbled number, a label read as a figure,
    and a lost question are wrong in every scheme, and each fix has a guard that leaves valid
    text alone (4+ digits and a data value; a literal only; the resumed question falls back to
    the raw one).
- **Alternatives rejected:**
  - Prompt rules for percentages or differences: these are sampled, the pattern that already
    failed.
  - Adding a share column to `rows`: the chart renderer would plot it as a second series.
  - Hard-coding the 12.5K cohort size (12,527): the denominator depends on the area and year
    asked.
- **Status:** Active. Live 2026-09-27: 30/30 use cases on 2 of 2 fresh runs; the typed bank
  flow was checked live through three pauses; live context suite 43/43.

### D-027 — CM Elevate: deterministic SQL guards and answer guarantees; both readings of "pending"
- **Date:** 2026-09-27 (`app/pipeline.py`, `app/schema_context.py`; CM Elevate use-case QA,
  KI-068 to KI-075).
- **Decision:**
  - **CM Elevate only** (`schemes == ["CM Elevate"]`):
    - five SQL rewrites in `execute_with_repair`: the programme split, zero-count sectors,
      pending at a level, a plain "pending" never becoming a level filter, and decision states
      on `file_status`;
    - `_cm_elevate_answer_guarantees` after the composer (AI_PIPELINE §2.8 and §2.9 step 3).
  - The Unresolved guard now also covers CM Elevate (`_UNRESOLVED_PLACEHOLDER_SCHEMES`).
  - "PRIME SEED" is a CM Elevate-only term. A bare "seed" is not, because Focus Legacy says
    "seed money".
  - **KI-074 interim:** "pending" stays `data_verified = 'On Hold'` (the SME rule), unchanged.
    A single-figure pending answer also states the `file_status = 'Pending'` count from the same
    query, so neither reading is hidden. Approved and rejected become answerable from
    `file_status`.
- **Reason:** every failure had correct rows or an unambiguous SQL fix, and each prose rule
  (schema_context rules 4, 5, 13 and 14) had already been in the prompt. CLAUDE.md §5 calls
  for a deterministic guard. The "pending" meaning is a product decision, so the interim shows
  both readings instead of silently switching the figure.
- **Alternatives rejected:**
  - New few-shots in `cmelevate_few_shot.yaml`: HANDOFF lists `data/<scheme>/*.yaml` as
    do-not-change, and few-shots are sampled.
  - Redefining "pending" as `file_status`: it would change every existing pendency answer
    without a product decision.
  - Making the guarantees scheme-agnostic: other schemes have not been QA'd against them.
- **Status:** Active. Live 2026-09-27: 55/55 questions (30/30 use cases) on 2 of 2 fresh runs;
  11 reworded probes gave 10 correct and 1 pre-existing intent flake (KI-076); context suite
  43/43. **Revisit KI-074** when the product owner decides.

- **Amendment 2026-09-28:** KI-074 decided by the product owner — "pending" = `data_verified = 'On Hold'` only; the interim file-status line was removed. CM Elevate also joined the village-grained guards (D-028 / D-029 pattern) after its all-villages run; final 7,364 / 7,364.

- **Amendment 2026-09-28 (2):** CM Elevate "status" = `current_file_status` (product owner, from the testers' 22 Sep sheet). Pending stays On Hold; nothing else changed.

- **Amendment 2026-10-05 (KI-186):** "pending AT a level" = `LOWER(current_level) = 'levelN'` AND `scheme_specific ->> 'file_status' = 'Pending'` (product owner, asked in session after the screenshot "unique applications pending at level 1 for all of Meghalaya" -> 8,372). The user first said "from current_file_status"; that column holds no pending value (forward / sendback / resubmit), so the options were put to them and they chose file_status = 'Pending'. Level 1 = 8,307 (was 8,372: 64 Rejected + 1 Approved are no longer counted), level 2 = 165, level 0 = 0. Plain "pending" with no level stays `data_verified = 'On Hold'` (also their choice). "unique / distinct applications" = COUNT(DISTINCT request_id) is now guaranteed by a guard.

### D-028 — Village-grained guards apply to MGNREGA and Focus Plus, not MGNREGA alone
- **Date:** 2026-09-27 (`app/pipeline.py`; Focus Plus all-blocks / all-villages run, KI-079 to
  KI-083).
- **Decision:**
  - The single-village guards built in the MGNREGA all-villages QA (KI-049 to KI-059) are gated
    on `_village_scheme(schemes)`: exactly one scheme, and it is MGNREGA or Focus Plus. Those are
    the two facts that carry `village_code` on every row.
  - Focus Plus adds three things of its own:
    - `_focusplus_narrow_village`: prefer villages that hold Focus Plus data;
    - `_focusplus_pin_village_where`: rebuild a single-village WHERE on the resolved code;
    - `_focusplus_alias_district_collision`: a district HQ alias that is also a village is
      asked about.
  - The twin-village "(LGD code)" chip tag applies to both schemes and fires only on a real
    same-name, same-block pair.
  - Other schemes (CM Elevate, Focus Legacy, CM Elevate Legacy) are unchanged. PMAY-G joined the
    gate on 2026-09-28 (D-029, KI-096).
- **Reason:** the Focus Plus run failed on exactly the shapes those guards fix (52/301 before);
  duplicating them per scheme would drift. `collides_across_dimensions` keeps its "a district hit
  settles it" rule because MGNREGA's 13,298/13,298 run depends on it; the alias case is handled
  in the Focus Plus flow only.
- **Alternatives rejected:**
  - Copying the MGNREGA functions for Focus Plus: two copies drift apart.
  - Relaxing the district rule in `entity_resolver`: it would change MGNREGA and every other
    scheme.
  - Prompt rules for the 30B: the model wrote block/district filters beside the resolved code on
    most tries.
- **Status:** Active. Live 2026-09-27:
  - Focus Plus blocks 204/204 and villages 7,026/7,026, with a confirmation pass of 204/204 and
    1,000/1,000;
  - MGNREGA regression sample 448/448 and 300/300.

### D-029 — PMAY-G: fixed-shape questions answered by a deterministic facts path; PMAY-G joins the village gate
- **Date:** 2026-09-28 (`app/pipeline.py`, `app/entity_resolver.py`, `app/premise_check.py`,
  `app/schema_context.py`, `data/pmay/pmay_few_shot.yaml`, `data/pmay/pmay_entity_resolver.yaml`;
  PMAY-G use-case QA and all-blocks / all-villages run, KI-089 to KI-097).
- **Decision:**
  - `_pmay_facts_query` recognises the use-case shapes (houses / beneficiaries, sanctioned,
    released, remaining, completed, incomplete, stage breakdown, full / partial / no release,
    fully paid but incomplete, unique sanction numbers, completion %, release %, financial
    summary, performance, A-vs-B comparison) for one village / block / district / the state or a
    list of named areas, optionally in one FY or on one date. It builds ONE parameter-bound
    query over `curated.v_pmay` and `_pmay_facts_answer` writes the answer in code (exact ₹ with
    Indian grouping plus lakh / crore; differences and the higher area for comparisons). No SQL
    model, no composer. It runs after the scope / year gates, and authorisation runs on its SQL.
  - Everything else (rankings, "each district", trends, allotment, instalments, averages, lists,
    "in progress", villages counted) stays on the model path, which gains
    `_pmay_sql_issue` (per-year GROUP BY on a whole-period question; AVG-based utilisation),
    `_pmay_comparison_limit` (LIMIT 1 on N named areas), prompt rules 7–9 and three few-shots,
    and `_pmay_rupee_format` (bare rupee cells → ₹ lakh / crore).
  - An area with no rows falls back to the model path (a misspelt place can never read as a
    zero), except a village resolved by LGD code, which gets a stated zero
    (`_pmay_no_houses_answer`).
  - PMAY-G joins `_village_scheme` / `_VILLAGE_FACT_SCHEMES` (D-028): `_pmay_village_names`
    narrows village resolution to villages PMAY-G holds, the chip / level / longest-name guards
    and the alias-district check apply, and MANIPUR / BURMA are villages.
- **Reason:** the 30B failed 11 of 28 use cases on sampling alone (per-year GROUP BY, AVG rates,
  LIMIT 1, dropped parts, 2-decimal crore), and two of those flipped between identical runs. The
  shapes are closed and one view answers all of them, the same situation as D-025 (MGNREGA fixed
  shapes). The village failures (727 / 5,120) were the exact shapes D-028 fixed for Focus Plus.
- **Alternatives rejected:**
  - Prompt rules alone: the project rule is that prose fails under sampling (CLAUDE.md §5).
  - Post-composition patching of the model's answer (as `_focusplus_answer_guarantees`): the
    per-year SQL returns the wrong rows, so there is nothing correct to patch from.
  - A PMAY-only copy of the village guards: copies drift (D-028).
- **Status:** Active. See TESTING.md for the live numbers.


### D-030 — Context relevance gate and a deterministic semantic contract before SQL runs
- **Date:** 2026-09-29 (`app/context_policy.py`, `app/pipeline.py`, `app/edge.py`,
  `app/entity_resolver.py`, `app/premise_check.py`, `app/context_manager.py`,
  `app/session_store.py`, `app/context_budget.py`; KI-030, KI-032, KI-034, KI-130 to KI-135).
- **Decision:**
  - A previous answer is context only for a message that shares something with it
    (`context_policy.continuation_signals`). Without a signal, the edge whitelist applies and no
    follow-up rewrite runs. The existing merge plan (D-022) is unchanged and runs only after
    this gate.
  - The contract between entity resolution and SQL is enforced in code, not only in the prompt:
    resolved district / block / comparison list / year and a stated Focus Plus payment amount
    must be in the SQL (`_resolved_scope_missing`, `_focusplus_stated_amount_missing`), or the
    existing repair loop runs. These sit before the 4B verifier.
  - An input the system cannot pin is asked about, never dropped: a letter-swapped district
    acronym, a Focus Plus amount it never pays, a bare "which one?".
  - Name searches are answered before entity resolution (`_village_name_search_answer`).
  - Every such decision logs one `pipeline_decision` line (labels only, no text).
- **Reason:** each reported failure was an input silently dropped or silently inherited, and
  the project rule is that prose rules fail under sampling (CLAUDE.md §5). Every check is
  deterministic, adds under 1 ms per turn (measured), and makes no model call.
- **Alternatives rejected:**
  - A 4B "is this related?" classifier call: an extra model call on every turn, and it is the
    same model whose rewrite glued the scheme on.
  - A full JSON semantic contract generated by a model and SQL parsed against it: a rewrite of
    working code (CLAUDE.md §4). The resolved-entities dict already is the contract; what was
    missing was enforcement.
  - Adding "WHK" as an alias: not in any SME catalogue (CLAUDE.md: never invent aliases).
  - A stated-amount guard for every scheme: only Focus Plus has SME-confirmed per-row amounts.
- **Amendment (same day, live):** the thread a follow-up continues is chosen per follow-up
  (`_followup_thread_state`, `_data_thread_antecedent`), never by wiping state on a knowledge
  turn: a wipe broke the pinned rule that a knowledge digression does not overwrite the DATA
  thread (`tests/test_context_manager.py` §6), while the per-follow-up rule satisfies both it and
  the reported "what is focus" conversation. Scheme vocabulary in the follow-up outranks the
  antecedent; a bare "Focus" is never given to the model rewrite.
- **Status:** Active. Live-verified 2026-09-29 (see TESTING.md).

### D-031 — Focus Legacy per-place breakdowns are written from the rows, not composed
- **Date:** 2026-09-29 (`app/pipeline.py`: `_focus_legacy_breakdown_answer`,
  `_focus_legacy_unplaced_row`, `_focus_legacy_answer_guarantees`; KI-145, KI-147, KI-150).
- **Decision:** a Focus Legacy result with one place column (district, block, village or
  constituency, aliases accepted) plus 1-3 numeric columns and at most 100 rows is answered by a
  fixed template: one line per place, the sum across the places, and the records with no place
  recorded (read from the data by re-running the query without the model's `IS NOT NULL`). A
  `LIMIT`-cut top-N list gets no total. The composer is not called for these shapes. Other shapes
  still go to the composer, followed by the month / rupee guarantees.
- **Reason:** the answer is fully determined by the rows. In the all-districts run the composer
  dropped the per-block counts for one district and called 5 blocks "5 producer groups" for another,
  which no prose rule reliably prevents (CLAUDE.md §5). Sums are exact because every pg_id
  sits in exactly one district, block and village (verified 2026-09-29: 0 groups span two).
- **Alternatives rejected:** a composer note (already tried for the no-block figure; written in
  words or dropped); a per-row faithfulness check with retry (adds a model call and still cannot
  catch a correct number attached to the wrong noun).
- **Revisit if:** a reload lets one pg_id span two places — the "together" sum would then over-count.

### D-032 — Focus Legacy: a duplicate producer group is a group paid more than once
- **Date:** 2026-10-05, confirmed 2026-10-07 (`app/pipeline.py` `_focus_legacy_duplicate_groups_answer`,
  `_focus_legacy_answer_notes`; `data/focus_legacy/focuslegacy_few_shot.yaml`; KI-187).
- **Decision (product owner):** a producer group paid more than once is a duplicate: 2,655 of 11,906 statewide
  (2,647 paid twice, 8 three times), reported without a year split. A district / block / village / year in the question
  narrows the rows first. The answer is written from a parameter-bound query and lists the groups with their payment years.
- **History:** supersedes the 2026-09-25 TC-12 reading ("repeat payments, not duplicates; duplicate records = 0").
  On 2026-10-06 the owner asked for "paid more than once in the same year" (7: FY 2022-23 = 1, FY 2025-26 = 6); on
  2026-10-07 the owner restated "the duplicates are 2,655" and chose "2,655, no year split".
- **Unchanged:** "duplicate payments / records" (same pg_id, same date) is a separate question and keeps the model path.
- **Alternatives offered and not chosen:** a year-of-repeat breakdown (8 / 22 / 37 / 2,592, overlapping); the same-year
  subset; same name + same village under different IDs (25 sets / 50 groups).

### D-033 — A bare "Focus" means Focus Legacy
- **Date:** 2026-10-10 (`app/pipeline.py` `_pin_bare_focus`, applied in `_run_pipeline` beside
  `_pin_cm_elevate_dataset`; KI-217, closes KI-212).
- **Decision (product owner):** when a question names only "Focus" (any case), it is Focus Legacy, on the DATA and the
  KNOWLEDGE paths. The question is rewritten once, before routing, so every later check sees "Focus Legacy" and the
  answer carries the visible `rewritten_question`. Focus Plus needs "Focus Plus" / "Focus+".
- **Supersedes:** CLAUDE.md §4 "a bare Focus must never be guessed — ask which Focus" and the which-Focus pause
  (`_focus_ambiguity_clarification`), which stays in the code but no longer fires for a pinned question.
- **Untouched:** "Focus Plus" / "Focus+" / "Focus Legacy" mentions, "focus" as an English word (`_FOCUS_AS_NOUN`),
  "Focus" inside a producer-group name (KI-157). A bare "FOCUS" beside "Focus+" in a cross-scheme question is pinned
  too, so Focus Legacy is no longer dropped silently (KI-212).
- **Risk accepted:** an officer who meant Focus Plus but typed only "Focus" now gets Focus Legacy figures; the
  rewritten question shows "Focus Legacy", so the reading is visible.


### D-034 — Cross-scheme comparisons are answered from fixed per-scheme queries
- **Date:** 2026-10-10 (`app/pipeline.py` `_cross_scheme_compare_plan`, `_cross_scheme_compare_answer`,
  `_cross_scheme_compare_data`; KI-213 … KI-228; report `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md`).
- **Problem:** the officers' 20 cross-scheme use cases ("beneficiary count across MGNREGA, PMAY-G, Focus+
  and FOCUS", "district-wise summary", "performance in <district>", "which scheme has the widest coverage")
  failed 11 of 20 live on model SQL — a branch that lost its district filter, "0 crore" from scheme codes
  the money view does not hold, a total added across lakh-, rupee- and DBT-money, Focus Legacy
  "beneficiaries" reading 102,021 or 11,906 by wording, 8,627 blank rows, a "highest" that was not, and
  verifier rejections of the prescribed shape. Prose rules for all of these were already in
  `schema_context.py` `_CROSS_SCHEME`; sampling skipped them.
- **Decision:** the same pattern as `_cross_scheme_money_answer` and the PMAY-G facts path. A comparison
  across schemes is answered from one parameter-bound query per scheme and measure — each the figure that
  scheme's own question returns — shown side by side with each unit named and never added together. With
  no year, each scheme's whole data window is used and stated (MGNREGA households: the latest year only).
  It runs in `_answer_data` before the which-scheme / scope / year pauses, and `classify_intent` keeps it
  on DATA.
- **Measures:** MGNREGA households employed (latest FY) / total_exp ÷ 100 (lakh → crore); PMAY-G houses and
  amount_released, `NOT is_placeholder`; Focus Plus `COUNT(DISTINCT beneficiary_key)` / amount_disbursed;
  CM Elevate `COUNT(DISTINCT request_id)`, **no money** ("no money recorded"); Focus Legacy memberships
  (`SUM(no_of_pg_members)`) in producer groups (`COUNT(DISTINCT pg_id)`) — never "people"; CM Elevate
  Legacy records / total_disbursement; NRLM members in SHGs / RF + CIF (cumulative). Coverage = villages
  (Unresolved placeholder off village counts only) and districts.
- **Scope, deliberately narrow:** two or more schemes named and not only MGNREGA + PMAY-G (those keep the
  sanctioned cross-scheme views and their model path), or none named with "across the schemes" / "each
  scheme" / "which scheme"; a beneficiary / money / coverage / performance / summary ask; no specific year;
  no single-scheme measure (person-days, tranche, status, gender, sub-scheme, …); after resolution at most a
  district, a district list or one block. Anything else returns None and runs the normal path unchanged.
  An unnamed statewide money ranking still uses `_cross_scheme_money_answer`.
- **Also:** the model path keeps working for every other wording, now with deterministic guards
  (`_cross_scheme_sql_issue`, `_verifier_join_complaint_on_aggregates`, `_range_claim_misstated`,
  `_cross_unit_total_stated`).
