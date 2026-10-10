# Cross-Scheme Use Cases — Test Report (2026-10-10)

*The 20 cases in `Cross Scheme Test Cases.csv`. Raw-source figures and a static
pipeline audit are complete; the DB and bot columns are **not run** — see §1.*

---

> **SUPERSEDED IN PART.** The DB and bot columns were run later the same day with the VPN up:
> `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md` (4 PASS / 16 FAIL). That report also
> corrects this one's Focus Plus beneficiary figure, PMAY-G house count, and closes KI-216.

## 1. Run status — READ THIS FIRST

The request was a three-way comparison: **raw vs DB vs bot**. Only the raw column and a
static code audit were possible in this session.

| Column | Status | Why |
|---|---|---|
| **Raw** (source CSV/XLSX) | **DONE**, all 6 schemes | Local files, no network |
| **DB** (`megh_db.curated`) | **NOT RUN** | `10.48.242.4:5432` unreachable |
| **Bot** (`pipeline.answer_question`) | **NOT RUN** | Same host serves every model endpoint |

`10.48.242.4` hosts Postgres, Qdrant **and** the vLLM gateway (`app/config.py:128,168,175,185,214,227`).
Ping: 2 sent, 0 received, 100% loss. Ports 5432/6333/8000/8001 all time out. The Fortinet VPN
is down, so no live answer could be generated and no DB figure read.

**What this report therefore is:** the raw baseline every DB and bot figure must be checked
against, plus **five defects proven from the source code alone**, which need no VPN to
establish. Three are wrong-answer defects that would not have shown up as errors.

**What it is not:** a pass/fail verdict per case. That needs the live re-run in §6.

---

## 2. The finding that frames all 20 cases

**"Beneficiary count" is not a defined quantity across these schemes, and two of them cannot
answer the money questions at all.** Each scheme's row means something different
(`app/schema_context.py` `SCHEME_METRICS`, VERIFIED):

| Scheme | One row is | "Beneficiary" reading | Money |
|---|---|---|---|
| MGNREGA | a village-year summary | households/persons employed (**per year, never summed**) | **lakh ₹** |
| PMAY-G | one house | houses sanctioned | ₹ |
| Focus Plus | one **payment** (member × tranche) | `COUNT(DISTINCT beneficiary_key)` | ₹, unit unverified |
| CM Elevate | one **application** (a request, not an award) | applications — *proves nobody was funded* | **NONE AT ALL** |
| Focus Legacy | one **payment to a producer group** | groups, or memberships — **no person record exists** | ₹, = memberships × 5,000 |
| CM Elevate Legacy | one applicant's sanction | sanction-and-disbursement records | ₹ |
| NRLM | one **SHG** (a group) | `SUM(total_members)`, never `COUNT(*)` | RF+CIF, **cumulative, no year** |

Consequences for the test set as written:

1. **CROSS-4, 5, 10, 11, 19, 20 ask CM Elevate for money it does not have.** Not a hard
   question — *impossible*. The correct answer must say so, not print a 0.
2. **CROSS-1, 2, 6, 17, 19, 20 ask Focus Legacy for a beneficiary count.** Focus Legacy has
   **no person record of any kind**. Only groups (11,906) or memberships (102,021).
3. **No two schemes share a full year window** (`_SCHEME_DATA_YEARS`): MGNREGA 2022-23…2025-26,
   PMAY-G 2017-18…2023-24, Focus Plus 2022-23 + 2025-26 only, Focus Legacy missing 2023-24,
   **CM Elevate has no year column at all**. Any unqualified "total" silently compares
   different periods.
4. **Geography grain is not shared.** `_AC_CAPABLE_SCHEMES` = MGNREGA, Focus Legacy, CM Elevate
   Legacy, NRLM; `_VILLAGE_FACT_SCHEMES` = MGNREGA, Focus Plus, PMAY-G, CM Elevate. **No scheme
   is in both lists except MGNREGA.**

A "pass" for these cases means the bot **states the mismatch and gives each scheme's own
correct figure side by side** — not that it produces one blended number. A single combined
total would be the wrong answer however well it is phrased.

---

## 3. Raw baseline — the figures every DB/bot answer must match

Computed from the source files this session. Focus Legacy and CM Elevate Legacy were
**aggregated only; no row was printed** (KI-022 / privacy boundary).

### Scheme totals

