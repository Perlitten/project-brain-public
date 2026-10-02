# V3 composition decisions

Made 2026-08-01 against measured full-resolution screenshots in
`audits/2026-08-01/shots/before/` (Playwright + real Chromium, five viewports).
These are decisions, not options. Implement them as written; if a decision is
wrong, argue with a measurement and change this file.

The embedded browser pane renders a 1440px viewport into ~160px. That is a
property of the pane. `scripts/shoot_dashboard.py` shoots at true device
pixels and is the only acceptable evidence for a visual claim.

---

## 1. Command — the two-column composition is wrong, not just short

**Measured at 1440×900:** the right column (Event flow) ends at y≈1250 while the
left column runs to y≈1700. Roughly 450×450px of dead black under the right
column. This is not a content shortage to be padded — it is a grid that pairs a
bounded panel with an unbounded one.

Three further findings from the same raster, which change the fix:

- The triage board is three columns wide and two of them read `Clear.` — "Now 0"
  and "Monitor 0" are empty while "Next 4" carries everything. Two thirds of the
  most prominent block on the page is reserved for emptiness.
- Event flow shows eight rows and every one is
  `brain.database.session:close_database_…`, truncated identically. The panel is
  not short of space; it is short of distinct content.
- `1810 CHUNKS` and `1810 VECTORS` state the same number twice with no stated
  relationship, inside a 2×2 grid that spans 700px for four ~50px glyph runs.

**Decision.** Pair panels by whether they are bounded, not by reading order.

- Below the triage board, the two-column grid holds **Corpus** (left) and
  **Agent runs** (right). Both are bounded and both are tall, so neither column
  can outlive the other.
- **Event flow becomes a full-width bounded strip** beneath them, capped at a
  fixed row count with a link to the Event log. A tail is a tail; it does not
  earn a third of the page width and it can never fill a column vertically.
- The triage board collapses empty lanes to a single labelled line rather than
  reserving a full column for `Clear.` A lane with zero items is one row of
  text, not a third of the board.
- `VECTORS` is dropped from the stat grid. Vectors equal chunks by construction;
  showing the identity twice teaches nothing. State the relationship in the
  coverage bar's label instead.

Acceptance: at 1280, 1440 and 1600, no region below the fold is empty for more
than 200px of height while an adjacent region has content.

---

## 2. Runs — the preamble is a symptom; three columns carry no information

**Measured at 1440×900:** the first data row begins at y≈390 — 43% of the
viewport is consumed before any data. Four stacked bands cause it: filter pills,
panel head with a two-line description, the sortable-columns sentence, and the
column header row.

But the deeper defect is the table itself:

- `TRIGGER` reads `not recorded` in 20 of 20 rows.
- `COMMIT` reads `snapshot:5`, `snapshot:f`, `snapshot:6` — **one character** of
  hash, followed by the word `copy` twenty times.
- `STATUS` reads `completed` in 18 of 20 rows.
- The rightmost column is clipped by the panel edge.

Reducing the preamble alone would reveal more of a table that is three-sevenths
noise.

**Decision.**

- Delete `TRIGGER`. A column with one distinct value is not data.
- `COMMIT` shows 7 characters. Copy moves to a hover/focus affordance.
- `STATUS` becomes a dot plus text only where the status is not the norm;
  `completed` is the expected outcome and does not need a chip on every row.
- The filter pills merge into the panel head row — one band, not two.
- The panel description collapses to a single line, and the sortable-columns
  sentence moves into the sort control's `title`.

Acceptance: first data row above y=180 at 1440×900; at least 12 rows visible
without scrolling.

---

## 3. Panel headers — one grammar, three declared variants

Two historical patterns coexist. Neither is deleted today, so both keep
accreting.

**Decision.** The canonical grammar is:

```
[eyebrow?]  title  ................................  [actions?]
[description?]
```

with exactly three variants, and nothing else:

- **default** — title, optional one-line description, optional actions.
- **`--compact`** — title and actions on one line. No eyebrow, no description.
  This is the variant for panels inside a grid, where a second band costs more
  than it teaches.
- **`--stat`** — title plus a single dominant number, for panels whose whole
  purpose is one figure.

The eyebrow survives **only** where it names a category the operator could
filter by. `SERVICE`, `ACT`, `RECORD`, `CATALOGUE`, `COMPILED`, `IMPACT`,
`SCALE` do not qualify — `ACT` sits above "Quick Actions" and says nothing.
Eyebrows that merely restate the masthead's repository scope
(`STREAM · ALL REPOSITORIES · GLOBAL RUNTIME`) are redundant with the `/app`
chip and are deleted.

The superseded pattern's rules and classes are **deleted**, not left dormant.

---

## 4. Density — 14px/1.6 is prose spacing in a command centre

`--fs-body:14px` with `--lh-body:1.6` gives a 22.4px line box. For a screen
whose cells are overwhelmingly single-line values, that is 8px of air per row
bought for nothing.

**Decision.** Line-height becomes role-specific rather than global:

- prose (descriptions, empty states, notices) keeps 1.6 — it is genuinely read;
- table cells, metric labels, chips and nav items take 1.35;
- headings keep their existing tight tracking and take 1.25.

**Ghost buttons.** A resting hairline was added because a real action and a
caption differed by only 1.25:1 of grey. That fix is right in isolation and
wrong in bulk: Command alone carries nine ghost actions (`INSPECT`, `RUN SCAN`,
`TRIAGE ALL`, `DEPENDENCIES`, `RUNS`, `LOG`, `ALL`, `REFRESH`, `OLDER`), and
nine outlined boxes is chrome, not hierarchy.

A ghost button takes a resting border when it stands alone or in a pair. Three
or more adjacent ghost actions become a **segmented group**: one border around
the cluster, hairline dividers between members. The affordance is preserved and
the box count drops from nine to three.

---

## 5. Evidence

`scripts/shoot_dashboard.py` shoots 390×844, 768×1024, 1280×800, 1440×900 and
1600×1000, reports HTTP status, full page height and horizontal overflow per
screen, and exits non-zero on any overflow or non-200. Before/after pairs live
under `audits/<date>/shots/`.

Deployment requires visual acceptance against those rasters. Passing tests is
not acceptance — every regression this pass fixed shipped green.
