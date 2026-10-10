# Change report — last 10 days (2026-09-29 to 2026-10-09)

**Subject:** the six schemes only — MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy,
CM Elevate Legacy. Every context layer. NRLM (the seventh scheme, onboarded 2026-10-06) is
excluded, except where a change it caused also runs in code the six schemes execute (§6).

Generated 2026-10-09.

---

## 1. Summary

Two commits in the window, and a large uncommitted tail.

| Commit | Date | Subject |
|---|---|---|
| `f38ea1b` | 2026-09-29 06:52 | Scheme QA fixes, context state hardening, and project docs (2026-09-26..29) |
| `36b7427` | 2026-10-07 01:40 | Scheme QA fixes, NRLM onboarding scaffolding, and architecture docs |

The 10-day window has two distinct phases:

- **2026-09-29** — the single densest six-scheme day in the project. Three sessions landed
  **~50 fixes** (KI-020, KI-030/032/034, KI-125/127/129, KI-130…KI-168) plus **two decisions**
  (D-030, D-031), and committed the whole context-state architecture.
- **2026-10-01 → 10-07** — four focused sessions: CM Elevate zero-programme and pending/unique,
  Focus Legacy duplicates and FY comparison, PMAY-G beneficiary counting.

**Totals for the six schemes:** ~56 issues fixed, 3 decisions recorded (D-030, D-031, D-032) plus
one amendment (D-027), and the test baseline moved 1,185 → 1,498 pytest.

**Two things are open and need you:** the evidence-deletion decision (§7) and the fact that
**five fixed six-scheme issues are not deployed** (§8).

---

## 2. Context layers — what changed, layer by layer

This is the part you asked for most directly. The six schemes' context handling was rebuilt on
2026-09-29 and committed in `f38ea1b`.

### 2.1 New modules (all committed `f38ea1b`)

| Module | Lines | Role |
|---|---|---|
| `app/context_policy.py` | 640 new | the relevance gate, merge plans, provenance checks |
| `app/context_budget.py` | 184 new | token budgeting for what context reaches a prompt |
| `app/premise_check.py` | 109 new | stated-amount / premise extraction before SQL |
| `app/session_sync.py` | 109 new | Postgres-backed state sync (D-023) |
| `app/session_store.py` | +102 | per-worker cache |
| `app/context_manager.py` | +421 | state commit / clear / inherit |
| `app/conversation_store.py` | +29 | versioned `context_state` persistence |

### 2.2 D-030 — context relevance gate and a deterministic semantic contract (2026-09-29)

The central context decision of the window. Four rules, all deterministic, under 1 ms per turn,
no model call:

1. **A previous answer is context only for a message that shares something with it**
   (`context_policy.continuation_signals`). Without a signal, the edge whitelist applies and **no
   follow-up rewrite runs**. The D-022 merge plan is unchanged and runs only after this gate.
2. **The entity-resolution → SQL contract is enforced in code, not only in the prompt.** Resolved
   district / block / comparison list / year, and a stated Focus Plus amount, must appear in the
   SQL (`_resolved_scope_missing`, `_focusplus_stated_amount_missing`) or the repair loop runs.
   These sit **before** the 4B verifier.
3. **An input the system cannot pin is asked about, never dropped** — a letter-swapped district
   acronym, a Focus Plus amount it never pays, a bare "which one?".
4. **Name searches are answered before entity resolution** (`_village_name_search_answer`).

Every such decision logs one `pipeline_decision` line (labels only, no text).

**Amendment the same day:** the thread a follow-up continues is chosen **per follow-up**
(`_followup_thread_state`, `_data_thread_antecedent`), never by wiping state on a knowledge turn.
A first version *did* wipe, and it broke the pinned rule that a knowledge digression must not
overwrite the DATA thread (`tests/test_context_manager.py` §6). Scheme vocabulary in the follow-up
outranks the antecedent, and **a bare "Focus" is never given to the model rewrite.**

Rejected alternatives worth remembering: a 4B "is this related?" classifier (an extra call on every
turn, from the same model whose rewrite caused the bug); a model-generated JSON contract with SQL
parsed against it (a rewrite of working code — the resolved-entities dict already *is* the
contract, what was missing was enforcement); adding "WHK" as an alias (not in any SME catalogue).

### 2.3 Context bugs fixed 2026-09-29 — the conversation layer