| Scheme | Source file | Rows | Key measures |
|---|---|---|---|
| **MGNREGA** employment | `MGNREGA_Employment.xlsx` | 26,375 | person-days **90,915,181**; persons 1,865,234 |
| **MGNREGA** expenditure | `Updated MNGREGA Expenditure WIth Year.xlsx` | 18,818 | **₹362,866.57 lakh** = ₹3,628.67 crore |
| **PMAY-G** | `PMAY_FullyMapped_with_dates.csv` | **171,107 houses** | sanctioned ₹2,222.75 cr; **released ₹2,185.26 cr** |
| **Focus Plus** | `Focus Plus Master.csv` | **385,671 payments** | **unique members 12,527**; ₹119.74 cr |
| **Focus Legacy** | `Focus Legacy to share to BLH.csv` | **14,569 payments** | **11,906 groups**; 102,021 memberships; ₹51.01 cr |
| **CM Elevate** | `CM_Elevate_AllSchemes_20260930_full.xlsx` | **8,627 applications** | **no money, no year** |
| **CM Elevate Legacy** | `Cm Elevate legacy…csv` | **2,823 applicants** | total disbursed **₹82.90 cr** (subsidy 52.57 + loan 30.33) |

MGNREGA person-days by year: 2022-23 **25,596,237** · 2023-24 **27,264,054** ·
2024-25 **14,667,872** · 2025-26 **23,387,018**.
MGNREGA expenditure by year (lakh): 2022-23 **88,629.40** · 2023-24 **75,961.36** ·
2024-25 **106,669.85** · 2025-26 **91,605.96**.

> **`Benefit to Households` is cumulative — do not sum it.** The naive sum is 213,784,268,
> which is not a household count. Report one year.

### Expected money ranking (CROSS-5, 12 — raw)

| Rank | Scheme | Crore | Measure |
|---|---|---|---|
| 1 | **MGNREGA** | **3,628.67** | expenditure incurred (₹362,866.57 lakh ÷ 100) |
| 2 | **PMAY-G** | **2,185.26** | money released (2,222.75 if read as *sanctioned*) |
| 3 | Focus Plus | 119.74 | cash disbursed to farmers |
| 4 | CM Elevate Legacy | 82.90 | subsidy + loan disbursed |
| 5 | Focus Legacy | 51.01 | memberships × ₹5,000 |
| – | **CM Elevate** | **not held** | no money column exists |

MGNREGA is first under either PMAY-G reading, so the top two are stable. But these are five
different *kinds* of figure — expenditure incurred vs money released vs cash disbursed vs
memberships × 5,000 — so the ranking is indicative only, never like-for-like.
`_cross_scheme_money_answer` already carries exactly that caveat verbatim, which is why
losing that path on CROSS-5 and CROSS-12 (XS-3) matters.

### District tables (for CROSS-3, 6, 7, 8, 11, 14, 16, 17, 18)

| District | MGNREGA exp (lakh) | PMAY-G houses | Focus+ unique | Focus+ payments | FL groups |
|---|---|---|---|---|---|
| WEST GARO HILLS | 79,241.81 | 33,147 | 4,659 | 126,259 | 2,701 |
| SOUTH WEST GARO HILLS | 46,279.43 | 11,947 | 2,452 | 53,740 | 794 |
| EAST KHASI HILLS | 41,492.84 | 23,952 | 1,394 | 50,250 | 909 |
| NORTH GARO HILLS | 40,465.39 | 16,028 | 1,914 | 38,286 | 1,173 |
| EAST GARO HILLS | 32,179.21 | 16,899 | 1,604 | 40,704 | 928 |
| SOUTH GARO HILLS | 29,597.77 | 11,901 | 504 | 32,936 | 532 |
| SOUTH WEST KHASI HILLS | 21,584.92 | 7,411 | – | – | 440 |
| RI BHOI | 18,315.99 | 17,565 | – | – | 955 |
| WEST JAINTIA HILLS | 17,324.82 | 12,570 | – | – | 1,130 |
| EAST JAINTIA HILLS | 15,043.70 | 5,756 | – | – | 626 |
| WEST KHASI HILLS | 13,154.46 | 8,437 | – | – | 926 |
| EASTERN WEST KHASI HILLS | 8,186.23 | 5,494 | – | – | 792 |

**CROSS-16 names Ri Bhoi, where Focus Plus has no registration-cohort members at all** —
the expected answer is an explicit zero-with-reason, not a dropped row.

