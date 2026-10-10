# SCHEME ONBOARD — NRLM (National Rural Livelihoods Mission)

Running work log for onboarding **NRLM** as the **seventh** scheme of Megh One AI.
Every coding step is appended here as it is made, in the order it was made, with the
file and symbol touched and why. This file is the step-by-step record; the durable
project docs (`docs/SCHEMES.md`, `docs/CURRENT_STATE.md`, …) are updated at the end.

- **Started:** 2026-10-06
- **Scheme canonical label:** `NRLM`
- **Data folder:** `data/NRLM/` (7 SME YAMLs + README, authored 2026-10-05, v1.1)
- **Query surface:** `curated.v_nrlm` (29 columns, one row per SHG), lineage fact
  `curated.fact_nrlm_shg`, reconciliation `meta.v_reconciliation_nrlm`
- **Owner:** MSRLS (Meghalaya State Rural Livelihoods Society), C&RD Department

---

## 0. Ground rules taken from the existing code

Read before touching anything (per `CLAUDE.md` §8 "Before work"):

- `CLAUDE.md`, `docs/CURRENT_STATE.md`, `docs/SCHEMES.md` (§Adding a scheme),
  `docs/DATA_MODEL.md`, `docs/AI_PIPELINE.md`, `docs/DECISIONS.md`.
- `docs/SCHEME_ONBOARDING_RUNBOOK.md` and `docs/HANDOFF.md` are referenced by
  `CLAUDE.md` §3 but **do not exist in the repo**. The authoritative procedure used
  instead is `docs/SCHEMES.md` §"Adding a scheme", which enumerates 14 registries and
  states its symbols were VERIFIED. Logged as a documentation conflict in §99 below.
- Evidence order honoured: source code > DB schema > tests > config > docs. The SME
  YAMLs in `data/NRLM/` are the contract for scheme facts; the code is the contract
  for how a scheme is wired.

### The three NRLM facts that shape every registry entry

These come from `data/NRLM/README.md` and `nrlm_schema_partitions.yaml`, and they are
the reason several entries below differ from the other six schemes:

1. **One row is one SHG.** `COUNT(*)` counts groups, never people. Members are a
   column: `SUM(total_members)` / `female_members` / `male_members`.
2. **The only year is the year the SHG was FORMED**
   (`formation_financial_year_short`, `1984-85` … `2022-23`). There is no reporting
   year, no release date, no extract-date column.
3. **Money is cumulative and undated.** `revolving_fund_amount` (RF) and `cif_amount`
   (CIF) hold what each SHG has received in total, in rupees. They add across SHGs but
   **cannot be placed in a year** — filtering money by the formation year returns a
   different figure that looks exactly like the one asked for. This is the single
   highest-risk wrong-number path in the scheme.

### Environment note

The office VPN was **not** connected during this session, so `megh_db`, Qdrant and the
model gateway at `10.48.242.4` were unreachable (`WinError 121`, semaphore timeout, on
`db.fetch_rows`). Consequences, carried through to §99:

- Column names, row counts and stored values below are taken from the SME YAMLs and
  `data/NRLM/README.md` §7 (the CSV→view column map), **not** read from the live DB.
- `refresh_scheme_years()` will overwrite the NRLM default year list at startup from
  the live view, so a wrong default is self-correcting there.
- Live `pipeline.answer_question` checks and the live context suite are listed as
  REMAINING WORK, not claimed as done.

---
## Step 1 — `app/annotations.py` (registry 2 of 14)

Added `"NRLM"` to the three dicts the few-shot / FK loader walks:

| Symbol | Value | Note |
|---|---|---|
| `_SCHEME_DIRS` | `_DATA_PART / "NRLM"` | folder is **upper-case** on disk, unlike the six lower-case siblings; commented so nobody "tidies" it |
| `_FEW_SHOT_FILE` | `nrlm_few_shot.yaml` | one bank only (103 pairs). No `*_prompt_few_shots.yaml` exists for NRLM, so there is no v1/v2 trap like CM Elevate Legacy's |
| `_FK_FILE` | `nrlm_foreign_key_augmentation.yaml` | 20 nodes / 23 edges, 16 prohibited |

