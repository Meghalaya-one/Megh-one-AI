# Cross-Scheme Use Cases — LIVE Retest Report (2026-10-10)

*The 20 cases in `Cross Scheme Test Cases.csv`, re-run **with the VPN up** against the live
DB and the live model gateway. Supersedes the raw-only audit in
`docs/Cross_Scheme_UseCase_Test_Report_2026-10-10.md`.*

**Testing only — no application code was changed in this session.**

---

## AFTER FIXES — 20/20 (live, 2026-10-10, 15:45 onward)

**Every one of the 20 officer cases now answers correctly, live, and so do the 15 typed-scope
variants and the user's UI screenshot question: 36/36**, each figure checked against megh_db by an
independent verifier whose SQL is written separately from the app's. 19 of 20 answer in one turn
with no pause; CROSS-9 asks one legitimate question ("Mawphlang" is a block and a village) and
answers on the reply.

| Case | Before | After (live) |
|---|---|---|
| 1 | year pause → "couldn't build" | MGNREGA 365,556 households (FY 2025-26) · PMAY-G 170,981 houses · Focus Plus 105,813 · Focus Legacy 102,021 memberships in 11,906 groups |
| 2 | "doesn't cover FOCUS" | Focus Plus 105,813 beneficiaries · Focus Legacy 102,021 memberships in 11,906 groups, never added |
| 4 | three pauses / "0 crore" ×3 | Focus Plus ₹119.74 · Focus Legacy ₹51.01 · CM Elevate Legacy ₹82.90 crore |
| 5 | three pauses | Focus Plus ₹119.74 vs CM Elevate Legacy ₹82.90 → Focus Plus higher |
| 7 / 18 | wrong range / no answer | 12-district table; NRLM's SHG members lead 8 districts, MGNREGA's households 4 |
| 10 | 997 junk rows | 4 rows: MGNREGA 3,628.67 · PMAY-G 2,185.26 · Focus Plus 119.74 · CM Elevate Legacy 82.90 |
| 11 | "737.68 / 888.11 crore total", FP statewide as EKH | East Khasi Hills, 7 schemes, no total: MGNREGA 414.93 · PMAY-G 307.13 · **Focus Plus 15.62** · CM Elevate none · FL 5.42 · CMEL 22.60 · NRLM 18.29 |
| 12 | "Focus Plus highest, 1197392500.00" | **MGNREGA highest, ₹3,628.68 crore** (sanctioned view; ±0.02 cr view rounding) |
| 13 / 14 / 15 | encyclopaedia text | coverage leader / district performance / top district per scheme, from the data |

**What changed:** D-034 (a deterministic cross-scheme comparison — fixed per-scheme queries,
each scheme's own measure, never a combined total), plus KI-214 vocabulary, KI-225 (an all-years
scope no longer moves CM-ELEVATE to Legacy), KI-219 (per-scheme years in the year pause), KI-222
(DATA intent) and deterministic guards on the remaining model-SQL path (KI-213/218/220/221/223/
226/227). See KNOWN_ISSUES for each.

**Controls unchanged (live):** "Compare MGNREGA and PMAY-G spending", Focus Plus beneficiaries in
West Garo Hills (35,039 ✓), CM Elevate on hold in Ri Bhoi, CM Elevate disbursed in East Khasi Hills
(→ Legacy ₹22.60 cr ✓), PMAY-G 2019-20, "What is Focus Plus?", MGNREGA+PMAY-G village overlap,
MGNREGA person-days — all keep their previous path and pauses.

**Harness correction (affects §0 and §2–§7 below):** those runs sent a clarification chip's
LABEL as the reply; the UI sends the chip's full `question`. "MGNREGA (rural employment)" sent as a
question is why CROSS-13/15 returned an encyclopaedia paragraph, and part of KI-224's triple
pause. The after-fix run uses the UI's behaviour.

**One judgement to confirm with the officers:** CROSS-7 / 18 name no scheme, so all seven are
compared, and NRLM's SHG members (410,847) lead 8 districts. If the officers mean the five schemes
of the other cases, the question should name them.