---

## 4. Defects found — proven from source, no VPN needed

### XS-1 · A second bare "FOCUS" is dropped silently · **HIGH** · 8 of 20 cases

**Cases:** CROSS-1, 2, 4, 6, 8, 17, 19, 20 — every case whose wording is "Focus+ … and FOCUS".

`_named_schemes` returns **only Focus Plus**; Focus Legacy is dropped and **no which-Focus
clarification fires**. Measured:

```
CROSS-2  "…beneficiaries in Focus+ and FOCUS."     named=['Focus Plus']            ambiguous=False
CROSS-17 "…MGNREGA, PMAY-G, Focus+, FOCUS, and CM-ELEVATE."
                        named=['MGNREGA','PMAY-G','Focus Plus','CM Elevate']       ambiguous=False
```

**Root cause** (`app/pipeline.py:1502` `_is_ambiguous_focus`): it early-returns `False` the
moment `_SCHEME_NAME_PATTERN["Focus Plus"]` matches **anywhere** in the question. The guard was
written for the single-mention case, where a Focus Plus match genuinely settles it. It never
considers two *separate* Focus mentions — one qualified, one bare. `\bfocus\s*\+` consumes
"Focus+", and the standalone "FOCUS" becomes invisible.

Isolating probe:

```
'Compare … in FOCUS.'                     named=[]               ambiguous=True   <- correct
'Compare … in Focus+ and FOCUS.'          named=['Focus Plus']   ambiguous=False  <- WRONG
```

**Why it matters:** the user asked to compare N schemes and gets N−1, with nothing in the
answer saying a scheme was dropped. This is the silent-wrong-answer class CLAUDE.md §5 is
written about. It also contradicts the standing rule that a bare "Focus" is **asked about,
never guessed** (docs/SCHEMES.md).

**Fix direction:** run the ambiguity test on the Focus mentions the qualified patterns did
**not** consume — subtract the Focus Plus / Focus Legacy matches, then test whether a bare
`\bfocus\b` survives. That residue technique is already the idiom in this file
(`app/pipeline.py:419-420`). Must not re-break the bare-"Focus"-alone pause.

### XS-2 · Focus Plus beneficiary guard is disabled on every cross-scheme question · **HIGH** · 8 of 20

**Cases:** CROSS-1, 2, 3, 6, 8, 16, 19, 20.

`_focusplus_single_district_beneficiary_guard` (`app/pipeline.py:8973`) opens with:

```python
if schemes != ["Focus Plus"]:
    return sql
```

So in **every** multi-scheme question the deterministic protection against reading
`COUNT(*)` as beneficiaries is **off by construction**, leaving only the prose prompt rule —
which CLAUDE.md §5 states has "repeatedly failed under sampling". The comment on the guard
itself records that this exact template once "silently dropped every real beneficiary it
wasn't built to see".

**Magnitude — this is the largest error in the set:**

| Reading | Statewide | Error |
|---|---|---|
| `COUNT(*)` (payments) | **385,671** | — |
| `COUNT(DISTINCT beneficiary_key)` | ~~12,527~~ **105,813** | ~~30.8×~~ **3.64× overstatement** |

> **CORRECTED 2026-10-10 (live DB).** The 12,527 figure above was `DISTINCT member_id`, which exists
> only on the 12.5K cohort (the 93K cohort has none). The correct beneficiary total is **105,813**.
> See `docs/Cross_Scheme_UseCase_Retest_Report_2026-10-10.md` §3 C-1. The defect is still real.


And it **reorders the district ranking** that CROSS-3, 6, 7, 16, 18 ask for, because the
fan-out ratio is not uniform (20.0× in North Garo Hills to 65.3× in South Garo Hills):

```
rank by UNIQUE members : WGH, SWGH, NGH,  EGH,  EKH, SGH
rank by PAYMENTS       : WGH, SWGH, EKH,  EGH,  NGH, SGH, + 6 more districts
                                    ^^^ East Khasi Hills 3rd vs 5th
```

The payments reading also shows **12 districts where the registration cohort covers 6**, so
"which district leads" changes answer. A wrong ranking is harder to spot than a wrong total.

