# NRLM Annotation Layer — How the YAMLs Are Built

This folder holds the hand-curated annotation layer for the **NRLM** scheme (National Rural
Livelihoods Mission, implemented in Meghalaya by MSRLS, the Meghalaya State Rural Livelihoods
Society, under the Community and Rural Development Department) for the Megh One AI
NLP-to-SQL bot. Everything here is derived from two inputs in `datasets/NRLM/`. Nothing in
this folder is generated at runtime: these files are the reviewed, SME-owned contract that the
retrieval and SQL-generation stages read.

It is a sibling of `Annotations/PMAY/README.md` and `Annotations/MGNREGA/README.md`. Read those
for the general method. This one records what is different about NRLM, which comes down to
five things:

1. **The unit is a Self Help Group, not a person, a house or a payment.** One row = one SHG.
   `COUNT(*)` is an SHG count. A member count is `SUM(male + female)`, never `COUNT(*)`.
2. **It is a snapshot, not a time series.** The file is the SHG register as at one extract
   date. There is no reporting year, no release date and no transaction history.
3. **`financial_year` is the year the SHG was FORMED, not a funding or reporting year.**
   The readiness document assumed "one SHG per financial year". The data disproves that (§4.1).
4. **The two money columns are cumulative per SHG with no date.** They can be added across
   SHGs but never split by year, never trended, never compared across extracts (§4.3).
5. **NRLM is already in `megh_db`**, unlike PMAY when its README was written. The live
   target is `curated.v_nrlm` over `curated.fact_nrlm_shg` (§7).

---

## 1. What is in this folder

| File | Layer | Answers the question | Status |
|---|---|---|---|
| `nrlm_schema_partitions.yaml` | **Semantic schema** (*stage2*) | *What columns exist, what do they mean, how are they aggregated?* | v1.1 - 29 view columns, 46 few-shot (14 refusals) |
| `nrlm_classification_rules.yaml` | **Clarification gate** | *Is the question answerable as asked, or must the bot ask or refuse first?* | v1.1 - 77 rules + 51 worked examples |
| `nrlm_default_rules.yaml` | **Defaults** | *What is assumed when the user does not say, and stated back?* | v1.1 - 74 rules + 28 worked examples |
| `nrlm_entity_resolver.yaml` | **Entity resolver** | *The user typed "EKH", "Mawkyrwat", "inactive", "revived groups": which stored value is that?* | v1.1 - 12 districts, 56 blocks, 55 constituencies, 197-name village registry, 45 worked examples |
| `nrlm_few_shot.yaml` | **Worked examples** | *Validated question-to-SQL pairs* | v1.1 - 103 pairs |
| `nrlm_foreign_key_augmentation.yaml` | **Join graph** | *Which joins are allowed, and which are prohibited* | v1.1 - 20 nodes, 23 edges (16 prohibited), 13 worked join decisions |
| `nrlm_response_template.yaml` | **Response shaping** | *How a number is phrased, and which caveats travel with it* | v1.1 - 82 templates, 38 follow-ups, 29 rendered answers |
| `README.md` | This guide | *How the layer is built and what must never change* | written 2026-10-05 |

The pipeline position of each file is the same as for PMAY:

```
user question
   |
   v
[ clarification gate ] <-- nrlm_classification_rules.yaml
   |  "funds released in 2021-22", "SHGs per GP", "growth in RF" -> ask or refuse
   v
[ defaults ]           <-- nrlm_default_rules.yaml
   |  "SHGs" -> active + inactive, "fund" -> both RF and CIF shown, top N -> 5
   v
[ entity resolution ]  <-- nrlm_entity_resolver.yaml
   |  "EKH" -> East Khasi Hills (274), "inactive" -> is_active = FALSE
   v
[ schema linking ]     <-- nrlm_schema_partitions.yaml
   |  "revolving fund" -> revolving_fund_amount, "formed in" -> formation year
   v
[ join check ]         <-- nrlm_foreign_key_augmentation.yaml
   v
[ SQL generation ] -> SQLGlot validation -> read-only execute   (few-shot from nrlm_few_shot.yaml)
   v
[ response ]           <-- nrlm_response_template.yaml
```

**Rule of thumb, unchanged:** if a fact is about a *column* it belongs in stage2. If it is about
a *value* it belongs in the resolver.

All seven YAMLs were written on 2026-10-05 in the order the user set: schema partitions,
classification rules, default rules, entity resolver, few-shot, foreign-key augmentation,
response template. §8 walks through how each one was built.

---

## 2. The source data

### 2.1 `datasets/NRLM/NRLM.csv`: the SHG register

- **40,629 rows x 20 columns**, comma-separated, one header row, no blank rows, no subtotals.
- One row = one SHG. `shg_code` and `id` are both unique on every row (§4.2).
- Profiled 2026-10-05 with pandas, every column read as text with `keep_default_na=False` so
  that blanks were counted as blanks rather than silently turned into NaN.