Checked first that the NRLM YAMLs carry the keys these loaders read — they do:
`sql_generation_examples` (few-shot), `foreign_key_augmentation` (FK), and the same
`schema_version / datasets / semantic_rules / nlp_sql_rules / few_shot_examples`
top-level set as `pmay_schema_partitions.yaml`. No loader change was needed.

NRLM ships no `common_mistakes`, `answer_shots`, `ranking_stop_words`,
`refusal_score_factor` or `max_refusal_shots`, so it gets the default ranking
behaviour `(1.0, None)` — the same as the five schemes other than CM Elevate Legacy.

## Step 2 — `app/entity_resolver.py` (registry 3 of 14)

1. `_RESOLVER_FILE["NRLM"] = data/NRLM/nrlm_entity_resolver.yaml`.
2. `_AC_CONTENTS_SQL["NRLM"]` — NRLM is the **first AC-capable scheme that needs no
   `dim_geography` join**: `v_nrlm` carries `constituency_name_raw` on the row.
   Predicate: `UPPER(constituency_name_raw) = UPPER($1) AND entity_type <> 'Unresolved'`.
3. `AC_CONTENTS_SOURCE["NRLM"] = "NRLM Self Help Group register"`.

### The one loader change this scheme did require — `_DIMENSION_ALIASES`

`load_all()` reads a fixed set of dimension names
(`district`, `block`, `year`, `assembly_constituency`, `tranche_label`, `cm_scheme`).
`nrlm_entity_resolver.yaml` names two of its dimensions differently:

| NRLM YAML name | Name the module reads | Values |
|---|---|---|
| `constituency` | `assembly_constituency` | 55 |
| `formation_year` | `year` | 30 |

Both carry the identical `canonical` + `aliases` value shape the module already
parses (verified field-by-field against MGNREGA's `assembly_constituency`), so the
fix is an alias entry, not new parsing code — exactly the precedent CM Elevate
Legacy set with `cm_scheme: ("scheme",)`.

**Why this mattered rather than being cosmetic:** without the
`assembly_constituency` alias NRLM's 55 constituencies resolve to nothing, and a
question naming one silently falls through to the **block** of the same name.
26 NRLM block names are also constituency names and they cover different areas
(Mawkyrwat: 1,106 SHGs as a block, 1,092 as a constituency) — so the failure mode
is a plausible wrong number, not an error.

**Verified** by loading all seven resolvers in-process:

```
MGNREGA     {'district': 12, 'block': 56, 'year': 4,  'assembly_constituency': 56}
PMAY-G      {'district': 12, 'block': 56}
Focus Plus  {'district': 12, 'block': 50, 'tranche_label': 4}
CM Elevate  {'district': 12, 'block': 58, 'cm_scheme': 15}
Focus Legacy{'district': 12, 'block': 56}
CM El Legacy{'district': 12, 'block': 59, 'cm_scheme': 13}
NRLM        {'district': 12, 'block': 56, 'year': 30, 'assembly_constituency': 55}
```

The six existing schemes' counts are **unchanged** by the alias addition (compared
before and after), because none of them defines a `constituency` or `formation_year`
dimension. NRLM's 12/56/55/30 match `data/NRLM/README.md` §1 exactly.

## Step 3 — `app/schema_context.py` (registry 4 of 14)

This is the block that becomes the **SQL model's prompt**, so it is where the scheme's
wrong-number risks have to be made deterministic rather than left to prose elsewhere.

Four edits:

1. **`SCHEME_CATALOG["NRLM"]`** — the one-paragraph identity. States the grain (one row
   per SHG, 40,629), that `COUNT(*)` is groups and never people, that the only year is
   the formation year, and that money is cumulative and undated.
2. **`SCHEME_METRICS["NRLM"]`** — 14 bullets, used to tell a user their question asked
   for something the data does not track. Includes the explicit NOT-HELD bullets
   (savings / bank linkage / loans, federations, member-level detail, formation date,
   households, targets, fund utilisation) because those are the NRLM questions most
   likely to be asked and most dangerous to answer with a substitute figure.