**Fix direction:** the guard's narrow single-district rewrite should not simply be widened —
but the *invariant* (Focus Plus beneficiaries = `COUNT(DISTINCT beneficiary_key)`, never
`COUNT(*)`) belongs in a post-SQL deterministic check that runs **regardless of scheme count**,
per the three-layer pattern in CLAUDE.md §5.

### XS-3 · `_cross_scheme_money_answer` misses two of its own cases · **MEDIUM** · CROSS-5, 12

CROSS-12 — "Which scheme provided the highest total financial assistance across Meghalaya?" —
is *precisely* the question the deterministic ranking path exists to answer. It does not fire:

```
CROSS-12  superlative=True   money=False  -> ranking NOT used
CROSS-5   superlative=False  money=True   -> ranking NOT used
```

Two independent single-token gaps:

- **`_MONEY_SUPERLATIVE` has no `assistance` / `support`.** Swapping one word fixes it:
  `"…highest total expenditure…"` → `rank=True`; `"…highest total financial assistance…"` →
  `rank=False`. (`assistance` appears in `pipeline.py` only at line 3565, in an unrelated
  eligibility regex.)
- **`_CROSS_SCHEME_SUPERLATIVE` doesn't match "higher … : A or B?"** (CROSS-5). Its
  "which/what scheme" branch lists `more|less` but the A-or-B colon shape with `higher`
  doesn't reach it.

**Impact:** both fall through to LLM-generated SQL. For CROSS-5 that is worse than a miss —
the deterministic answer is the one that correctly reports **CM Elevate as "not held"** rather
than 0. Losing it on a question that explicitly asks Focus+ *vs* CM Elevate removes the
protection exactly where it was needed.

**Fix direction:** add `assistance|support` to `_MONEY_SUPERLATIVE` and a `higher|more` A-or-B
branch to `_CROSS_SCHEME_SUPERLATIVE`. Both are additive; `tests/test_cross_scheme_money.py`
is the regression lock.

### XS-4 · A two-scheme question routed as single-scheme · **MEDIUM** · CROSS-2

Because XS-1 drops the second scheme, CROSS-2 resolves to `['Focus Plus']` — a **one**-scheme
set. The cross-scheme guidance block is then **never loaded into the SQL prompt**:

```
CROSS-2   n=1  cross_block=False   ['Focus Plus']
CROSS-3   n=2  cross_block=True    ['Focus Plus', 'CM Elevate']
```

So the model is asked a comparison question with single-scheme context, and none of the
FAMILY-A/B/C procedure, the unit warnings or the "never row-join" invariants are present.
A consequence of XS-1, listed separately because it would persist for any other wording that
collapses to one scheme.

### XS-5 · 5 of 20 cases name no scheme and depend on the LLM classifier · **LOW/INFO**

CROSS-7, 12, 13, 15, 18 return `shortcut=None`, so the scheme set comes from the 4B classifier
with no deterministic floor. On failure the fallback is the **whole seven-scheme catalog**,
which pulls NRLM and both Legacy schemes into questions the officers meant for the main four,
and the year/grain mismatches of §2 then all apply at once. Not a defect — but it is the least
predictable path in the set and should be read first in the live re-run.

---

## 5. Per-case assessment

Verdicts are **static** (code + raw). "Needs live" = no defect found offline, must still be run.

| Case | Schemes resolved | Static verdict |
|---|---|---|
| CROSS-1 | MGNREGA, PMAY-G, Focus+ | **FAIL** XS-1 (FOCUS dropped), XS-2 |
| CROSS-2 | Focus+ **only** | **FAIL** XS-1, XS-2, XS-4 (routed single-scheme) |
| CROSS-3 | Focus+, CM Elevate | **FAIL** XS-2 (ranking reorders) |
| CROSS-4 | Focus+, CM Elevate | **FAIL** XS-1; CM Elevate money impossible |
| CROSS-5 | Focus+, CM Elevate | **FAIL** XS-3 (ranking missed on the one case that needs it) |
| CROSS-6 | Focus+, CM Elevate | **FAIL** XS-1, XS-2 |
| CROSS-7 | *classifier* | Needs live · XS-5 |
| CROSS-8 | Focus+, CM Elevate | **FAIL** XS-1 |
| CROSS-9 | MGNREGA, PMAY-G, CM Elevate | Needs live (block grain; prompt warnings present) |
| CROSS-10 | MGNREGA, PMAY-G, Focus+, CM Elevate | Needs live; CM Elevate money impossible |
| CROSS-11 | **all 7** | Needs live; widest unit/year spread in the set |
| CROSS-12 | *classifier* | **FAIL** XS-3 (`assistance` gap) · XS-5 |
| CROSS-13 | *classifier* | Needs live · XS-5 |
| CROSS-14 | MGNREGA, PMAY-G, Focus+, CM Elevate | Needs live |
| CROSS-15 | *classifier* | Needs live · XS-5 |
| CROSS-16 | Focus+, CM Elevate | **FAIL** XS-1, XS-2; Ri Bhoi = Focus+ zero |
| CROSS-17 | MGNREGA, PMAY-G, Focus+, CM Elevate | **FAIL** XS-1 (FOCUS dropped from a 5-scheme ask) |
| CROSS-18 | *classifier* | Needs live · XS-5 |
| CROSS-19 | Focus+, CM Elevate | **FAIL** XS-1, XS-2; CM Elevate money impossible |
| CROSS-20 | MGNREGA, PMAY-G, Focus+, CM Elevate | **FAIL** XS-1, XS-2 |