| # | Column | Type seen | Blank | Distinct | What it holds |
|---|---|---|---|---|---|
| 1 | `id` | int | 0 | 40,629 | Row id 1 to 40,629. **Not** equal to `shg_code` on any row. Export sequence; stability across extracts is unconfirmed. |
| 2 | `gp_name` | text | 0 | 3,990 | Free-text Gram Panchayat / village council name. No code. Not part of LGD (§4.6). |
| 3 | `shg_name` | text | 0 | 27,692 | SHG display name. **Not unique** (§4.2). 30,434 contain "SHG". |
| 4 | `shg_code` | int | 0 | 40,629 | SHG code, 2 to 46,368. **The natural key.** |
| 5 | `shg_type` | code | 0 | 3 | `New` 38,453 · `Revived` 1,744 · `Pre-Nrlm` 432 |
| 6 | `group_form` | ? | 0 | **1** | `00:00.0` on **every** row: an Excel time format left over from a corrupted export. **No information. Not loaded.** (§4.4) |
| 7 | `male` | int | 0 | 10 | 0 on 39,924 rows. Range 0 to 10. |
| 8 | `female` | int | 0 | 31 | Mode 10 (16,277). Range 0 to 38. |
| 9 | `active_sta` | bool | 0 | 2 | `TRUE` 39,432 · `FALSE` 1,197. Truncated name of *active status*. |
| 10 | `cif_amount` | number | 0 | 355 | Community Investment Fund. 0 on 29,788 rows (73.3%). Max 1,360,000. |
| 11 | `rf_amount` | number | 0 | 10 | Revolving Fund. 15,000 on 33,241 rows; 0 on 6,929. |
| 12 | `financial_year` | `YYYY-YY` | 0 | 30 | 1984-85 to 2022-23. **Formation year** (§4.1). |
| 13 | `mapped_district_lgd_code` | int | 0 | 12 | LGD district code. **Authoritative** over the name. |
| 14 | `mapped_district_lgd_name` | text | 0 | 12 | District name. 12 rows contradict the code (§4.5). |
| 15 | `mapped_block_lgd_code` | int | 0 | 56 | LGD block code. **Authoritative** over the name. |
| 16 | `mapped_block_lgd_name` | text | 0 | 56 | Block name. The same 12 rows contradict the code. |
| 17 | `mapped_village_lgd_code` | int | **2,032** | 4,976 | LGD village code. Blank = unmapped. |
| 18 | `mapped_village_lgd_name` | text | **2,032** | 4,712 | Village name. Blank on exactly the same rows as the code. |
| 19 | `mapped_constituency_name` | text | **2,032** | 55 | Assembly constituency, mixed case (`Mawkyrwat`). |
| 20 | `mapped_constituency_name_and_number` | text | **2,032** | 55 | Number + upper-case name (`36 MAWKYRWAT`). |

No column has leading or trailing whitespace. Every amount is a whole number. No negative values
anywhere.

### 2.2 `datasets/NRLM/NRLM_Data_Readiness_Document.pdf`: the client questionnaire

An 8-page document written by the project team **for the client** to complete. It is not a data
dictionary and gives no answers; it lists what the team *assumed* and what the client must confirm.
Sections:

| § | Content | Use to this layer |
|---|---|---|
| 1 | Purpose: move from iterative corrections to a frozen, signed-off baseline | Context only |
| 2 | Dataset overview: owner MSRLS / C&RD; source system to be stated (MSRLS MIS, LokOS or NRLM national MIS); **assumed grain "one SHG per financial year"**, candidate key `shg_code + financial_year` | The grain assumption is **wrong**; see §4.1 |
| 3 | Field inventory: the 20 fields with expected type, mandatory/optional and the question to confirm for each | The source for every "UNCONFIRMED" note in stage2 |
| 4 | Ten written clarifications required (listed in §5 below) | Every open question in this layer traces to one of these |
| 5 | Client internal validation checklist A to G, including the NRLM-specific checks in G | Each check in G was run against the file; results in §5 |
| 6 to 11 | Readiness package contents, acceptance validation and criteria, handover flow, change control, submission control sheet | Process only; no data content |

The document's key warning, which this layer adopts as a rule: *if `cif_amount` and `rf_amount` are
cumulative, `financial_year` cannot be used for year-on-year analysis and this must be recorded as a
constraint.* The data says they are (§4.3), so that constraint is recorded.

---

## 3. CSV to YAML: the derivation pipeline

The steps run on 2026-10-05, in order, and what each one feeds:

