# General tolerances for open dimensions

"Open" dimensions have no tolerance printed on them (R6, 2× R6, 2 × 45°, plain lengths and Ø).
They are governed by the general-tolerance standard the drawing names. This feature fills them in
automatically and lets inspectors teach a rule once.

## Data files (this folder)

| File | Content |
|---|---|
| `iso2768.json` | ISO 2768-1 linear, radius/chamfer and angular tables (classes f/m/c/v). IS 2102 maps here. |
| `tsds0407_general_tolerances.json` | TVS TSDS0407 *Unspecified Tolerance Chart*: 12 tables plus the lost-wax angle/roughness box. Every cell is the chart text exactly as printed (`±0.1`, `-0.1`, `+0.2`, `+0.4/-0.2`, `±1°30'`, `±0.8%`, `null` for `—`/`NA`). |

**The TSDS0407 file is an UNVERIFIED transcription.** It was read from `TSDS0407_2D_A4.pdf`, which has no
text layer, so it was transcribed from the rendered chart. TVS must check every cell against the
controlled copy before production use. Code never edits or extrapolates these values.

## Architecture (backend/app/services/general_tolerance/)

| Module | Job |
|---|---|
| `standards.py` | Loads the JSON files, parses chart cells, finds the band for a size (range convention option), returns blank + reason for null or out-of-range cells. |
| `classify.py` | Eligibility (section 1 of the spec) and feature category: linear, thickness, outside Ø, inside Ø (bored / drilled), radius, chamfer (size + angle), angular. The count in `n×` is ignored. |
| `geometry.py` | Runs inside the extractor (`tvs_extractor._extract`) and writes only `Feature.meta['gt_class']`. Decides inside vs outside Ø from the dimensioned circle (enclosed by outline → hole; free-standing → shaft) or section hatching. Measures the shorter leg of angles. If undecided, it flags instead of guessing. |
| `detect.py` | Reads the standard, process, sheet thickness, geometric class and any printed tolerance table from the title block (cell-based) and notes. Pages without a text layer go through the existing OCR path (PyMuPDF + Tesseract). |
| `engine.py` | **`resolve()` is the only precedence function** (see below). It also holds the TSDS0407 process → column mapping. |
| `store.py` | Rules (Postgres `general_tolerance_rules` + `general_tolerance_rule_audit`, or the JSON fallback `backend/data/general_tolerance_rules.json`). Org settings and per-drawing settings use the existing `app_settings` store (same pattern as the instrument rules). |
| `apply.py` | Inspection-report rows (`refresh_job`, called on every job save), TVS Excel rows, the drawing banner and Summary rows. |
| `api/routes/general_tolerances.py` | REST: standards, settings, rule CRUD/history/import/export, per-job state, drawing standard/process, per-feature process override (balloons or area), teach, preview. |

Row fields added to inspection-report rows: `tol_source`, `tol_flags`, `tol_auto`, `tol_angle`,
`tol_category`, `tol_size`. Every export shows a "Tolerance source" column and italicises auto values:
- inspection report PDF, Excel and CSV
- master report and CMQ_LAB
- master extraction (All_Dimensions sheet)
- JSON: the row fields plus a `general_tolerance` banner

The TVS extraction Excel keeps its columns. It fills Upper/Lower/Max/Min, appends the source and flags to
Notes, and adds a Summary section. `general_tolerances=False` (or `SMORX_GENERAL_TOL=0`) turns it off.
The answer-key evaluator runs with it off.

## Precedence (highest first) — `engine.resolve()`

1. Tolerance printed on the dimension (the dimension is not open; never touched)
2. Manual override on that balloon (persists by balloon number across revisions while the text is unchanged)
3. Rule scoped to this drawing (the process or standard the user chose for the drawing applies at level 5)
4. General-tolerance table printed on this drawing
5. Standard stated on the drawing: TSDS0407 with its process, or ISO 2768 / IS 2102 with its class
6. Part-family rule
7. Customer/organisation rule, then the organisation's default standard
8. None: blank + `None – <reason>`