**Static totals: 11 of 20 FAIL on code evidence alone; 9 need the live run.**
Not one of the 11 would surface as a visible error — each returns a confident answer with a
wrong number, a missing scheme, or a ranking in the wrong order.

What the prompt **does** get right, and should not be weakened: the `_CROSS_SCHEME` block in
`schema_context.py` correctly warns that CM Elevate is absent from both cross-scheme views and
calls a CM Elevate money comparison "IMPOSSIBLE"; that `v_cross_scheme_village_coverage` has no
Focus Plus and no CM Elevate column; and that Focus Plus beneficiaries are
`COUNT(DISTINCT beneficiary_key)`. Verified present for all multi-scheme sets in §5.

---

## 6. How to finish this — the live re-run

Needs the Fortinet VPN. Order matters: fix XS-1 and XS-3 first, or 10 cases re-test unchanged.

```bash
# 0) confirm the VPN
ping 10.48.242.4

# 1) DB column — the figures in §3 must reconcile against curated.*
.venv/Scripts/python.exe -c "import asyncio;from app.db import run_readonly;\
print(asyncio.run(run_readonly('SELECT DISTINCT scheme_code FROM curated.v_cross_scheme_money_district_year')))"
#    ^ settles whether Focus Plus is in the money view (docs call it UNCONFIRMED)

# 2) bot column — in-process, from the repo root so .env loads
.venv/Scripts/python.exe -c "import asyncio;from app import pipeline;\
print(asyncio.run(pipeline.answer_question('<case text>'))['answer'])"

# 3) regression suites
.venv/Scripts/python.exe -m pytest -q $(grep -lE "^\s*(async )?def test_" tests/test_*.py)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/test_cross_scheme_money.py
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe tests/test_cross_scheme_collision.py
.venv/Scripts/python.exe tests/live_context_validation.py      # XS-1 touches routing
```

Each fix needs a regression test calling the **real function** (CLAUDE.md §7): XS-1 →
`_is_ambiguous_focus("…Focus+ and FOCUS…") is True`; XS-3 → `_wants_cross_scheme_money_ranking`
on both wordings; XS-2 → the invariant on a multi-scheme set.

**One open DB question** this session could not settle: whether `v_cross_scheme_money_district_year`
contains Focus Plus. `docs/SCHEMES.md` says unconfirmed, `schema_context.py` tells the model to
probe it at runtime, and `_CROSS_SCHEME_MONEY_SQL` assumes **not** (it adds Focus Plus via a
separate UNION branch). If the view *does* carry Focus Plus, that SQL **double-counts it**.
Marked **UNKNOWN — NEEDS VERIFICATION**; step 1 above answers it in one query.

---

## 7. Note on the test set itself

Eight cases say "FOCUS" where the catalog has two Focus schemes. That wording is what exposed
XS-1, so it is worth keeping. But for the officers' own sign-off the intent should be confirmed:
if "FOCUS" means Focus Legacy, cases CROSS-1/2/6/17/19/20 are asking a scheme with **no person
record** for a beneficiary count, and the only correct answer states that and offers groups or
memberships instead. Worth settling before these become an acceptance criterion.

---

*Raw figures: computed this session from the source files, aggregate-only for the two PII
partitions. Code findings: VERIFIED by executing the real pipeline functions offline. DB and
bot columns: NOT RUN (VPN).*