---

## 0. CORRECTED VERDICT — typed-scope retest (supersedes §2 and §7)

**Why this section exists.** The user ran CROSS-1 in the UI with the scope **typed into the
question** ("…across all financial years") and got a correct answer — while §2 below scored
CROSS-1 as "no answer". Both are true: the run in §2-§7 answered the year pause by **clicking the
"All financial years combined" chip**, and the chip-resume path behaves worse than the typed
path. Typing the scope skips the pause entirely. So the first live run over-counted failures
that were really **chip-path** failures. Every non-passing case was therefore re-run with the
scope typed in, the way an officer naturally writes it, and every figure re-checked against the
DB. **The verdict below uses the better of the two phrasings for each case.**

| Case | Best verdict | Typed-scope answer vs DB |
|---|---|---|
| **1** | **PASS** | MGNREGA 365,556 / PMAY-G 170,981 / FP 105,813 / FL 102,021 — **all 4 match DB**. Caveat: four different measures under one "beneficiaries" label |
| 2 | PARTIAL | FP 105,813 ✓; "FL 11,906 beneficiaries" is **producer groups**; then adds people + groups = 117,719 (invalid) |
| **3** | **PASS** | 25 rows, all match (chip path) |
| 4 | **FAIL — wrong** | "**0 crore** for Focus Plus, Focus Legacy and CM Elevate Legacy". Truth: **119.74 / 51.01 / 82.90 cr**. SQL filtered the MGNREGA+PMAY money view on scheme codes that don't exist |
| 5 | PARTIAL | "Focus+ higher" — correct vs CM Elevate **Legacy** (119.74 > 82.90 cr), but no figures given, and CM-ELEVATE was swapped to Legacy |
| 6 | PARTIAL | WGH FP 35,039 ✓, FL 2,701 groups ✓, 493 ✓ — but 493 is **CM Elevate Legacy**; CM-ELEVATE (applications) in WGH is **1,708** |
| 7 | **FAIL — wrong** | Correct conclusion (MGNREGA leads every district) but says the range tops out at "56,936 in EKH"; **WGH 65,886 is the max and was in the result**. PMAY "up to 32,346" — real max **33,143** |
| 8 | PARTIAL | FP 50,250 payments / 13,608 ✓. FL 924/898 and CMEL 212 only reproduce with an extra `entity_type<>'Unresolved'` filter (plain DB 935/909/241); CM-ELEVATE swapped to Legacy; amounts printed raw with no unit |
| 9 | **FAIL — no answer** | "couldn't build a working query" (both paths) |
| 10 | **FAIL** | Typed: no answer. Chip: 3 correct figures + ~997 junk rows |
| 11 | **FAIL — wrong** | Now lists all 7 schemes, but **Focus Plus ₹119.74 cr is the STATEWIDE figure** (SQL branch has no district filter); EKH truth is **15.62**. Still adds them into one cross-unit total (888.11 cr) and says CM Elevate "0.00" |
| 12 | **FAIL — wrong** | Typed: re-scoped to CM Elevate Legacy's own sub-schemes ("Warehouse Scheme highest, ₹22.62 cr") — a different question. Chip: "Focus Plus highest". **Truth: MGNREGA ₹3,628.67 cr** |
| 13 | **FAIL — no data** | Encyclopaedia paragraph (both paths) |
| 14 | **FAIL — no data** | Prose on audit lapses, 0 rows (no pause involved — phrasing-independent) |
| 15 | **FAIL — no data** | Encyclopaedia paragraph (both paths) |
| 16 | **FAIL — no answer** | "couldn't build a working query" (both paths) |
| **17** | **PASS** | All match except FP off by one (105,812 vs 105,813) |
| **18** | **PASS** | All 12 districts match |
| 19 | **FAIL — wrong** | FP 105,813 ✓, CMEL 2,823 ✓, ₹828,978,537.75 ✓ (no unit) — but "**no total amounts for Focus Plus**" is false (₹119.74 cr exists); FL "11,906 beneficiaries" = groups |
| **20** | **PASS** | Coverage + money match (CME villages 2,087 vs 2,092) |