3. **`_NRLM_TABLES` / `_NRLM_RULES` / `_NRLM_VOCAB`** — new blocks, matching the house
   style of `_CMELEVATELEGACY_*` (numbered rules, "breaking these produces a wrong
   number", a `->` vocabulary table).
4. **`_SCHEME_BLOCKS["NRLM"]`** — registers the triple.

### What the 17 NRLM rules encode (and why each is there)

| # | Rule | The failure it prevents |
|---|---|---|
| 1 | Grain: `COUNT(*)` = SHGs; members are `SUM(total_members)` | counting 40,629 groups as 410,847 people — ~10x understatement of members |
| 2 | The only year is the FORMATION year; `'Pre-Nrlm'` is a type, not a year | 432 typed Pre-Nrlm vs 645 formed before 2011-12 — the sets differ |
| 3 | **Money is cumulative and undated** | the scheme's top risk: filtering money by formation year returns a *different, plausible* figure. No trend, no growth, no utilisation. Cohort view only on request, aliased `*_held_cr` |
| 4 | Units: `/1e7` crore (state/district), `/1e5` lakh (block/village), rupees per SHG | mixing NRLM rupees with MGNREGA lakh |
| 5 | Zero is recorded, not NULL; show both averages | 73.3% hold no CIF, so one average hides the other |
| 6 | `is_active` NOT NULL; all 1,197 inactive SHGs show 0 funds (NR-15) | reporting a structural zero as a funding finding |
| 7 | The 2,032 Unresolved-village SHGs stay in district/block totals, out of village ones | excluding them under-reports by 5%; constituency sums to 38,597 not 40,629 |
| 8 | Village identity is `village_code` | 197 names belong to >1 village |
| 9 | District/block UPPERCASE; EASTERN WEST KHASI HILLS (740) ≠ WEST KHASI HILLS (279) | a mixed-case filter returns zero rows **silently** |
| 10 | `constituency_number_raw` is `'36 MAWKYRWAT'`, never cast; `LIKE '36 %'` | `'3 %'` matching `'36'`; block vs same-name constituency confusion |
| 11 | `gp_name` is advisory free text, not an LGD level | treating GP as a geography level (differs from LGD village on ~23-28% of rows) |
| 12 | `shg_name` is display only; `shg_code` is the key | 'Iatreilang Shg' is 295 different SHGs |
| 13 | LIMIT discipline; RF's 10 distinct values make "top 5 by RF" meaningless | an arbitrary 5 cut out of a large tie |
| 14 | Year series groups pre-2014-15, ordered by `MIN(...)` not the label | `'before 2014-15'` sorting last (digits sort before letters) |
| 15 | Quality flags mark real rows; CIF > 5 lakh unconfirmed (NR-25); rupee unit assumed (NR-07) | silently dropping real rows, or stating an unconfirmed max as fact |
| 16 | No row-level cross-scheme join; aggregate per scheme in its own CTE | SHG grain x house grain multiplies both sides |
| 17 | The NOT-HELD list | substituting RF/CIF for savings or loans — the most likely NRLM mistake |

### Verified

```
catalog: [MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy, CM Elevate Legacy, NRLM]
blocks : [MGNREGA, PMAY-G, Focus Plus, CM Elevate, Focus Legacy, CM Elevate Legacy, NRLM]
build_schema_context(['NRLM'])   -> 19,232 chars; sections = SHARED TABLES, NRLM TABLES,
                                    NRLM RULES, NRLM BUSINESS VOCABULARY, SHARED RULES
build_schema_context(['PMAY-G']) -> 10,636 chars; 'v_nrlm' NOT present  (no regression)
SCHEMA_CONTEXT (all 7)           -> 117,710 chars
```

Scope is correct in both directions: an NRLM question sees only the shared blocks plus
NRLM's own, and an existing scheme's context does not gain NRLM text.

## Step 4 — `app/pipeline.py` (registry 5 of 14) — 18 edits in 4 passes

### 4a. Naming and spelling

| Symbol | Entry |
|---|---|
| `_SCHEME_NAME_PATTERN["NRLM"]` | `nrlm`, `n.r.l.m`, `day-nrlm`, `nrlm shg`, `aajeevika`/`ajeevika`/`ajivika`, `msrls`, `srlm`, "national rural livelihood(s) mission", "state rural livelihoods society/mission" |
| `_SCHEME_FUZZY_ALIASES["NRLM"]` | `aajeevika`, `ajeevika`, `nrlmshg`, `msrls` |
| `_SCHEME_CANONICAL_SPELLING["NRLM"]` | `"NRLM"` |

Two deliberate omissions, both commented in the code:

- **A bare "SHG" / "self help group" is NOT in the name pattern.** That is the *unit*
  this scheme counts, not its name — and CM Elevate also accepts SHG applicants (as an
  `applicant_category` value). Putting it in the name pattern would turn "How many CM
  Elevate applications came from SHGs?" into a two-scheme question. It is handled as
  *vocabulary* in `_NRLM_ONLY_TERMS` instead, which only fires when no scheme is named.
- **`"nrlm"` is not in the fuzzy aliases.** `_fuzzy_named_schemes` only considers words
  of 5+ letters, so a 4-letter alias is unreachable — listing it would be dead weight.
  The longer spoken forms, which *can* be typo'd, are listed.

### 4b. `_NRLM_ONLY_TERMS` + `_infer_scheme_from_terms`

New regex, registered in the `_infer_scheme_from_terms` tuple. Included: the scheme
names, `self help group(s)` / `shg(s)` / `shg code|name|type|members`, `revolving
fund` / `rf amount`, `community investment fund` / `cif`, `pre-nrlm`, `revived
shgs/groups`, `formation year|financial year|fy`, `year of formation`,
`formed in FY/19xx/20xx`.

**Excluded on purpose** (each would create a false positive):
bare `members` / `women` (Focus Legacy counts PG memberships, Focus Plus pays individual
members); bare `fund` / `funds` / `amount` (five schemes hold money); bare `active` /
`inactive` (generic status words).

### 4c. Years — and a design decision I reversed mid-way

My first draft listed only the dense tail (`2014-15`…`2022-23`) as NRLM's years, on the
reasoning that offering `1984-85` as a chip invites a one-SHG answer.

**That was wrong and I changed it.** `_SCHEME_DATA_YEARS` feeds *two* consumers: the
year chips **and** the out-of-range guard (`_year_out_of_range_clarification`, see the
comment at the guard). A truncated list makes the guard refuse *"SHGs formed in
2008-09"* — a real question with a real answer. Correctness of the guard outranks chip
tidiness, so the **full 26-value span** held in the register is listed, and the
per-year SQL groups the sparse years as `'before 2014-15'` (schema_context rule 14).

Also added the NRLM branch to `refresh_scheme_years()`, probing
`formation_year_key` from `curated.v_nrlm` — so chips and guard track the live data,
and my hand-written list is only the startup default.

### 4d. Everything else in pipeline.py

| Symbol | Entry / note |
|---|---|
| `_AC_CAPABLE_SCHEMES` | `+ "NRLM"` — 4th AC-capable scheme |
| the KI-148 AC-specific scheme ask | now offers NRLM (otherwise an AC question can never reach it) |
| the generic "which scheme?" ask | NRLM option + "Compare across schemes" text, **patched in both places** the block appears verbatim |
| measure-swap offers | SHGs / members / "funds received to date" — the fund offer is worded as a cumulative total, never with a year |
| scheme blurb | says "one row is one GROUP, not one person" and "a snapshot, not a yearly series" |
| `_SCHEME_FIT["NRLM"]` | "joining a women's Self Help Group" — for the recommender |
| `_CROSS_SCHEME_MONEY_SQL` | own `UNION ALL` branch, `RF + CIF`, `/1e7`; semantics string says *"cumulative with no year"* |
| `_SCHEME_DISPLAY_NAME`, `_MEASURE_PLAIN` | NRLM worded as a **stock, not a flow**, so the money ranking cannot read as "NRLM spent this much this year" |

### Verified — routing, in-process

```
infer=['NRLM']    How many SHGs are there in East Khasi Hills?
infer=['NRLM']    How many self help groups are active?
infer=['NRLM']    What is the total Revolving Fund received by SHGs?
infer=['NRLM']    total CIF by district
infer=['NRLM']    How many SHGs were formed in 2020-21?
infer=['NRLM']    SHGs in Mawkyrwat constituency
named=['NRLM']    aajeevika groups in Ri Bhoi        (fuzzy/alias path)
```

No regression, and no hijacking of the SHG word:

```
named=['PMAY-G']      How many PMAY-G houses were sanctioned?
infer=['MGNREGA']     total person-days in 2024-25
named=['CM Elevate']  How many CM Elevate applications were received?
named=['CM Elevate']  How many CM Elevate applications came from SHGs?   infer=None
named=['Focus Plus']  Focus Plus disbursements to SHG members            infer=None
```

The last two are the case the `_NRLM_ONLY_TERMS` design had to get right: the question
names its scheme, so `_named_schemes` wins and `infer` correctly reports ambiguity
rather than dragging the question to NRLM.

## Step 5 — `app/followups.py` (registry 6 of 14)

| Symbol | Entry |
|---|---|
| `_SCHEME_RX["NRLM"]` | mirrors the pipeline name pattern (no bare "SHG" — this map answers *which scheme was named*) |
| `_primary_schemes` fallback | `+ "NRLM"` |
| `_nrlm_data()` | new builder |
| dispatch in `build_followups` | `elif primary[0] == "NRLM"` |
| `_KNOWLEDGE_LADDER["NRLM"]` | what is NRLM / who can join / what funds / how a group is formed |

`_nrlm_data()` carries two scheme-specific constraints, documented in its docstring:

1. **Never offer money beside a year.** Every fund offer is worded "received to date"
   and omits the `gsuf` year phrase; the formation-year offer is a **count** offer. An
   offer like "RF in FY 2021-22" would advertise the one question the scheme must refuse.
2. **Never offer a trend.** One snapshot, no history — so no "over the years", no growth.

Verified in-process (`build_followups` with a real GROUP BY):

```
Q: How many SHGs are there in East Khasi Hills?
   -> How many SHG members are there in East Khasi Hills?
   -> Break that down by block within East Khasi Hills.
   -> How many SHGs are there of each type — New, Revived and Pre-NRLM in East Khasi Hills?
Q: What is the total Revolving Fund received by SHGs?      (money asked -> count offered back)
   -> How many SHGs have received the Revolving Fund in East Khasi Hills?
knowledge -> Who can join an NRLM Self Help Group? / What funds ... / How is a new SHG formed ...
```

## Step 6 — `app/edge.py` (registry 7 of 14)

| Symbol | Entry |
|---|---|
| `_DOMAIN_WORDS` | `nrlm`, `day-nrlm`, `aajeevika`, `ajeevika`, `msrls`, `srlm`, `self help group` (3 spellings), `\bshgs?\b`, `revolving fund`, `\brf\b`, `community investment fund`, `\bcif\b`, `pre-nrlm`, `rural livelihood` |
| `_SCHEME_ALIASES["NRLM"]` | scheme-name forms + `self help group` |
| `_SCHEME_CAPABILITY["NRLM"]` | says the figures are a snapshot and the funds cannot be split by year |
| `_SCHEME_STARTERS["NRLM"]` | 5 chips — **no money-by-year chip**, by design |

`_DOMAIN_WORDS` matters more than it looks: without it *"how many shgs are there?"* has no
domain word and the edge layer **bounces it as off-topic** before routing ever runs.
Short forms are word-anchored (`\bshgs?\b`, `\brf\b`, `\bcif\b`) following the existing
`\bpgs?\b` precedent — an unanchored `"rf"` would match inside unrelated words.

Verified:

```
has_domain_vocabulary:  True  how many shgs are there? / what is nrlm? /
                              total revolving fund by district / community investment fund in Ri Bhoi /
                              self help groups formed in 2020-21 / aajeevika groups
                        False tell me a joke / what is the weather today      (still rejected)
_named_scheme:          NRLM        how many NRLM shgs are there? / self help groups by district
                        CM Elevate  CM Elevate applications from SHGs      (correctly NOT NRLM)
                        Focus Legacy producer groups paid
detect_edge_case(NRLM data question) -> None                 (passes through to the pipeline)
```

## Step 7 — registries 8–13

| Registry | File | Entry |
|---|---|---|
| 8 | `app/prompt_builder.py` | `_scheme_of()` NRLM branch (`"nrlm" in t`), docstring, and NRLM added to the live-schema scoping tuple |
| 9 | `app/schema_introspect.py` | `_SUBJECT_TO_SCHEME` (`nrlm`, `day-nrlm`, `shg`, `livelihood(s)`), `_TABLE_NAME_TO_SCHEME` (`nrlm`, `shg`) |
| 10 | `app/kb_ingest.py` | `_SOURCES` + the 2 NRLM docs; `_CANONICAL_SCHEME` NRLM spellings; filename guess placed **above** the Focus fallbacks |
| 11 | `app/rag.py` | `_SCHEME_DOC_NAMES["NRLM"]` — tells the composer NRLM / DAY-NRLM / Aajeevika are one scheme. **No `_KB_SCHEME_ALIAS` entry**: NRLM is its own programme with its own KB, unlike CM Elevate Legacy which folds into CM Elevate |
| 12 | `app/auth.py` + `app/users.yaml` | NRLM added to **all 8** roles' `schemes`, and to the users.yaml comment |
| 13 | `app/config.py` | `ASR_PROMPT` now names NRLM and adds "Self Help Groups (SHGs), Revolving Fund, CIF" so dictated NRLM questions transcribe |

Table ownership verified — no collisions:

```
v_nrlm -> NRLM                      v_cm_elevate            -> CM Elevate
fact_nrlm_shg -> NRLM               v_cm_elevate_disbursement-> CM Elevate Legacy
v_pmay -> PMAY-G                    v_focus_legacy          -> Focus Legacy
v_employment -> MGNREGA             dim_geography           -> shared
```

All 8 roles carry 7 schemes; `rag._SCHEME_DOC_NAMES` has 7 entries; `kb_ingest._SOURCES`
has 12 docs including both NRLM files. The **"NRLM" label appears 17 times** in
`NRLM_Complete_Reference.md` and **18** in `NRLM_FAQ.md` — the check SCHEMES.md requires,
because the composer reports "not covered" when the label is absent from the docs.

## Step 8 — `web/ai_query.html` (registry 14 of 14)

Scheme card (`data-scheme="nrlm"`, matching the `CARD_QUESTIONS.nrlm` key), `.nrlm-card`
CSS tint + icon colour (the class I first used had no rule), hero subtitle and composer
placeholder in **all 4 locales**, the greeting, `CARD_QUESTIONS.nrlm` (6 chips, no
money-by-year chip), `prettyName` mappings for `v_nrlm` / `fact_nrlm_shg`, and the source-chip
scheme list.

**A figure I had to correct:** my first draft put "₹101.80 Cr" on the card. The README's own
totals table (line 532) gives RF **50.86 Cr** and CIF **98.48 Cr** = **₹149.34 Cr**. Fixed.
Card stats now read 40,629 SHGs · 4,10,847 members · ₹149.34 Cr RF+CIF received to date,
all three traceable to `data/NRLM/README.md`.

## Step 9 — tests: 7 pinned-roster failures, each read before changing

The first full run gave **1,440 passed, 7 failed**. Every failure was a test asserting a
hard-coded roster of 5 or 6 schemes — the category `CLAUDE.md` §7 warns about ("some tests
pin exact source strings… read it before 'fixing' it"). In each one the **production code
was correct** and the expected value was stale:

| Test | Was | Now | Why |
|---|---|---|---|
| `test_ac_focus_legacy::test_the_capable_set_is_exactly_the_schemes_with_a_route` | 3 AC schemes | 4 | NRLM holds constituency data |
| `test_focus_legacy_usecase_fixes::test_ki148_...` | 3 AC options; 7 generic options | 4; 8 | same, plus the generic ask grew |
| `test_cm_elevate_legacy::test_scheme_listing_...` | 5 bullets | 6 | 7 schemes list as 6 programmes (the CM Elevate pair still collapses to one — the point of that test, still passing) |
| `test_followup_scheme_scope` (3 tests) | 4/5-scheme rewrite strings | + NRLM | the "remaining schemes" rewrite; grammar ("Focus Legacy and NRLM") produced correctly by the code |
| `test_scheme_pick::test_it_keeps_moving_on...` | 5 pick phrases | 6 | compares against `_pickable_schemes()`, which **already** returned NRLM — only the number of turns was short |

That last one is worth noting: its right-hand side was already correct, which is positive
evidence the registries are mutually consistent rather than needing a hand-matched literal.

While fixing it I first used "yet another one" as the 6th phrase; `_is_scheme_pick_request`
rejected it (`_PICK_DELEGATION` needs a verb like "pick"), so the turn silently repeated
Focus Legacy. Replaced with "pick another one", which the test's own `PICK` list already
declares valid.

### Results

| Suite | Baseline (2026-10-05) | Now |
|---|---|---|
| pytest, 26 files | 1,436 passed | **1,447 passed, 0 failed** |
| plain scripts, 14 files | 14/14 | **14/14** |
| `tests/live_context_validation.py` | 43/43 | **NOT RUN** — needs the VPN (see §99) |

## Step 10 — new regression suite `tests/test_nrlm_onboarding.py` (51 tests)

CLAUDE.md §7 requires every fix to have a test calling the **real function**, never a
re-implementation. The suite is grouped by the risk each test protects:

| Group | Tests | What it pins |
|---|---|---|
| 1. registry completeness | 9 | all 14 touchpoints carry NRLM; the 7 YAMLs + README exist on disk; both KB docs registered **and** containing the literal "NRLM"; NRLM has its own KB (no `_KB_SCHEME_ALIAS`); all 8 roles; ASR prompt; the resolver loads 12/56/55/30; the AC query needs no `dim_geography` join |
| 2. routing | 24 | 9 NRLM phrasings infer NRLM; 5 name forms recognised; 4 other schemes not dragged in; **3 that the SHG word must not hijack**; edge layer accepts NRLM vocabulary and still rejects off-topic; the scheme ask and the AC ask both offer NRLM; `v_nrlm`/`fact_nrlm_shg` ownership |
| 3. the money/year trap | 6 | the prompt forbids money-in-a-year and carries the `*_held_cr` cohort escape; catalog + metrics say the funds have no year; **no follow-up chip and no starter chip pairs money with a year or a trend**; the cross-scheme row is labelled a stock; the formation-year span is complete so the out-of-range guard cannot misfire |
| 4. the grain trap | 3 | `COUNT(*)` is groups and members are `SUM(total_members)`; the `COUNT(DISTINCT shg_name)` undercount; the village / Unresolved / 38,597 rules; the NOT-HELD list forbids calling the funds savings or loans |
| 5. no regression | 8 | NRLM's prompt block is scoped to NRLM; each of the six existing schemes gains no NRLM text; CM Elevate Legacy still collapses into one pickable programme |

Two self-corrections while writing it, both my error rather than the code's:

- `test_the_nrlm_prompt_block_is_scoped_to_nrlm_only` first asserted that
  `curated.v_pmay` never appears in an NRLM context. It does — `_PREAMBLE` uses it as the
  generic "schema-qualified table" example for **every** scheme. Rewritten to assert on the
  section **headers** (`PMAY-G RULES` etc.), which is what "scoped" actually means.
- `few_shot_examples()` takes a **list** of schemes; I first passed the bare string
  `"NRLM"`, which iterated characters and returned nothing. The registry was fine.

## Step 11 — verification runs

### Startup and loading

```
app.main imports OK; 11 routes
NRLM few-shots loaded : 103      NRLM FK graph loaded: True     NRLM IDF tokens: 131
shots per scheme: MGNREGA 68 · PMAY-G 66 · Focus Plus 87 · CM Elevate 156 ·
                  Focus Legacy 86 · CM Elevate Legacy 132 · NRLM 103
```

### Few-shot ranking (IDF, per scheme)

Each question retrieves its own exact match first, which is the behaviour the ranking
exists for:

```
"How many SHGs are there in East Khasi Hills?" -> that exact example, then two other
                                                  district/region examples
"total revolving fund by district"             -> RF-by-district examples
"How many women are members of SHGs?"          -> that exact example, then all-women SHGs
"villages with the most SHGs"                  -> that exact example, then village rankings
```

### Test results

| Suite | Baseline (2026-10-05) | After onboarding |
|---|---|---|
| pytest, **27** files (was 26) | 1,436 passed | **1,498 passed, 0 failed** |
| plain scripts, 14 files | 14/14 | **14/14** |
| `tests/live_context_validation.py` | 43/43 | **NOT RUN** — needs the VPN |

The +62 are the 51 new NRLM tests plus 11 cases added by parametrised roster tests that
now cover a seventh scheme.


---

## Step 12 — live use-case QA (2026-10-07, a later session; no code changed)

The live checks this log listed as REMAINING WORK were run. The VPN was up, so `megh_db`, Qdrant
and the gateway were all reachable.

**Result: 16 of 43 use cases passed, 27 failed.** Report with per-case proof images:
`NRLM_UseCase_Test_Report_2026-10-07.xlsx` (repo root). Issues opened: **KI-188 to KI-196**.

### What the run confirmed about the onboarding

- The wiring works. NRLM is classified, routed, SQL is generated against `curated.v_nrlm`, and
  the composer answers — the 16 passes include the statewide counts, the membership totals, the
  per-year breakdown (30 of 30 years), the peak year, the top block, the top constituency and the
  top CIF recipient, each exact against both the raw CSV and the DB.
- **The §0 column facts were right.** Every value this log took from the SME YAMLs instead of the
  live DB has now been read from `curated.v_nrlm` and matches: 40,629 rows, 12 districts,
  56 blocks, 30 formation years, 2,032 unresolved villages, 1,197 inactive SHGs all at 0 CIF/RF
  (NR-15). The `volumes_verified: false` caveat in `nrlm_schema_partitions.yaml` can be lifted.
- **The raw file and the database agree exactly** on 34 scalar metrics and on the district, block,
  year, constituency and village breakdowns. All 27 failures are pipeline behaviour.

### What it found that the offline onboarding could not

- **KI-189, the one to fix first.** The token `SHG` resolves to the district **South Garo Hills**
  (alias `SGH`, `nrlm_entity_resolver.yaml:420`) and raises an entity-ambiguous pause. Step 2 of
  this log added the entity-resolver entries and could not have caught this: it only appears in
  the full pipeline, against the live district catalogue. Every NRLM question contains "SHG", so
  it blocks 6 of the 43 use cases. Reproduced 10/10.
- **KI-191.** The `critical` refusals this log describes in Step 2 —
  `money_in_a_year_requested` and `money_trend_or_growth_requested` — **are not firing**. UC37
  presented cumulative CIF as a year-on-year trend with no caveat, which §0 fact 3 names as the
  scheme's single highest-risk wrong-number path. The rules exist in
  `nrlm_classification_rules.yaml`; they need a deterministic guard behind them (CLAUDE.md §5).
- **KI-190.** The NR-07 rupee-unit risk is real: money is emitted as a bare number with the unit
  dropped ("…is 98.48" for ₹98.48 crore).
- **KI-188, KI-192, KI-193, KI-194, KI-195** are shared pipeline behaviours rather than NRLM
  wiring faults, though NRLM is where they were measured.

### Still remaining

- `tests/live_context_validation.py` — still **NOT RUN** for NRLM. It should be re-run once
  KI-189 is fixed, since the pause it causes would distort any multi-turn result.
- No code was changed in this session, so the 2026-10-07 baseline (pytest 1,498 passed over
  27 files, scripts 14/14) still stands.

## Step 13 — the fixes, and NRLM signed off (2026-10-07, same session as step 12)

All eight defects step 12 found were fixed the same day and the use cases re-run live on the
final code: **43 / 43**. Details and the symbol for each fix are in KNOWN_ISSUES.md
(KI-188 … KI-195, "The fixes"); regression tests are `tests/test_nrlm_usecase_fixes.py` (71).

What this closes from the earlier steps of this log:

- **Step 2's entity-resolver work** had one gap it could not have seen offline: the token
  `SHG` resolving to the district **South Garo Hills** (KI-189). Fixed in
  `entity_resolver._ACRONYM_VOCABULARY`.
- **Step 2's `critical` refusal conditions** (`money_in_a_year_requested`,
  `money_trend_or_growth_requested`) were never read by any code. They now have the
  deterministic guard the project requires: `_nrlm_money_year_caveat` (KI-191).
- **§0 fact 3's "highest-risk wrong-number path"** is now guarded in code, not just in prose.
- **§0 fact 1 (one row = one SHG)** turned out to be something the SQL *verifier* did not know:
  it demanded an aggregate on a per-SHG money column. `_verifier_nrlm_grain_complaint_is_false`
  teaches it the grain.
- **`volumes_verified: false`** in `nrlm_schema_partitions.yaml` can now be lifted: every
  volume in this log has been read from the live view and matches (40,629 SHGs, 12 districts,
  56 blocks, 30 formation years, 2,032 unresolved villages, 1,197 inactive all at 0 CIF/RF).

### Still remaining

- `tests/live_context_validation.py` — still **NOT RUN**. KI-192 touched a clarification path
  and KI-193 touched the DATA→KB fallback, so the multi-turn suite should be re-run to confirm
  them (43/43 is the baseline). This is the single outstanding item for NRLM.