```
# Step 1 - shape and header check
#   -> 40,629 x 20, single header, no blank rows                     feeds stage2 row counts
# Step 2 - per-column census (blank, distinct, top values, max length)
#   -> the §2.1 table                                                 feeds stage2 sample_values
# Step 3 - key uniqueness: id, shg_code, shg_name, (shg_code, financial_year)
#   -> shg_code unique on its own: grain is one SHG, not SHG x FY     feeds stage2 grain + resolver ids
# Step 4 - group_form census
#   -> one value, "00:00.0", on all rows: dead column                  feeds stage2 exclusion
# Step 5 - financial_year shape vs fund amounts
#   -> CIF share falls from ~60% (2015-16) to 0% (2022-23): formation year,
#      and the money is cumulative                                     feeds the gate's year refusals
# Step 6 - code <-> name agreement for district, block, village, constituency
#   -> 12 contradicting rows; 197 village names with >1 code           feeds resolver ambiguity registry
# Step 7 - blank pattern on the village and constituency columns
#   -> all four blank together on the same 2,032 rows                   feeds the 'Unresolved' rule
# Step 8 - duplicate search on every column except id and shg_code
#   -> 5 pairs (10 rows)                                              feeds is_possible_duplicate caveat
# Step 9 - member-count norm (NRLM norm 10 to 20, 5 minimum)
#   -> 5 zero, 74 at 1-4, 99 above 20                                  feeds member_count_out_of_norm caveat
# Step 10 - totals by district (SHGs, active, members, RF, CIF)
#   -> the golden answers in §9
```

---

## 4. Verified facts (profiled 2026-10-05)

### 4.1 `financial_year` is the formation year: the headline finding

The readiness document assumed one row per SHG **per financial year**. The data shows:

- `shg_code` is unique on all 40,629 rows. No SHG appears twice, so there is no second year for any
  group.
- The 30 years run from **1984-85** (1 SHG) to **2022-23** (1,680). A register of funding years
  would not have one row in 1984-85 and none in 2023-24 or later.
- The distribution is a formation curve: it peaks in 2019-20 (10,604) and 2020-21 (11,354), the NRLM
  mobilisation drive.
- Older groups hold more money. The share with any CIF falls steadily with the year:

| financial_year | SHGs | % with RF > 0 | % with CIF > 0 | Mean CIF (Rs) |
|---|---|---|---|---|
| 2015-16 | 582 | 91 | 60 | 83,411 |
| 2016-17 | 1,279 | 88 | 59 | 77,046 |
| 2017-18 | 3,634 | 79 | 45 | 69,002 |
| 2018-19 | 4,368 | 90 | 52 | 45,449 |
| 2019-20 | 10,604 | 83 | 31 | 20,997 |
| 2020-21 | 11,354 | 91 | 17 | 9,949 |
| 2021-22 | 6,028 | 74 | 4 | 2,443 |
| 2022-23 | 1,680 | 42 | 0 | 193 |

That is what a cumulative amount on a group formed in year X looks like: the longer a group has
existed, the more funding rounds it has passed through.

**Consequence:** "SHGs formed in 2020-21" is answerable. "Funds released in 2020-21", "RF disbursed
this year", "growth in CIF year on year" are **not**: the bot must refuse them and explain why. The
live DB agrees: `fact_nrlm_shg.formation_year_key` is commented *"NOT a funding or reporting year.
Meaning pending client confirmation (NR-02)."*