**Corrected totals: 5 PASS · 4 PARTIAL · 11 FAIL** (was 4 / 0 / 16).
Of the 11 FAILs: **5 state a wrong number** (4, 7, 11, 12, 19), **3 give no data** (13, 14, 15),
**3 give no answer** (9, 10, 16).

**What the typed-scope retest changes about the defects:**
- **KI-221 is narrower than §5 says.** The WIDE CROSS-JOIN shape *does* pass the verifier on the
  typed path (CROSS-1's working SQL is a 4-way CROSS JOIN). The verifier rejections cluster on
  the **chip-resume** path; on the typed path only 9, 10, 16 still fail to build.
- **New — KI-225: typing a year flips CM-ELEVATE to the other dataset.** `_pin_cm_elevate_dataset`
  reads "across all financial years" as a Legacy cue (by design — CM Elevate has no year), so
  "Focus+ and CM-ELEVATE … across all financial years" silently answers for **CM Elevate Legacy**
  (2,823 sanction records) instead of CM Elevate (8,627 applications). An officer typing the
  year to skip the pause changes which scheme they get. Cases 5, 6, 8, 19 hit it.
- **New — KI-226: the composer misstates extremes it was given.** CROSS-7: the max it reported
  (56,936) is a real row value, so the numeric-faithfulness check passes, but it is not the max
  (65,886). Faithfulness checks that a number exists, not that a "highest/lowest" claim is true.
- **New — KI-227: one UNION branch loses the place filter.** CROSS-11: every branch has
  `lgd_district='EAST KHASI HILLS'` except Focus Plus, so a statewide figure is reported as a
  district figure.
- **KI-218 widened:** CROSS-4 queried `v_cross_scheme_money_district_year` for `'Focus Plus'`,
  `'Focus Legacy'`, `'CM Elevate Legacy'` — none exist there — and answered **"0 crore" three
  times**. Same pattern as CROSS-2's fabricated `'FOCUS'`.
- **New — KI-228: "Focus Legacy beneficiaries" has no stable meaning.** The same label came back
  as **102,021** (memberships, CROSS-1) and **11,906** (groups, CROSS-2) for two phrasings of
  one question. Focus Legacy has no person record; the answer must name which it is.

---

## 1. What is different from the first report

| | First report (earlier today) | This retest |
|---|---|---|
| Raw column | done | done (unchanged) |
| **DB column** | **not run** (VPN down) | **DONE** — live `megh_db.curated` |
| **Bot column** | **not run** (VPN down) | **DONE** — live `pipeline.answer_question`, multi-turn |
| Code under test | pre-D-033 | **post-D-033** (`_pin_bare_focus`, KI-212/KI-217 fixed) |

Two things changed between the reports: the **VPN came back**, and someone **fixed KI-212**
via D-033 (a bare "Focus" now *means* Focus Legacy rather than raising a which-Focus pause).
Both are reflected below. Three findings from the first report are **corrected** in §3.

**Method.** Each case called the real `pipeline.answer_question` with a real `Session`, and
where the pipeline paused for clarification the harness answered the way an officer would —
picking the widest option ("All financial years combined", "All of Meghalaya") — up to 3 hops,
mirroring `tests/live_context_validation.py`. Startup replicated `app/main.py` (pool, LLM
client, Qdrant, annotations, entity resolver, schema catalog, scheme years). Every figure the
bot stated was then re-queried directly against the DB.

---

## 2. Headline result

| Verdict | Count | Cases |
|---|---|---|
| **PASS** — figures match the DB, answer is sound | **4** | CROSS-3, 17, 18, 20 |
| **FAIL — no answer at all** ("couldn't build a working query") | **6** | CROSS-1, 6, 7, 8, 16, 19 |
| **FAIL — endless pause, never answered** | **3** | CROSS-4, 5, 9 |
| **FAIL — wrong or misleading number stated confidently** | **4** | CROSS-10, 11, 12, 2 |
| **FAIL — answered from the knowledge base, no data at all** | **3** | CROSS-13, 14, 15 |

**4 / 20 pass. 16 / 20 fail**, and the failures split almost evenly between *visible*
(no answer, 9 cases) and *silent* (a confident wrong number or prose instead of data, 7 cases).

**18 of 20 paused for clarification on the first turn** — overwhelmingly
`year-not-specified` (13). These questions genuinely name no year, so a pause is defensible,
but see XS-7: the pause offers a **union of 9 financial years no single scheme has**.

---

## 3. Corrections to the first report

Running the DB settled three things I had to mark provisional or got wrong:

**C-1 · Focus Plus beneficiaries are 105,813, not 12,527 — and KI-213's magnitude is 3.64×, not 30.8×.**
My raw pass counted `DISTINCT member_id`. The DB shows why that is wrong:

```
batch_label  rows     DISTINCT beneficiary_key   DISTINCT member_id
93K          373,144  93,286                     0        <- no member_id at all
12.5K        12,527   12,527                     12,527
TOTAL        385,671  105,813                    12,527
```

`member_id` exists **only** on the 12.5K cohort — exactly as `SCHEME_METRICS` warns
("12.5K registration cohort only … NOT the scheme total"). The correct measure is
`COUNT(DISTINCT beneficiary_key)` = **105,813**. So there are **two** wrong readings to guard
against, not one: `COUNT(*)` overstates by **3.64×**, and `member_id` understates by **8.45×**.
KI-213 is still real and still High — the guard is still disabled on multi-scheme questions —
but the figure in the first report was wrong and is corrected here.

**C-2 · PMAY-G is 170,981 houses, not 171,107.** The raw file has 171,107 rows; 126 are
`is_placeholder` and the mandatory `WHERE NOT is_placeholder` excludes them. The bot applied
this filter correctly everywhere it mattered.

**C-3 · KI-216 is CLOSED — there is no double-counting.**
`SELECT DISTINCT scheme_code FROM curated.v_cross_scheme_money_district_year` returns
**MGNREGA and PMAY only**. Focus Plus is absent, so `_CROSS_SCHEME_MONEY_SQL`'s separate
Focus Plus UNION branch is correct. The view's own totals also reconcile with my raw figures to
within rounding (MGNREGA 3,628.68 vs 3,628.67 cr; PMAY 2,185.24 vs 2,185.26 cr) — **raw and DB
agree**.

---

## 4. KI status after the retest

| KI | First report | Now |
|---|---|---|
| KI-212 bare FOCUS dropped | Open (High) | **FIXED** — verified below |
| KI-215 two-scheme routed as one | Open (Medium) | **FIXED** — consequence of KI-212 |
| KI-216 Focus Plus double-count | UNKNOWN | **CLOSED — not a defect** (§3 C-3) |
| KI-213 beneficiary guard off | Open (High) | **Still open**, magnitude corrected to 3.64× |
| KI-214 money ranking misses | Open (Medium) | **Still open — and now proven to cause a wrong answer** (XS-8) |

**KI-212 / KI-215 fix verified.** All 8 bare-FOCUS cases now pin to Focus Legacy before routing:

```
CROSS-1   ['MGNREGA','PMAY-G','Focus Plus','Focus Legacy']          (was 3 schemes)
CROSS-2   ['Focus Plus','Focus Legacy']        xblock=True          (was 1 scheme, no xblock)
CROSS-17  ['MGNREGA','PMAY-G','Focus Plus','CM Elevate','Focus Legacy']  (all 5)
CROSS-20  ['MGNREGA','PMAY-G','Focus Plus','CM Elevate','Focus Legacy']  (all 5)
```

**But routing is not the answer.** CROSS-2 resolves both schemes and still replies
*"Focus Plus: 105,813 beneficiaries"* plus *"The data available doesn't cover the FOCUS scheme
for this comparison"* — see XS-6. The pin works; the SQL and the composer do not follow it.

---

## 5. Defects found in the live run

### XS-6 · The LLM invents a fake scheme filter instead of querying Focus Legacy · **HIGH** · CROSS-2

Routing is correct (`['Focus Plus','Focus Legacy']`, cross-scheme block loaded), but the
generated SQL is:

```sql
SELECT 'Focus Plus' AS scheme, COUNT(DISTINCT beneficiary_key) AS beneficiaries
FROM curated.v_focus_plus
UNION ALL
SELECT 'FOCUS' AS scheme, 0 AS beneficiaries
FROM curated.v_cross_scheme_money_district_year
WHERE scheme_code = 'FOCUS'          -- no such scheme_code: the view holds MGNREGA and PMAY
```

The second branch queries the **money** view for a **beneficiary** count, filtered on a
`scheme_code` that does not exist, so it returns no rows — and the answer then asserts the data
"doesn't cover the FOCUS scheme". It does: Focus Legacy has 11,906 producer groups and 102,021
memberships. **The user is told data is missing when it is present.** Worse, this is the shape
D-033 was supposed to fix; the pin reaches the router but not the generator.

### XS-7 · The year pause offers a union of years no scheme actually has · **HIGH** · 13 cases

CROSS-1's pause (verbatim):

> "MGNREGA, PMAY-G, Focus Plus and Focus Legacy data is available for FY 2017-18, FY 2018-19,
> FY 2019-20, FY 2020-21, FY 2021-22, FY 2022-23, FY 2023-24, FY 2024-25 and FY 2025-26.
> Which of these is required…?"

Those 9 years are the **union**. No scheme has all 9. Per `_SCHEME_DATA_YEARS`: MGNREGA
2022-23…2025-26, PMAY-G 2017-18…2023-24, **Focus Plus only 2022-23 and 2025-26**, Focus Legacy
missing 2023-24. An officer who picks **FY 2019-20** gets a "comparison" containing **PMAY-G
only** — the other three schemes have no data that year, and nothing says so. The sentence
asserts the availability as joint ("MGNREGA, PMAY-G, Focus Plus and Focus Legacy data is
available for …"), which is false for every one of the 9 options.

### XS-8 · KI-214's consequence: a confidently wrong "highest scheme" · **CRITICAL** · CROSS-12

Because `_MONEY_SUPERLATIVE` lacks `assistance`, the deterministic ranking does not fire and the
LLM wrote the SQL instead. The bot answered:

> "**Focus Plus** provided the highest total financial assistance with **1197392500.00** across
> Meghalaya for all financial years combined."

The truth from the DB:

| Scheme | Crore |
|---|---|
| **MGNREGA** | **3,628.67** |
| PMAY-G | 2,185.26 |
| Focus Plus | 119.74 |
| CM Elevate Legacy | 82.90 |
| Focus Legacy | 51.01 |

**MGNREGA is the highest, at 30× Focus Plus.** The generated SQL simply omitted the MGNREGA and
PMAY-G branches — even though the pipeline had resolved 4 schemes — then `ORDER BY … LIMIT 1`
turned a partial list into a confident superlative. The figure is also printed as a bare
`1197392500.00` with **no unit**. `_cross_scheme_money_answer` would have produced the correct
ranking *with* its measure-semantics caveat; one missing vocabulary token is the whole
difference. **This is the single most damaging result in the run.**

### XS-9 · A cross-unit total presented as one figure, with schemes silently dropped · **CRITICAL** · CROSS-11

All 7 schemes resolved. The answer:

> "MGNREGA recorded 414.93 crore, PMAY-G recorded 307.13 crore, Focus Plus recorded 15.62 crore,
> and CM Elevate recorded 0.00 crore. **The total expenditure across all four schemes in East
> Khasi Hills sums to 737.68 crore.**"

Three defects in one sentence:
1. **A meaningless sum.** MGNREGA's figure is expenditure incurred (from lakh), PMAY-G's is money
   released (from rupees), Focus Plus's is DBT cash. `docs/DATA_MODEL.md` rule 6 forbids mixing
   units in one SUM; the prompt forbids combining these measures. 737.68 crore is not a quantity.
2. **"all four schemes" is wrong** — 7 were resolved. Verified present in East Khasi Hills and
   silently dropped: **CM Elevate Legacy 22.60 cr, NRLM RF+CIF 18.29 cr, Focus Legacy 5.42 cr**.
3. **"CM Elevate recorded 0.00 crore"** states a false zero. CM Elevate has **no money column**;
   the correct statement is "not held", which the prompt explicitly calls IMPOSSIBLE to compare.

### XS-10 · The semantic verifier rejects the prompt's own prescribed pattern · **HIGH** · 6 cases

The 6 no-answer cases (CROSS-1, 6, 7, 8, 16, 19) all die the same way. **9 of the 11 repair
attempts in the run were triggered by the verifier calling a legitimate aggregate-then-join a
"PROHIBITED JOIN."** Example (CROSS-1):

> "The SQL joins `curated.v_employment` with `curated.v_pmay` and `curated.v_focus_plus` via
> CROSS JOIN. This violates the prohibition: 'NEVER join curated.v_pmay →
> curated.fact_mgnrega_employment directly.' … The generated SQL aggregates each side
> independently but then joins the results, which is the exact pattern forbidden…"

That reading is **wrong**, and `schema_context.py` says so: CROSS JOIN of one-row per-scheme
subqueries is the **required** FAMILY C "WIDE" shape, and "the ONLY valid cross-scheme joins
are … a FULL OUTER JOIN between two subqueries ALREADY aggregated to the same grain." The
prohibition targets **row-level** fact joins. The 4B verifier cannot tell the two apart, burns
all repair attempts, and the user gets *"I couldn't build a working query."* This is KI-002
(verifier false positives) but specifically fatal on cross-scheme questions — the hardest ones,
where the prescribed pattern is *always* an aggregate-then-join.

### XS-11 · Three cases answered from the knowledge base instead of the data · **HIGH** · CROSS-13, 14, 15

CROSS-13 ("widest coverage across districts") and CROSS-15 ("highest concentration of
beneficiaries") both paused `scheme-not-specified`, and the harness's reply ("MGNREGA") turned
each into a KNOWLEDGE question. Both answered with an **encyclopaedia paragraph**:

> "MGNREGA … is an Indian labour law and social security measure. It is the world's largest
> rights-based employment guarantee programme…"

CROSS-14 ("performance of … in East Khasi Hills") never paused and still returned prose about
*independent studies* and *audit lapses* — **0 rows, no SQL, no East Khasi Hills figure at all**.
These are DATA questions ("which district", "highest concentration", "performance in X") that
left the DATA path entirely. CROSS-13/15 also show the scheme pause mis-framing a
*cross-scheme* question as needing one scheme — the answer *is* the scheme, which is what
`_CROSS_SCHEME_SUPERLATIVE` exists to catch (both have `superlative=True` but no money word).

### XS-12 · An un-aggregated UNION branch floods the result with 997 junk rows · **MEDIUM** · CROSS-10

```sql
...
UNION ALL
SELECT 'CM Elevate' AS scheme, NULL AS total_disbursement_crore
FROM curated.v_cm_elevate        -- no aggregate, no GROUP BY -> one row PER APPLICATION
```

This emits 8,627 rows, truncated by the `run_readonly` 1,000-row cap. `row_count` = **1000**,
the answer says "Here are the 40 results", and the rendered list is 3 real figures followed by
a wall of empty "CM Elevate" lines. The three real figures are right (3,628.67 / 2,185.26 /
119.74 cr — all match the DB), so the data is sound and the **presentation** is broken. This is
the prompt's "NEVER wrap/emit a finished per-scheme subquery without its aggregate" invariant
being broken in a way no guard catches.

### XS-13 · Three cases never reach an answer at all · **HIGH** · CROSS-4, 5, 9

Pause chains that never terminate within 3 hops:

```
CROSS-4  scope-not-specified -> year-not-specified -> scheme-not-specified  (still paused)
CROSS-5  scope-not-specified -> year-not-specified -> scheme-not-specified  (still paused)
CROSS-9  entity-ambiguous    -> year-not-specified -> scheme-not-specified  (still paused)
```

The third pause asks **"which scheme?"** for questions that named their schemes explicitly —
CROSS-4 says "Focus+, FOCUS, and CM-ELEVATE", CROSS-5 says "Focus+ or CM-ELEVATE" — and offers
**"MGNREGA (rural employment)"** among the options. Three pauses for one question is beyond any
officer's patience, and the last one discards information the question already gave. Related to
the KI-180/181 family (a pause that re-asks what was already stated).

---

## 6. The four passes — what good looks like

**CROSS-3** (district comparison, Focus+ vs CM Elevate): all **25 rows match the DB exactly**
(EWKH 301/1,221; EGH 605/11,379; EJH 155/404; EKH 1,052/13,608 …). Correct per-scheme measures
(`COUNT(DISTINCT beneficiary_key)` and `COUNT(DISTINCT request_id)`), correct
`entity_type <> 'Unresolved'` filter, correct UNION ALL shape. *Caveat:* the answer labels CM
Elevate's applications as "beneficiaries" — an application is a request, not an award.

**CROSS-17** (district-wise summary, 5 schemes): every statewide figure matches —
MGNREGA 90,915,181 person-days, PMAY-G 170,981 houses, CM Elevate 8,600 applications — and the
West Garo Hills peaks match exactly (17,539,710 / 33,143 / 35,039). **One off-by-one:** Focus+
stated as 105,812 against a true 105,813. *Also:* Focus Legacy was resolved but does not appear
in the answer.

**CROSS-18** (leading scheme per district): **all 12 district figures exact** (11,147 … 65,886),
and MGNREGA genuinely does lead every district. The 13th row is a NULL-district artifact that
should be filtered.

**CROSS-20** (overall comparison): village coverage matches for 4 of 5 schemes
(MGNREGA 4,673, PMAY-G 5,120, Focus+ 3,523, Focus Legacy 3,430); CM Elevate stated 2,087 vs
2,092 (the Unresolved filter). Money figures correct. Correctly reports CM Elevate as 0 crore —
though "not held" would be the accurate wording.

**The pattern:** every pass uses the LONG (UNION ALL) shape. Every no-answer failure attempted
the WIDE (CROSS JOIN) shape and was killed by the verifier (XS-10). That is an actionable signal
— the WIDE shape is effectively unusable today.

---

## 7. Per-case results

| Case | Verdict | Bot said | DB truth | Defect |
|---|---|---|---|---|
| CROSS-1 | **FAIL** no answer | "couldn't build a working query" | – | XS-10 |
| CROSS-2 | **FAIL** wrong | FP 105,813; "doesn't cover FOCUS" | FL = 11,906 groups / 102,021 memberships | XS-6 |
| CROSS-3 | **PASS** | 25 rows | all 25 match | labels apps "beneficiaries" |
| CROSS-4 | **FAIL** no answer | 3 pauses, unanswered | – | XS-13 |
| CROSS-5 | **FAIL** no answer | 3 pauses, unanswered | – | XS-13, KI-214 |
| CROSS-6 | **FAIL** no answer | "couldn't build…" | – | XS-10 |
| CROSS-7 | **FAIL** no answer | "couldn't build…" | – | XS-10 |
| CROSS-8 | **FAIL** no answer | "couldn't build…" | – | XS-10 |
| CROSS-9 | **FAIL** no answer | 3 pauses, unanswered | – | XS-13 |
| CROSS-10 | **FAIL** presentation | "40 results" + 997 junk rows | 3 figures correct | XS-12 |
| CROSS-11 | **FAIL** wrong | "737.68 cr across all four" | 3 schemes dropped; sum invalid | XS-9 |
| CROSS-12 | **FAIL** **wrong** | "**Focus Plus** highest, 1197392500.00" | **MGNREGA 3,628.67 cr** | XS-8, KI-214 |
| CROSS-13 | **FAIL** no data | encyclopaedia paragraph | – | XS-11 |
| CROSS-14 | **FAIL** no data | prose on audit lapses, 0 rows | – | XS-11 |
| CROSS-15 | **FAIL** no data | encyclopaedia paragraph | – | XS-11 |
| CROSS-16 | **FAIL** no answer | "couldn't build…" | – | XS-10 |
| CROSS-17 | **PASS** | 12 district rows | match; FP off by 1 | 105,812 vs 105,813 |
| CROSS-18 | **PASS** | MGNREGA leads all 12 | all 12 match | NULL-district row |
| CROSS-19 | **FAIL** no answer | "couldn't build…" | – | XS-10 |
| CROSS-20 | **PASS** | 5 schemes, coverage + money | match; CME 2,087 vs 2,092 | wording "0 crore" |

---

## 8. Priority order for fixes

1. **XS-8 / KI-214 (CRITICAL)** — add `assistance|support` to `_MONEY_SUPERLATIVE` and a
   `higher|more … A or B` branch to `_CROSS_SCHEME_SUPERLATIVE`. Smallest change, largest
   payoff: it converts the run's worst wrong answer into the correct deterministic ranking.
   Lock: `tests/test_cross_scheme_money.py`.
2. **XS-10 (HIGH, unblocks 6 cases)** — teach the verifier that an aggregate-then-join of
   per-scheme subqueries is *required*, not prohibited. The prohibition must apply to row-level
   fact joins only. This single fix is the difference between 6 no-answers and 6 attempts.
3. **XS-9 (CRITICAL)** — a deterministic composer check: never state a total across schemes
   whose units differ, never say "all N schemes" unless N matches the resolved set, and never
   render CM Elevate money as 0.00 (say "not held"). Per CLAUDE.md §5 this belongs as a guard,
   not a prompt rule.
4. **XS-7 (HIGH)** — the year pause must show coverage **per scheme**, not a union, and warn
   when a chosen year covers only some of them.
5. **XS-11 (HIGH)** — a cross-scheme superlative with no money word ("widest coverage",
   "highest concentration") must stay on the DATA path, not fall through to RAG.
6. **XS-6, XS-12, XS-13, KI-213 (HIGH/MEDIUM)** — as described above.

Every fix needs a regression test calling the **real function** (CLAUDE.md §7), and
`tests/live_context_validation.py` must be re-run for anything touching routing or pauses.

---

## 9. Reproducing this run

```bash
ping 10.48.242.4                 # needs the Fortinet VPN
# DB column
.venv/Scripts/python.exe -I <scratch>/db_baseline.py out.json
# bot column (multi-turn, resumable; skips ids already in the jsonl)
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe -I <scratch>/bot_run2.py bot2.jsonl ""
```

Harness notes for the next session:
- `answer_question(question, session=..., scope=None)` — there is **no** `session_id` kwarg.
- Startup must include `init_pool`, `init_client`, `init_qdrant`, `load_all` (annotations **and**
  entity resolver), `schema_introspect.load`, `refresh_scheme_years`. Omitting Qdrant makes any
  KNOWLEDGE-path case raise instead of answering.
- `ClarificationNeeded` is an **exception**, so a pause must be caught and resumed via
  `router.remember_pause`; a single-turn harness scores 18/20 as "paused" and tests nothing.
- Use the configured `session_store` singleton (`SessionStore()` needs 3 positional args).
- Don't run two harness instances at once — both write the same jsonl.

---

*DB and bot columns: VERIFIED live, 2026-10-10, VPN up. Raw column: recomputed and reconciled
against the DB (§3). Both PII partitions aggregated only — no row printed. No application code
was changed in this session.*