- If the drawing states a standard or table (levels 4–5), levels 6–7 never apply. A matching family or
  customer rule produces a `standard conflict` warning instead.
- If a level finds its table but no value for that size (out of range, `—`, `NA`), the search stops
  with a blank and that reason.
- Conflicting standards on one drawing: the title block wins. If that's still ambiguous, the
  organisation's preferred standard applies, with a flag.

## Inspector guide (half page)

**What happens automatically.** When you balloon a drawing, the app reads its general-tolerance standard
from the title block or notes, for example "ISO 2768-mK" or "UNSPECIFIED TOLERANCES AS PER TSDS0407". It
then fills Tol −/Tol + for every open dimension: lengths, Ø, radii, chamfers and angles. Auto values are
shown in *blue italics* with a **GT** badge. Hover the badge to see the table/column used and any flags,
such as "hole type assumed: boring" or "leg length estimated". A red **!** means no value could be given
(e.g. R20 under TSDS0407 machining, where the chart has "—"); the reason is in the tooltip. Basic, reference,
fit, thread, GD&T and surface-finish values are never changed. Neither is any tolerance printed on the drawing.

**Setting the process (TSDS0407).** The banner above the report shows the standard and process, e.g.
"General tolerance: TSDS0407 – machining (from drawing notes)". If the drawing doesn't state the process,
the app assumes machining and the banner turns amber. Click **Change standard / process**, pick the right
process (and the sheet thickness for pressed parts), then **Apply to this drawing**. The choice is saved
for that part number and reused on every revision. For a machined bore on a cast part, the admin API
`feature-process` sets a per-feature override by balloon or by area.

**Teaching a rule.** Type a tolerance into an open dimension and leave the row. The app asks whether to use
it for this dimension only, or for all open dimensions of that kind and size range on this drawing, this
part family, or all drawings of this customer. The size range defaults to the band of the active standard;
you can edit it. The rule applies at once and to future uploads. A drawing's own standard always wins over
family or customer rules: you'll see a "standard conflict" note instead. To undo a manual value, click its
**M** badge. Rules and defaults are managed with the **General Tolerance Settings** button in the Inspection report toolbar, next to Instrument Settings.

## Open questions for TVS

1. **Range boundaries.** Is "4–16" *over 4 up to and including 16* (implemented default) or *from 4 up to
   but not including 16*? It's an admin setting until you confirm. Should the first band include its lower
   value? (Casting 1–4 currently includes 1.)
2. **Angular "basic size".** Implemented as the length of the shorter leg (same as ISO 2768). Is that the
   intended basis for the angular machining table? Is a chamfer angle's leg the chamfer size?
3. **How drawings state the process.** Which wording / title-block field do TVS drawings use (e.g. "AS
   CAST", "HPDC", material ADC12, a PROCESS cell)? Should HPDC + MACHINED drawings default the machined
   features to machining automatically?
4. **TSDS0407 vs IS 2102 / ISO 2768.** The chart's note 1 says to refer to IS 2102. If a drawing names both,
   which wins? Implemented: the title-block field, then the organisation's preferred standard.
5. **Gaps in the chart.** Radii and chamfers have no column under casting, pressing, forging, welding,
   plastic or rubber. Neither does outside Ø under pressing or forging. These are left blank and flagged.
   Should they use the length column?
6. **Positional and geometric tables.** These are loaded but applied to no size. How do TVS drawings mark a
   location dimension, and where is "geometric class 1/2/3" stated?
7. **Transcription check.** Please confirm the transcribed values, in particular: angular machining ≤4 =
   ±1°30′ and 4–16 = ±1°0′; forging thickness +1.5/−0.5 at 63–250; rubber > 500 = ±0.8 % / ±1.5 %.