| KI | Symptom | Fix |
|---|---|---|
| KI-030 | an "all years" choice not carried forward, so the year pause was asked again | `ConversationState.year_all` + `context_manager.inject_year_scope` |
| KI-032 | a merge plan's CLEAR not applied — an old block survived a district change and was inherited later | `update_state` drops fields the plan CLEARs/REPLACEs; a question with no plan resets geography and year |
| KI-034 | SQL dropped **all** mandatory resolved entities on a fallback fragment and the verifier passed it → a statewide all-years figure | `_resolved_scope_missing` repair guard, before the verifier |
| KI-130 | after a scheme answer, an unrelated message ("who is harshit") was rewritten "…under Focus Plus" and answered | `continuation_signals` gates the edge relaxation, the rewrite **and** the KNOWLEDGE scheme fallback |
| KI-131 | "now give me five thousand loan for me i am in crisis" became a Focus Plus DATA question | new edge kind `personal_request` |
| KI-134 | a bare "Which one?" was rewritten and run instead of asked | `is_bare_reference` → `reference-ambiguous` pause (deliberately not remembered) |
| KI-136 | after a CM Elevate Legacy turn, "what is focus" became "What is the focus of the CM Elevate Legacy scheme in FY 2024-25?" — the scheme name read as a noun, which-Focus never asked | `_names_bare_focus_scheme` skips the model rewrite; `_FOCUS_AS_NOUN` |
| KI-137 | a rewritten how-it-works question carried the previous DATA turn's year and place, so the docs answered "not listed for East Khasi Hills" | `_drop_inherited_time` + `_drop_inherited_place` on the KNOWLEDGE path (a **typed** year/place is kept) |
| KI-138 | "give me beneficiaries" rewritten to a disbursement question — the follow-up's own metric replaced, and provenance passed it | `rewrite_violation` now requires the follow-up's own metric family in the rewrite |
| KI-139 | a KNOWLEDGE turn on another scheme left the old scheme's year/metric/places feeding the next follow-up | per-follow-up `_followup_thread_state` (see the D-030 amendment) |
| KI-140 | after a KNOWLEDGE answer, "give me beneficiaries" was paraphrased and answered from the docs | `_measure_after_knowledge` + `_typed_intent` |
| KI-141 | a paused follow-up was remembered as the typed fragment, so a typed year resumed without the scheme | `turn_context["standalone_question"]`, read first by `routers.query.pause_question` |
| KI-142 | "and all of them combined?" after "what about 2022-23?" kept the last year | `ConversationState.last_dimension` + `_ALL_OF_THEM_RX` CLEAR; `_all_years_rewrite` |
| KI-143 | "and in West Garo Hills?" after two KNOWLEDGE follow-ups continued the documents question, not the figure | `_data_thread_antecedent` — a place/year-only change after a knowledge answer continues that scheme's last DATA turn |
| KI-144 | "and person-days?" after "who is eligible for PMAY-G?" was pinned to PMAY-G and answered **"19,058 person-days" from a house count** (found by the session's own live battery, before release) | `_measure_after_knowledge` defers to scheme vocabulary |

### 2.4 Context work in October (2026-09-29 → 10-07, committed in `36b7427`)

- **KI-180 — scheme swap by request verb.** "give me for pmay" after an MGNREGA person-days
  question returned a PMAY-G *scheme description* (RAG). Worse, every swap carrying a measure only
  the old scheme holds ended in "couldn't build a working query" after 4 rejected SQL attempts.
  Fixed: `_SCHEME_SWAP_FOLLOWUP` accepts a request-verb opening, `_SUBSTITUTION_CUE` added, and new
  `_swap_measure_gap` / `_SCHEME_OWN_MEASURES` pause with the **new** scheme's own measures for the
  same scope as chips — no guess. "tell me about pmay" is still KNOWLEDGE.
- **KI-181 — typed replies now resume every chip pause.** Before, only scope/year/entity/ranking
  and which-scheme pauses were remembered; any other pause with chips (measure gap, year out of
  range, tranche, region, AC part, Sericulture, CM sub-scheme group, stated amount) **forgot its
  question** unless a chip was clicked. Fixed in `routers.query.remember_pause` (now remembers
  every rule with options) plus `pipeline._resume_option_pause` and `_paused_thread_antecedent`
  (an unmatched reply stays on the paused scheme).
  *Still open:* an unmatched reply with a measure and place but no year keeps the paused scheme but
  not the paused year.

---

## 3. Per-scheme changes

### MGNREGA
- **KI-148** — a constituency named with no scheme pinned MGNREGA, because `_MGNREGA_ONLY_TERMS`
  still held "assembly constituency" from when only MGNREGA had one. "total amount disbursed for
  Baghmara assembly constituency" **failed on all 55 ACs**. Fixed 2026-09-29: `_mgnrega_terms`
  ignores the AC term alone, and `_scheme_clarification` offers only the three schemes that have
  constituencies. Verified 0/55 → 55/55.
- Shared village guards gained scheme coverage (KI-153, KI-162 — see §3.5/§6).

### PMAY-G
- **KI-125 — a bare year is the CALENDAR year.** Revised 2026-09-29 after a user report: the FY
  figures had been leading while the calendar ones sat in a note, so the table disagreed with the
  question. Now "during 2017" → calendar 2017 in the answer, table **and** SQL (110,890 for 2023),
  with the FY reading as a one-line note (106,527). An explicit FY is unchanged.
- **KI-127 — beneficiaries include zero-sanction records.** Decided and fixed 2026-10-03: a
  beneficiary is **every** record (LASKEIN 5,251; Ri Bhoi 17,565; state 171,107). Every other
  figure — houses sanctioned, money, stages, release counts, sanction numbers — **still excludes**
  them. This resolved a long-open product question against the tester's own 14 Sep remark.
- **KI-129 — the result table showed the facts path's whole 30-column working row**
  (`fnd_st_plinth`, `sanctioned_positive`, …) and the Sources line read "TRUE, PMAY-G", because the
  UI takes the word after every `FROM` and the SQL said `IS DISTINCT FROM TRUE`. Fixed:
  `_pmay_display_rows` returns only the place plus the asked figures, the SQL uses
  `NOT COALESCE(is_completed, FALSE)`, and the UI skips TRUE/FALSE/NULL.
- **KI-128** — PMAY-G answers carried extra information (house counts and release % appended to a
  financial summary, side details on single figures, follow-ups repeating the previous figure).
  Fixed PMAY-G-only; the shared rewrite untouched.
- **Open:** KI-126 (a shared village name asks village *then* year — two taps, tester wants one).

### Focus Plus
- **KI-132 — a stated amount silently dropped from SQL.** "for a loan of five thousand" produced
  `SELECT SUM(amount_disbursed) FROM curated.v_focus_plus` — **₹119.74 cr for all payments instead
  of ₹46.64 cr for the ₹5,000 ones.** Fixed 2026-09-29 with `premise_check.stated_amount_filters`,
  the `_focusplus_stated_amount_missing` repair guard, an `amount-not-held` pause, and a "not a
  loan" composer note. Focus Plus only — it is the one scheme with SME-confirmed per-row amounts.
- **KI-064** — a tranche or batch **is** the scope a question pins, so asking "which area?" first
  only added a click. `_needs_scope_clarification` returns False for a tranche/batch question.
- `FocusPlus_KI065_KI084_Response.md` (66 lines) committed in `f38ea1b`.

### CM Elevate
- **KI-182 — a named programme with 0 applicants is now stated as 0.** The week's largest
  single-scheme QA effort. "How many applicants are there in [district] under [P1] and [P2]?" on
  **every ordered pair: 12 districts × 15 × 14 = 2,520 questions.** When one programme had 0 in the
  district, `GROUP BY scheme_name` returned one row and the answer **named only the other
  programme** — no 0, no combined total. Before: 1,131 FAIL. After: **2,520/2,520 PASS**
  (one-zero pairs were 13/1,144 the same morning).
  Reworked **twice from your screenshots**: both-programmes-zero returned an empty result and said
  "I couldn't find any matching records" (now `_cme_zero_programmes_answer`, live 282/282); then
  the one-zero appended line read as a stub (now `_cme_zero_breakdown_answer` rebuilds the whole
  answer, one line per programme in the question's order, live 1,426/1,426).
  Guard scope is deliberately narrow: plain `COUNT`, exact programme literals, no HAVING/OR/JOIN,
  no LIMIT that could drop a group, and the place confirmed by the app's own bound lookup.
- **KI-186 — "pending at level N" and "unique" redefined** (D-027 amendment, 2026-10-05).
  Your screenshot showed 8,372 — every level-1 file counted as pending, including **64 Rejected and
  1 Approved** — and "unique" ignored (`COUNT(*)`, not distinct). You first said
  `current_file_status`; that column holds no pending value, so the options were put to you and you
  chose **`current_level = 'levelN'` AND `file_status = 'Pending'`**. Level 1 = **8,307**,
  level 2 = 165, level 0 = 0. Plain "pending" with no level stays `data_verified = 'On Hold'`.
  Live: 871/871 pending questions. Found on the way: case-folded district/level literals, a 4B
  verifier false check-2, and a prompt-wording regression on plain "pending" (4 of 6 runs filtered
  the wrong column).
- **Open, reported not fixed at your instruction (2026-10-05):** **KI-184** — "Meghalaya Sports &
  Wellness Centre Scheme" named in full still asks "which scheme?" in **12/12 districts**; no other
  programme does. **KI-185** — status-distribution wording when a programme is absent from a
  district (61 cases say "couldn't find any matching records" instead of 0), one answer omits the
  programme name, one adds a stray "mean of 355.00".
- **Open:** KI-124 (applicants = distinct `request_id` vs the sheet's applications — needs a
  definition decision).

### Focus Legacy
The most-worked scheme of the window. **D-031** plus ~25 fixes.

- **D-031 — per-place breakdowns are written from the rows, not composed.** A result with one place
  column (district/block/village/constituency, aliases accepted), 1–3 numeric columns and ≤100 rows
  is answered by a fixed template: one line per place, the sum across places, and the records with
  no place recorded. The composer is **not called** for these shapes. Reason: in the all-districts
  run the composer dropped the per-block counts for one district and called **5 blocks "5 producer
  groups"** for another — no prose rule reliably prevents that. Sums are exact because every
  `pg_id` sits in exactly one district, block and village (verified: 0 groups span two).
  *Revisit if* a reload lets one `pg_id` span two places.
- **D-032 / KI-187 — a duplicate producer group is one PAID MORE THAN ONCE.** Settled with the
  product owner over three days: **2,655 of 11,906** (2,647 twice, 8 three times; 5,318 of 14,569
  payment records), **no year split**; a district/block/village/year in the question narrows the
  rows first. Written from a parameter-bound query. Supersedes the 2026-09-25 TC-12 reading
  ("repeat payments, not duplicates; duplicate records = 0"); the 2026-10-06 same-year-only count
  (7) was **withdrawn**. "Duplicate payments/records" (same pg_id, same date) remains a separate
  question on the model path.
- **KI-183 — the hedge guard discarded a correct comparison.** Reported on the deployed server:
  "Compare total remittances between FY 2023-24 and 2024-25" → "couldn't build a working query".
  **Not reproducible on repo code** (20/20 in-process, the exact 4-turn conversation through the
  real `/api/query` as the same user, and both the `f38ea1b` and `7064ab6` builds). But a real
  defect was found in the same flow: the composer's correct comparison was thrown away because the
  sentence "The scheme has no data for FY 2023-24" matched `_HEDGE_RE`. Fixed —
  `compose_response` ignores sentences naming a year the resolver's gap note says is absent; a
  hedge anywhere else still trips the guard. 5/5 live.
- **KI-020 — the PG alternate-spelling view is finally used.** `_focus_legacy_pg_name_answer`
  searches current names **and** earlier spellings together (`v_focus_legacy_pg_search`); an older
  spelling equal to another group's current name lists both, and the answer says the typed name is
  an earlier spelling. This closed a long-standing PLANNED item.
- **KI-164 — month breakdowns were one month early.** `DATE_TRUNC('month', date_of_remittance)`
  returns timestamptz in Asia/Kolkata and the driver returns UTC, so 1 Apr 2022 00:00 IST arrived
  as 31 Mar 18:30 UTC and the answer said **"₹5.20 crore in March 2022"** when the truth was April.
  Fixed for Focus Legacy (`_focus_legacy_date_trunc_as_date` casts to `::date`).
  **Other schemes NOT VERIFIED** — PMAY-G's schema notes show `date_trunc('month', sanction_date)`
  and may share it. Worth checking.
- **The rest of the 2026-09-29 all-levels run** (32,560/32,560 questions): KI-145 (per-block
  breakdown silently dropped PGs with no block and called the listed sum the total), KI-146
  (constituency drill-down read MGNREGA's `v_employment` for *every* scheme), KI-147 (totals
  printed as "54205000.00", months as "month 4"), KI-149 (219 of 9,452 group names unparseable),
  KI-150 (composer breakdown errors), KI-152 (edge refused group names holding a state/country
  word — "Rakkam China Banana Group"), KI-154, KI-156 (a duplicate-name village chip looped for
  ever), KI-157 (a PG named "Focus Bibari" triggered the which-Focus gate), KI-158 (`_` in a name
  defeated the whole-word match), KI-159, KI-160, KI-161, KI-162, KI-163 (same-name villages in one
  block — the wrong twin was answered), KI-165.

### CM Elevate Legacy
All from the 2026-09-29 use-case re-test and all-blocks/all-villages run:

- **KI-166** — "How many applications have been sanctioned?" answered **2,823**; DB and raw give
  **2,820** (5 of 5 runs). 3 Refused records have no sanctioned amount; the rule existed only as a
  prompt rule.
- **KI-167** — "…each village in Tikrikilla block" said "20 villages each have 1 record"; the truth
  is **24**, and the 6 no-village records went unmentioned. The composer sees only the first 40 of
  42 rows.
- **KI-168** — "Which schemes have the highest number of applications?" cut to **10 of 13** schemes
  and said the lowest was 33 (true: Motorcaravan **1**). An unrequested `LIMIT 10`; the top-N pause
  knows districts/blocks/villages, not schemes.
- **KI-169** — a twin-village chip (DOMBAGRE) re-asked "which DOMBAGRE?" for ever, because the
  KI-156 chip-tail pin ran for five schemes but not this one.
- **KI-170** — "Nongthymmai" (10 registry villages): chips showed 5, the Jirang village holding the
  records was not among them, and the answer was **0**.
- **KI-171** — a true ₹0 disbursement written "under ₹0.01 crore"; ₹62,500 and ₹1,25,000 both read
  "₹0.01 crore".
- **KI-172** — place names misspelled by the composer: "Sellsella" for SELSELLA (3 of 3 runs),
  "Mawsynrut" for MAWSHYNRUT — though the resolved names were known exactly.
- **KI-173** — William Nagar (MB) wards answered 0 / "couldn't build a query" (true: 1 record).
- **KI-174, KI-177** — counts listed "… respectively" with no names.
- **KI-175** — "Tura Municipal Board" refused as "not in Meghalaya".
- **KI-176** — the composer **invented a subsidy split** when only a total was in the result.
- **KI-178, KI-179** — village-name and Garo-range gaps.

---

## 4. Decisions recorded

| ID | Date | Decision |
|---|---|---|
| **D-030** | 2026-09-29 | Context relevance gate + deterministic semantic contract before SQL (§2.2) |
| **D-031** | 2026-09-29 | Focus Legacy per-place breakdowns written from the rows, not composed |
| **D-032** | 2026-10-05, confirmed 10-07 | Focus Legacy: a duplicate producer group = paid more than once (2,655) |
| **D-027 amendment** | 2026-10-05 | CM Elevate: pending at a level = `current_level` + `file_status 'Pending'`; unique = distinct |

---

## 5. Documentation

`f38ea1b` committed the documentation set largely wholesale: `docs/AI_PIPELINE.md` (+1,181),
`docs/ARCHITECTURE.md` (645 changed), `docs/KNOWN_ISSUES.md` (+1,092), `docs/DECISIONS.md` (+499),
`docs/HANDOFF.md` (+426), `docs/SCHEMES.md` (+330), `docs/DATA_MODEL.md` (+313), `docs/TESTING.md`
(+241), `CLAUDE.md` (+204), plus `docs/Context_Validation_Report_2026-09-26.md` (+1,022) and three
`*_DB_Issues.md` files for MGNREGA, Focus Plus and Focus Legacy.

`36b7427` added:
- **`docs/COMPLETE_ARCHITECTURE.md`** (1,066 lines) — the whole system in one document, reconciled
  against **source, not docs**; every count re-verified. Added to the CLAUDE.md §3 map.
- **`tools/verify_wiring.py`** (525 lines).
- Stale facts corrected on the way: `pipeline.py` is **15,214** lines, not "about 8,400" (CLAUDE.md
  §2 and ARCHITECTURE.md §1 both updated).
- Conflicts recorded rather than silently fixed (new doc §20.1): the `pipeline.py` docstring still
  justifies the no-framework choice by "2 schemes"; `data/pmay/README.md` §9 still says PMAY is not
  in `megh_db`; the `SCHEMA_FOR_DEVELOPERS.md` the scheme READMEs cite is not in this repo.

Uncommitted: **`docs/SCHEME_ONBOARDING_RUNBOOK.md`** (471 lines) replaces the lost
`SCHEME_ONBOARDING_HANDOFF.md`, and `docs/SCHEMES.md` gained a **15th registry** entry — see §6.3.

---

## 6. Shared layers touched for NRLM that the six schemes also run

Strictly out of your scope as *NRLM work*, but **in scope by blast radius**: these execute on
six-scheme turns, so they are this window's regression surface for the six. All uncommitted.

### 6.1 Additive only — VERIFIED unchanged for the six

Read directly from `app/pipeline.py`. In each case NRLM was **appended** and the six schemes'
entries are byte-identical:

| Symbol | Value now |
|---|---|
| `_VILLAGE_NARROW_SCHEMES` | `("Focus Plus", "PMAY-G", "CM Elevate")` — **unchanged** |
| `_VILLAGE_EXACT_NAME_SCHEMES` | `_VILLAGE_NARROW_SCHEMES + ("NRLM",)` — new tuple |
| `_VILLAGE_CODE_SCHEMES` | `("MGNREGA", "NRLM")` — MGNREGA unchanged |
| `_ONE_FIGURE_SCHEMES` | `(["Focus Plus"], ["CM Elevate"], ["NRLM"])` — first two unchanged |

Two shared regexes widened permissively: `_GEO_BESIDE_VILLAGE_RE` also strips
`block_lgd_code` / `district_lgd_code` (including a quoted value in an integer column, which
crashed Postgres), and `_PLACE_PREP_RE` now admits `&` between words in a name — which also
benefits the five Focus Legacy-relevant ampersand villages from KI-162.

### 6.2 Genuinely changed shared behaviour — two items

1. **`_parse_year_key`.** An **explicit** `NNNN-NN` range is now read **from 1900 on**, because the
   pair itself is unambiguous. A **bare** four-digit number stays pinned to **2010–2039 on
   purpose**, so "top 2000 villages" and "the 1984 census" are still never read as financial years.
   For the six this only means an explicitly-written pre-2010 FY now parses instead of erroring, and
   `_year_in_data_range` then correctly refuses it. The guard that matters is unchanged.
   Also: a digit token the question introduces as an **identifier** (`code`/`id`/`no.`/`number`
   immediately before the digits) is skipped — relevant to any scheme with numeric IDs.
2. **The village scan backstop gate** moved from `_stated_level is None` to
   `in (None, "village")`. **This runs for the six schemes too.** The argument: saying "village" is
   the strongest possible signal a village is wanted, and the old gate assumed the extractor had
   already taken the village path when it had not. Covered by `tests/test_village_block_narrowing.py`
   (40 tests); the full suite was re-run because all the fixes sit in the shared village path.

### 6.3 A 15th registry you should know about

`_UNSUPPORTED_SCHEME` is a registry, and it is **not** among the 14 in
`docs/SCHEMES.md §Adding a scheme` — because it is a **removal, not an addition**, which is why it
was missed when NRLM joined. The result was a scheme being refused in the same reply that offered
its own chip, looping for ever.

This matters for the six because the **four coverage replies in `app/edge.py` are shared and
user-visible on every out-of-scope, greeting and out-of-area turn, whatever scheme is asked
about** — `_OUT_OF_SCOPE_REPLY`, `greeting`, `identity` and `_out_of_area_reply` (which also said
"these six schemes", now "seven"). All four were rewritten.

Structural guard added: `tests/test_supported_scheme_not_refused.py` walks `SCHEME_CATALOG` and
every alias in `_SCHEME_NAME_PATTERN`, fails if any alias of a loaded scheme sits in
`_UNSUPPORTED_SCHEME`, and asserts **no refusal chip can resume into another refusal**. Both
`docs/SCHEMES.md` and the new runbook now record the registry.

Also shared: `entity_resolver._ACRONYM_VOCABULARY` now exempts `GP`/`GPS`/`AC`/`ACS` (gram
panchayat, assembly constituency) from district acronym near-miss matching. **The WHK → West Khasi
Hills behaviour KI-133 added is unchanged.**

---

## 7. Test baseline, and one gap

| Point | pytest | Scripts | Live |
|---|---|---|---|
| 2026-09-29 (KI-136…144) | 1,185 | 14/14 | context suite 43/43; battery S1–S8 |
| 2026-09-29 night (Focus Legacy all levels) | 1,265 | 14/14 | 43/43; use cases 28/28; bulk **32,560/32,560** |
| 2026-10-03 (KI-183) | 1,377 | 14/14 | 20/20 + the 4-turn conversation; Focus Legacy 28/28 |
| 2026-10-05 (KI-182) | 1,405 | 14/14 | **2,520/2,520** ordered pairs |
| 2026-10-05 (KI-186) | 1,433 | 14/14 | 871/871 pending; OFF-009 sample 150/150 |
| 2026-10-07 (`36b7427`) | **1,498** | 14/14 | not re-run (no code changed) |

**The gap:** `tests/live_context_validation.py` (43/43 on 2026-09-26 and 09-27) has **not been
re-run since the §6.2 shared-layer changes.** CLAUDE.md §7 requires it after any routing, rewrite or
state change, and the village-backstop gate and year parser both qualify. **This is the one
outstanding verification for the six schemes.** Needs the VPN.

**Also outstanding — your decision.** 762 QA evidence screenshots, 9 `.xlsx` test reports and
`docs/CM_Elevate_Legacy_DB_Issues.md` are deleted in the working tree and were deliberately left
out of `36b7427`, because `CURRENT_STATE.md` and `KNOWN_ISSUES.md` still cite those folders as the
evidence for specific CM Elevate and CM Elevate Legacy KI numbers. Either retire the evidence (and
update the citing docs in the same commit) or restore it (`git checkout HEAD -- docs/`). This is a
six-scheme decision.

---

## 8. Deployment — five fixed six-scheme issues are not live

**VERIFIED 2026-10-06:** `115.124.102.167:8300` still gave the old "0 duplicate payment records"
answer for Focus Legacy. Everything after `f38ea1b` was uncommitted at that point, so none of it
was deployed. The local `:8300` (127.0.0.1 only, `--reload`) runs current code and is **not** what
that address serves.

Related and still unexplained (KI-183): a Focus Legacy FY comparison failed on the VM but answered
correctly in every repo reproduction, including the committed `f38ea1b` and `7064ab6` builds. The VM
therefore runs different code or configuration — **UNKNOWN — NEEDS VERIFICATION** (VM logs for
14:19:43 UTC).

**Not live: KI-182, KI-183, KI-186, KI-187/D-032, KI-127.** Redeploy from this repo, then re-test
those five.

---

## 9. Next steps

1. **Commit the uncommitted work** — ~1,500 lines and 5 test files are unprotected. `36b7427`
   already showed the cost of letting a tree accumulate for nine days.
2. **Decide the evidence-deletion question** (§7) — it blocks a clean tree.
3. **Re-run `tests/live_context_validation.py`** (§7) — the only outstanding six-scheme check.
4. **Redeploy and re-test the five fixes** (§8).
5. **Confirm whether KI-184 / KI-185** (CM Elevate) should now be fixed — both are open only
   because you asked for a test.
6. **Check whether KI-164's timezone bug affects PMAY-G** — its schema notes show
   `date_trunc('month', sanction_date)` and the fix was Focus Legacy-only.
7. Settle the two open definition questions: **KI-124** (applicants vs applications) and
   **KI-126** (PMAY-G two-tap village+year UX).

---

## 10. Evidence labels

- **VERIFIED:** the commit list and timestamps (`git log --since`); file deltas
  (`git show --stat`, `git diff --numstat`); the current values of the four scheme tuples in §6.1
  (read in `app/pipeline.py`); the `edge.py` and `entity_resolver.py` diffs; the decision dates in
  `docs/DECISIONS.md`; the issue rows in `docs/KNOWN_ISSUES.md`; the test progression in
  `docs/TESTING.md`.
- **INFERRED:** the deployed VM running different code (KI-183).
- **UNKNOWN — NEEDS VERIFICATION:** the VM's actual code/config; whether the six schemes pass
  `live_context_validation.py` after §6.2; whether KI-164's timezone bug affects PMAY-G.