Pre-NRLM years: 645 SHGs carry a year before 2011-12 (NRLM's launch). 432 of all SHGs are typed
`Pre-Nrlm`, so the year is not just a relabelled `shg_type`. Report the year as given.

### 4.2 Keys and names

| Candidate | Unique? | Use |
|---|---|---|
| `shg_code` | **Yes**, 40,629 / 40,629 | The key. Counting, lookup, joins within NRLM. |
| `id` | Yes, but an export sequence (1 to 40,629) | Lineage only (`source_row_id` in the DB). Never a user-facing identifier. |
| `shg_name` | **No**: 27,692 distinct | Display only. "Iatreilang Shg" alone is 295 SHGs. 2,684 names appear in more than one village; 174 rows repeat a name inside the same village. |

A name lookup ("show me Iatreilang Shg") must return a list with village, block and `shg_code`, or ask
which one is meant. It must never assume the first match.

### 4.3 The money columns

| | `rf_amount` (Revolving Fund) | `cif_amount` (Community Investment Fund) |
|---|---|---|
| Non-zero SHGs | 33,700 (82.9%) | 10,841 (26.7%) |
| Total | **Rs 50.86 crore** | **Rs 98.48 crore** |
| Shape | 10 values only: the NRLM norm 15,000 on 33,241 SHGs; 30,000 on 246 (likely two rounds); 13,500 / 12,000 / 10,500 / 9,000 / 7,500 / 5,000 / 20,000 on the rest | 355 values; median 50,000 among non-zero; 39 SHGs above 5 lakh; max 13.6 lakh (shg_code 3329, East Khasi Hills) |
| Inactive SHGs | 0 on all 1,197 | 0 on all 1,197 |
| Unit | Rupees, **assumed**. Client to confirm (DB note NR-07). | Same. |

Rules this sets:
- Both are **additive across SHGs** (sum by district, block, type, status).
- Neither is **additive across time or extracts**. No date exists.
- Every aggregate answer states the unit (rupees, or crore when divided by 1e7) and that the figure
  is cumulative as at the extract date.
- 0 means "no fund recorded". There are no blanks to distinguish (clarification 7 in §5).
- The 39 CIF values above 5 lakh are real rows but unconfirmed (DB note NR-25). Rankings by CIF will
  surface them first; the response carries that caveat.
- **Inactive groups show 0 for both funds** (DB note NR-15). Whether money was clawed back, never
  released, or zeroed on deactivation is unknown. "Funds to inactive SHGs" returns 0 with that
  caveat, not a bare zero.

### 4.4 `group_form` is dead

Every row holds `00:00.0`: the display of a date/time cell whose date part was lost when exported
from Excel (`mm:ss.0` format). The readiness document calls it *"the single largest open item"*. It
carries no information in this file and is **not loaded** to `fact_nrlm_shg`. Questions about
*formation date* (day or month) are refused; *formation year* uses `financial_year` (§4.1).

### 4.5 Geography

- **12 districts, 56 blocks, 4,976 village codes, 55 constituencies.** All Meghalaya.
- **Codes are authoritative, names are not.** 12 rows carry district code 657 (East Jaintia Hills)
  and block code 2000 (Saipung) with the names *Eastern West Khasi Hills / Mairang*. All 12 are
  village Tangnub. The DB keeps the code and flags them `has_name_code_conflict = TRUE`.
- **Eastern West Khasi Hills is a real district** here: LGD code 740, 1,610 rows, blocks Mairang (1,069)
  and Mawthadraishan (541). (A CM Elevate DB comment calls the same name "not a real district"; for NRLM it is the
  LGD name of code 740.) Its resolver alias must not be confused with *West Khasi Hills* (279).
- Village code to name is 1:1 and each village sits in one block. **197 village names map to more
  than one code**, so a village name alone is ambiguous; the resolver must ask for the block.
- **2,032 SHGs (5.0%) have no village**, and the same rows have no constituency. The district and
  block are present on every one of them. In the DB they point at their block's `Unresolved`
  placeholder (`entity_type = 'Unresolved'`, `on_roster = FALSE`). **Count them in district and
  block totals; exclude them from village counts and village lists.** Largest gaps: West Garo Hills
  440, East Khasi Hills 355, Ri Bhoi 345.
- Blocks per district: East Khasi Hills 11, West Garo Hills 8, Ri Bhoi / South Garo Hills /
  West Khasi Hills 5, North Garo Hills / South West Garo Hills / West Jaintia Hills 4,
  East Garo Hills / East Jaintia Hills 3, Eastern West Khasi Hills / South West Khasi Hills 2.
- In the DB, `dim_geography.lgd_district` and `lgd_block` are stored **upper case**; a mixed-case
  equality filter returns zero rows silently (same trap as Focus+).

### 4.6 `gp_name` is not a geography level

Meghalaya is mostly outside the Panchayati Raj system, so `gp_name` holds traditional village or
council names typed as free text. It has no code and is not part of the District > Block > Village
hierarchy. On mapped rows it differs from the LGD village name on 10,817 of 38,597 (28.0%, exact
case-insensitive comparison; the DB comment quotes ~23% under a looser match). Names repeat across
blocks. **"SHGs per GP"** is answered only with that caveat, or redirected to village or block.

### 4.7 Constituencies

The two constituency columns agree on every row: each of the 55 names maps to exactly one numbered
form. Three are spelled differently: `Ampathi` / `53 AMPATI`, `Sohing` / `23 SOHIONG`,
`Tikrikila` / `45 TIKRIKILLA`. Four more are truncated in the numbered field: `Sutnga-saipung` /
`5 SUTNGA`, `Rambrai jyrngam` / `33 RAMBRAI`, `William Nagar` / `43 WILLIAM`, `Rongara-siju` /
`58 RONGARA`. All forms are resolver aliases. Numbers 16-19 and 51 are absent (no rural SHG in
those seats). Constituency is blank on the
2,032 unmapped rows, so a constituency total will not add up to the state total; the response must
say so. In the DB these are `constituency_name_raw` and `constituency_number_raw` (the second
actually holds the *name and number* string).

### 4.8 Members

- Total members **410,847**: female 409,249 (**99.6%**), male 1,598. 39,924 SHGs have no male member.
- NRLM norm is 10 to 20 members (5 minimum in special cases). 29,552 SHGs are 10 to 20, 10,899 are
  5 to 9, 74 are 1 to 4, **5 are 0**, 99 are above 20 (max 38). The DB flags the 178 outside
  {>= 5, <= 20} as `member_count_out_of_norm`.
- Whether counts are current or as at formation is unconfirmed (clarification 5).

### 4.9 Duplicates

Five pairs (10 rows) are identical in every field except `id` and `shg_code`: same name, GP, village,
year, members and money. Probable double registration (DB note NR-24). Both rows are kept and flagged
`is_possible_duplicate`. Counts include them; the response mentions them only if a question lands on
one.

### 4.10 Status and type

| shg_type | Active | Inactive | Total |
|---|---|---|---|
| New | 37,313 | 1,140 | 38,453 |
| Revived | 1,689 | 55 | 1,744 |
| Pre-Nrlm | 430 | 2 | 432 |
| **Total** | **39,432** | **1,197** | **40,629** |

`active_sta` has no as-of date and no reason code (clarification 2). "Dissolved", "closed" and
"defunct" are mapped to inactive by the resolver only with a stated assumption.

---

## 5. The ten readiness clarifications, answered from the data

| # | Clarification (readiness doc §4) | What the file shows | Status |
|---|---|---|---|
| 1 | Definition of `group_form` | One value, `00:00.0`, on all rows. Corrupted. | **Blocked**: column dropped |
| 2 | Definition of `active_sta` | Boolean TRUE / FALSE. No as-of date. | Partly answered |
| 3 | Grain | One SHG per row. `shg_code` unique. Not SHG x FY. | **Answered by data** |
| 4 | Meaning of `gp_name` | Free text, no code, 28% differ from LGD village. | Treat as advisory |
| 5 | Member counts | male + female, no third category. 5 zero, 178 out of norm. | Current vs at-formation unknown |
| 6 | Fund amounts annual or cumulative | Pattern says cumulative (§4.1 table). | **Treated as cumulative**: no YoY |
| 7 | Zero vs blank in fund amounts | No blanks exist; 0 only. | Answered |
| 8 | Federation linkage (VO / CLF) | No VO or CLF id in the file. | **Out of scope**: refuse |
| 9 | Dissolved / merged / renamed groups | Only an active flag; no history. | Unknown |
| 10 | Bank linkage, savings, loans | Not present. | **Out of scope**: refuse |

Readiness §5.G NRLM-specific checks, run on this file:

| Check | Result |
|---|---|
| LGD code and name pairs match | **Fail on 12 rows** (§4.5) |
| No non-Meghalaya geography | Pass |
| Village mapping gaps quantified | 2,032 blank (5.0%), all on the same rows as blank constituency |
| Constituency fields agree | Pass (3 spelling differences, same constituency) |
| `shg_code` present, no placeholders | Pass |
| No duplicate `shg_code` + `financial_year` | Pass, but 5 near-duplicate pairs under different codes |
| male and female non-negative, not both zero | **Fail on 5 SHGs** (both zero) |
| No negative amounts | Pass |
| Formation values plausible, no future dates | `group_form` unusable; `financial_year` max 2022-23, no future years |

---

## 6. Question scope: what the YAMLs must answer, ask about, or refuse

| Answer | Ask first | Refuse, with the reason |
|---|---|---|
| SHG counts by district, block, village, constituency, type, status, formation year | A village name with more than one code (197 names) | Funds released / disbursed **in a year** |
| Members (total, female, male), averages per SHG | An SHG name shared by more than one group | Year-on-year growth of RF or CIF |
| RF and CIF totals and averages by geography, type, status | "Fund" with no qualifier: show both RF and CIF, or ask | Formation date or month |
| "SHGs formed in FY X", "formed before NRLM" | "SHGs per GP": warn or redirect to village/block | Village Organisation / CLF / federation questions |
| SHGs with no CIF / no RF, out-of-norm membership, inactive SHGs | | Savings, bank linkage, loans, credit |
| Top or bottom N districts / blocks by count, members or fund | | Individual members (no names, no member-level rows) |
| Cross-scheme counts **aggregated separately** per village or district | | Any row-level join of NRLM to another scheme's fact |

---

## 7. Database alignment: NRLM is in `megh_db`

`SCHEMA_FOR_DEVELOPERS.md` (generated 2026-10-05) contains three NRLM objects:

| Object | Role |
|---|---|
| `curated.fact_nrlm_shg` | The fact, ~40,629 rows, unique on `shg_code`. Loaded as a **snapshot**: a new extract replaces the table (`--force`). |
| `curated.v_nrlm` | **The query surface the YAMLs target.** Fact + `dim_geography` + `dim_year` (as formation year). Adds `total_members`. |
| `meta.v_reconciliation_nrlm` | `raw_rows = curated_rows + quarantined`. Expected on the 2026-09 file: 40,629 = 40,629 + 0. |

CSV column to view column:

| CSV | `v_nrlm` | Note |
|---|---|---|
| `id` | not exposed (`source_row_id` on the fact) | lineage |
| `shg_code` / `shg_name` / `shg_type` | same names | |
| `group_form` | **not loaded** | dead (§4.4) |
| `male` / `female` | `male_members` / `female_members`, plus `total_members` | |
| `active_sta` | `is_active` | boolean |
| `rf_amount` | `revolving_fund_amount` | |
| `cif_amount` | `cif_amount` | |
| `financial_year` | `formation_year_key`, `formation_financial_year`, `formation_financial_year_short` | **named as formation year** in the DB |
| `mapped_district_lgd_code` / `mapped_block_lgd_code` | `district_lgd_code` / `block_lgd_code` | authoritative |
| district / block / village names | `lgd_district` / `lgd_block` / `lgd_village_name` (from `dim_geography`, **upper case**) | raw names kept on the fact only |
| `mapped_village_lgd_code` | `village_code` | Unresolved placeholder code when blank |
| `gp_name` | `gp_name` | |
| `mapped_constituency_name` / `..._and_number` | `constituency_name_raw` / `constituency_number_raw` | |
| (derived) | `entity_type`, `on_roster`, `has_geo_conflict`, `is_possible_duplicate`, `has_name_code_conflict`, `member_count_out_of_norm` | flags added by the loader |

The DB comments cite client issue codes **NR-02** (formation year meaning), **NR-07** (rupee unit),
**NR-15** (inactive SHGs at 0), **NR-24** (duplicate pairs) and **NR-25** (CIF above 5 lakh). The full
NR issue list is not in this folder; the YAMLs reference those codes as given.

Not yet checked against the live DB: the `dim_scheme` row for NRLM and the `semantic.join_graph`
prohibitions for `fact_nrlm_shg`. Check both before writing `nrlm_foreign_key_augmentation.yaml`.

---

## 8. How each YAML was built (2026-10-05, step by step)

Every file follows the shape of its PMAY sibling (`Annotations/PMAY/pmay_*.yaml`, v2.0): same
top-level keys, same field names, same rule/condition style, so the pipeline loads NRLM exactly
as it loads PMAY. What changed is the content, driven by §4.

### Step 1 - `nrlm_schema_partitions.yaml` (stage2)

Built from `SCHEMA_FOR_DEVELOPERS.md` for structure and from the CSV profile for values.

- The `database` block points at `curated.v_nrlm`; `volumes_verified: false` because there is no
  read credential for `megh_db` on this machine.
- **29 column documents** for `v_nrlm`, each with `source_column`, `meaning`, `type`, `category`,
  `nlp_sql_priority`, `sql_operations`, `synonyms`, `sample_values` and `business_rules`, in PMAY's
  order: keys, SHG attributes, time, geography, members, money, data-quality flags.
- `fact_nrlm_shg` documented for lineage: 4 declared FKs, 2 CHECK constraints, the columns not in
  the view, and `group_form` under `dropped_at_ingest`.
- `semantic_rules` carries the three NRLM-specific rules: `unresolved_rule`, `time_meaning:
  FORMATION`, and `financial_measures.cumulative_rule`.
- `nlp_sql_rules.mandatory_predicate` is deliberately **none**: PMAY's `NOT is_placeholder` has
  no NRLM counterpart.
- 27 few-shot examples, 8 of them refusals with `sql: null`.

### Step 2 - `nrlm_classification_rules.yaml` (the gate)

77 conditions, ordered as in PMAY: what the source cannot answer first, then the NRLM time-and-money
refusals, data quality, measure ambiguity, time, geography, identifiers and answer shape.

- Not-held conditions come from readiness clarifications 8-10 and the absent fields: federations,
  savings, bank linkage, loans, member detail, social category, activity, grading, formation date
  and urban NULM.
- `money_in_a_year_requested` and `money_trend_or_growth_requested` are marked `critical`. They are
  the most important rules in the folder.
- Ambiguities measured from the data: beneficiaries (40,629 SHGs vs 410,847 members), Pre-NRLM
  (432 typed vs 645 formed before 2011-12), "New" (type vs recent), village names (197 shared) and
  SHG names (27,692 for 40,629 SHGs).

### Step 3 - `nrlm_default_rules.yaml`

74 defaults, each with `default_value`, `assumption_text` and, where useful, `sql_effect`. A script
checked that **no condition appears in both the gate and the defaults** (PMAY house rule: the gate
would always win).

- Population defaults come first: all SHGs (active and inactive); Unresolved included in totals and
  excluded from village figures; duplicates and out-of-norm groups included.
- A money question with no period defaults to "as at the current extract". That is not a time
  default; it is a statement every money answer must carry.
- Year defaults exist **only** for formation questions (latest 2022-23, earliest 1984-85).
- Regions expand by LGD code: Khasi Hills 12,557 SHGs, Garo Hills 17,565, Jaintia Hills 6,799,
  Ri Bhoi 3,708.

### Step 4 - `nrlm_entity_resolver.yaml`

About 2,060 lines. The hand-written parts (normalisation, the 8-stage matching pipeline, blocked
pairs, output contract, overloaded terms, collisions, worked examples, maintenance) follow PMAY.
The catalogues were **generated by a pandas script that groups the CSV by LGD code**, so every
count matches what the database stores:

| Catalogue | Size | Carries |
|---|---|---|
| district | 12 | code, acronym, region, SHGs, active, members, RF, CIF, blocks, villages, unresolved, aliases |
| block | 56 | code, district, SHGs, villages, `also_a` (village / constituency), aliases |
| constituency | 55 | number, numbered form, districts, SHGs, every spelling and truncated form |
| village ambiguity registry | 197 names | 160 resolve by district, 37 need the block |
| shg_type / shg_status / formation_year | 3 / 2 / 30 | counts per value |

New compared with PMAY: the constituency and gp_name dimensions, the shg_code and shg_name
identifiers, three-way name collisions (block, village and constituency share up to 26 names), and
blocked pairs such as West Khasi Hills vs Eastern West Khasi Hills.

### Step 5 - `nrlm_few_shot.yaml`

63 question-to-SQL pairs in PMAY's `sql_generation_examples` format, grouped as SHG counts,
formation year, members, funds, villages, constituency and GP, single-SHG and name lookups, data
quality, and two cross-scheme examples that use the aggregate-then-combine pattern. 35 carry an
`expected` value computed from the CSV. As in PMAY, refusals live in stage2, not here.

### Step 6 - `nrlm_foreign_key_augmentation.yaml`

20 nodes and 23 edges:

- the 4 declared FKs of `fact_nrlm_shg`, plus the view-to-fact lineage edge;
- **2 allowed aggregate-then-combine patterns**, by village_code and by district;
- **16 prohibited edges**: row-level joins from NRLM to every other scheme's fact, to both
  cross-scheme views (which do not include NRLM), to `dim_geography_alias` and to Producer Groups
  (tempting and wrong); shg_name and constituency/block self-joins; and lining the formation year
  up against another scheme's money year.

### Step 7 - `nrlm_response_template.yaml`

82 templates and 38 follow-up rules in PMAY's `formatting` / `templates` / `follow_up_rules` shape.
The three caveats every NRLM answer must carry have their own lines: `shg_grain_note`,
`cumulative_money_note` and `formation_year_note`. Refusals have their own wording
(`money_in_year_refused`, `money_trend_refused`, `federation_not_held` and others), each followed by
an offer of what *can* be answered.

### Checks run on all seven

- Every file parses with `yaml.safe_load`.
- No condition is in both the gate and the defaults, and no condition repeats within either file.
- Figures were re-derived from the CSV. Four errors were caught and fixed before finishing: the
  pre-2011-12 count (first written as 1,021; correct 645), the count of SHGs formed before 2014-15
  (1,050; correct 931), a few-shot sort that would have put "before 2014-15" last, and the README
  district table, which had summed female members only.
- **No query has been run against `megh_db`.** §7 and the resolver's `open_verification_tasks`
  list what must be checked there.

---

## 9. Golden questions (expected answers from the CSV, 2026-10-05)

| # | Question | Expected |
|---|---|---|
| 1 | How many SHGs are there in Meghalaya? | **40,629** (39,432 active, 1,197 inactive) |
| 2 | Total SHG members? | **410,847** (409,249 women, 1,598 men) |
| 3 | Total Revolving Fund? | **Rs 50.86 crore**, cumulative as at the extract |
| 4 | Total CIF? | **Rs 98.48 crore**, cumulative as at the extract |
| 5 | Which district has the most SHGs? | **East Khasi Hills, 6,768** (then West Garo Hills 6,526) |
| 6 | Which district has the most CIF? | **West Jaintia Hills, Rs 18.89 crore** |
| 7 | How many SHGs were formed in 2020-21? | **11,354** |
| 8 | How many revived SHGs? | **1,744** (1,689 active) |
| 9 | How many villages have an SHG? | **4,976** village codes; 2,032 SHGs excluded as unmapped |
| 10 | RF released in 2021-22? | **Refuse**: no release date; amounts are cumulative per SHG |
| 11 | SHGs with no CIF? | **29,788** (incl. all 1,197 inactive) |
| 12 | How many SHGs in Mawkyrwat constituency? | **1,092** |

District totals for reference, grouped by **LGD code** as `curated.v_nrlm` does:

| District | LGD | SHGs | Active | Members | RF (Cr) | CIF (Cr) |
|---|---|---|---|---|---|---|
| East Garo Hills | 273 | 2,700 | 2,582 | 27,020 | 3.28 | 3.57 |
| East Jaintia Hills | 657 | 2,744 | 2,692 | 26,601 | 3.76 | 7.75 |
| East Khasi Hills | 274 | 6,768 | 6,527 | 70,636 | 7.62 | 10.66 |
| Eastern West Khasi Hills | 740 | 1,610 | 1,559 | 16,743 | 2.23 | 7.22 |
| North Garo Hills | 656 | 2,832 | 2,806 | 28,295 | 3.42 | 4.63 |
| Ri Bhoi | 276 | 3,708 | 3,618 | 38,276 | 5.25 | 8.85 |
| South Garo Hills | 277 | 2,016 | 2,000 | 20,434 | 2.54 | 4.74 |
| South West Garo Hills | 663 | 3,491 | 3,454 | 34,196 | 4.69 | 6.99 |
| South West Khasi Hills | 658 | 1,867 | 1,811 | 19,671 | 2.38 | 7.89 |
| West Garo Hills | 278 | 6,526 | 6,248 | 64,514 | 6.90 | 7.74 |
| West Jaintia Hills | 275 | 4,055 | 3,949 | 41,049 | 5.67 | 18.89 |
| West Khasi Hills | 279 | 2,312 | 2,186 | 23,412 | 3.11 | 9.53 |
| **Total** | | **40,629** | **39,432** | **410,847** | **50.86** | **98.48** |

Grouping by the CSV's district **name** instead moves the 12 conflicting Tangnub SHGs from East
Jaintia Hills (2,744 by code, 2,732 by name) to Eastern West Khasi Hills (1,610 by code, 1,622 by
name). Every golden figure for the DB uses the code-based numbers above.

> Correction, 2026-10-05: the first version of this table grouped by source name and its Members
> column summed **female members only**. Both are fixed above.

---

## 10. Regeneration checklist

Run whenever a new extract arrives. The table is replaced, not appended, so every figure can move.

- [ ] Re-run the §3 steps. Diff every count against §4 and §9; explain every change.
- [ ] Re-check `shg_code` uniqueness. If an SHG appears twice, the grain has changed: stop and
      re-design (the readiness doc's SHG x FY grain may have arrived).
- [ ] Re-check whether `group_form` now carries a real value.
- [ ] Re-check the formation-year / fund shape (§4.1). If amounts gain a date, the year refusals in
      the gate must be lifted.
- [ ] Re-run code to name agreement, the village-name ambiguity list, and the duplicate-pair search.
- [ ] New district / block / village / constituency / type values: **append** to the resolver.
      Never delete one.
- [ ] Watch the client answers to NR-02, NR-07, NR-15, NR-24, NR-25 and the ten clarifications;
      each one resolved changes a caveat.
- [ ] Bump versions, update `last_generated` and row counts, validate every YAML with
      `yaml.safe_load`, and re-run §9.

---

## 11. House rules

- `datasets/NRLM/` is a **read-only input**. Never write to it.
- **One row is one SHG.** Never call an SHG count "beneficiaries" or "members".
- **Never split money by year.** The only year in the data is the formation year.
- **State the unit and that money is cumulative**, every time money is reported.
- **Codes over names** for district and block. **Exclude Unresolved** from village counts only.
- Never row-join NRLM to another scheme's fact. Aggregate each side first.
- Prefer **ask over guess**: ambiguous village name, shared SHG name, bare "fund".
- **No YAML in this folder is created or edited without an explicit instruction naming the file
  and the change.**

---

## 12. Change log

| Date | Change |
|---|---|
| 2026-10-05 | Folder created with seven empty YAML stubs (`nrlm_classification_rules`, `nrlm_default_rules`, `nrlm_entity_resolver`, `nrlm_few_shot`, `nrlm_foreign_key_augmentation`, `nrlm_response_template`, `nrlm_schema_partitions`) mirroring `Annotations/PMAY/`. |
| 2026-10-05 | `README.md` written. `NRLM.csv` profiled in full (40,629 x 20) and the readiness document read end to end. All CSV figures checked against the live `fact_nrlm_shg` comments in `SCHEMA_FOR_DEVELOPERS.md`, and they agree (2,032 unmapped, 12 name/code conflicts, 5 duplicate pairs, 246 at RF 30,000, 39 CIF > 5 lakh, 5 / 74 / 99 out-of-norm member counts, 1,197 inactive at 0). Headline: **the grain is one SHG, `financial_year` is the formation year, and money is cumulative with no date.** No YAML content written. |
| 2026-10-05 | All seven YAMLs written (v1.0) in the order schema partitions -> classification rules -> default rules -> entity resolver -> few-shot -> foreign-key augmentation -> response template, each in the shape of its PMAY v2.0 sibling (see §8). README updated: file status table, new §8, constituency truncations in §4.7, and the §9 district table regrouped by LGD code with total members (the first version summed female members only). |
| 2026-10-05 | v1.1: more examples in all seven YAMLs, every figure computed from `NRLM.csv` grouped by LGD code. Few-shot 63 -> 103 pairs; stage2 few-shot 27 -> 46 (14 refusals); new `worked_examples` in the gate (51), defaults (28) and foreign-key file (13 join decisions); resolver worked examples 16 -> 45; new `rendered_examples` in the response template (29 finished answers in Indian number format). New findings surfaced by the examples: 7 blocks and 7 constituencies hold exactly zero CIF; 13 of the 39 SHGs above Rs 5 lakh CIF are in Pynursla block; 204 of the 246 SHGs at Rs 30,000 RF are in Gasuapara and Resubelpara; Mawhati block (69 SHGs) vs Mawhati constituency (519) is the costliest name collision. The extra keys (`worked_examples`, `rendered_examples`) are additive - PMAY's files do not have them, so a loader that reads only the PMAY keys ignores them. |
