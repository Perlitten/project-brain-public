# V3 Command screen — product contract

Distilled from the design system's `ui_kits/cockpit/screen-overview.js` (project
`ab40a0e5-c3a4-40af-9f9d-0d0ddba77a7d`). That file is the authority; this is the
structure a server-rendered port must reproduce. The React internals do not
transfer — the composition, the ordering, the lane logic and the voice do.

**The screen's thesis, quoted from its own header comment:** one narrative, one
primary action, then a triaged queue. Everything on the screen is a jump into
the harness — nothing is a dead summary. Production's current Overview violates
this: it presents parallel summary cards and makes the operator reconcile them.

## 1. Narrative section (top, full width)

A `<section>` with a left spine coloured by the worst live condition
(`--bad-fg` when retrieval is compromised). Inside, a two-column flex that wraps:

**Left column** (`flex: 1 1 460px`, `min-width: 280px`), in this exact order:
1. Status line: a `Signal` dot, then an uppercase mono micro label naming the
   condition in the system's own words (the reference reads `Retrieval
   compromised`), then `· N events since <last visit>` in `--text-3`.
2. `<h1>` at `--fs-title`, `--tracking-tight`, `text-wrap: balance` — **a
   sentence stating the conclusion**, not a page name. Reference: "Scheduled
   reindex stopped 9 days ago".
3. A paragraph at `--fs-body`, `max-width: 62ch`, explaining the causal story in
   prose, with the repository path and the frozen commit as inline links/code.
   Reference: "n8n went unreachable, so <repo> froze at <commit> — N commits and
   812 unvectorised chunks behind. Every pack built since is stale."
4. Action row: ONE primary Button (the remediation, e.g. `Reindex now`), one
   ghost Button (`Inspect`, opens the lead insight), and a keyboard hint.

**Right column** (`flex: 0 1 260px`): a 2×2 grid of metric tiles, 1px gaps over
`--border-subtle` so the gaps read as hairlines, each tile `--surface-inset`.
Tile = value at `--fs-num-sm` weight 600 in the tone colour, label under it at
mono 10px `--text-3`. Reference tiles: commits behind (bad), chunks unvectorised
(warn), packs affected (warn), days since last index (bad).

**Feedback**: a `Notice` slot under the columns for the result of the primary
action (queued → complete), `aria-live`.

**Footer band** (`--surface-inset`, separated by a hairline): an uppercase mono
micro heading — the reference literally reads "Why one fix clears three
findings" — followed by a `CausalChain` of the lead insight's cause steps.

## 2. Queue section

`SectionHead` titled `Queue` with the total count and a ghost action `Triage
all →` linking to Insights. Then a responsive grid (min column 300px) of exactly
three lanes:

| id | label | hint |
|---|---|---|
| `now` | Now | blocking correct answers |
| `next` | Next | degrades quality |
| `monitor` | Monitor | no action needed |

**Lane assignment is a pure function of the record and must be reproduced
exactly:** `critical` → `now`; `warning` → `next` when its status is `new`,
otherwise `monitor`; anything else → `monitor`.

Lane header: label at `--fs-heading` weight 600, count in mono `--fs-micro`
`--text-3`, hint right-aligned in mono 10px `--text-disabled`. Empty lane renders
the single word `Clear.` at `--fs-sm` `--text-3` — not an illustration, not a
panel.

Queue item: a full-width button, grid `auto minmax(0,1fr) auto`, with a left
spine in the severity tone, a mono rank number, the title (single line,
ellipsis), a wrapping mono 10px meta line of `type · scope · age`, and a chevron
that turns `--teal-fg` on hover. Rows separate with `border-top`, not gaps.

## 3. Split section (main + 400px aside)

**Main**, two panels:
- `Corpus`, described by the repository path, action `Runs` → Index runs, footer
  `indexed <commit> · head <commit>` on the left and the staleness in
  `--warn-fg` on the right. Body: a MetricRow of files / chunks / symbols /
  vectors with deltas (tone `ok` for gains, `bad` for the vector shortfall);
  then two labelled `IndexBar`s — vector coverage (`vectorised` accent +
  `missing` warn) and freshness vs master (`indexed` accent + `changed since`
  warn + `excluded` idle/muted); then a `Disclosure` "Memory & runtime counters"
  holding a DefinitionList of decisions / rules / packs / diff reviews.
- `Agent runs`, flush, action `All` → Orchestration. Rows: task title on one
  line, then mono 10px `agent · model · duration · tokens · pack #N`, with a
  StatusChip on the right (`completed`→ok, `failed`→bad, else warn).

**Aside**: `Event flow`, flush, action `Log` → Logs. Rows: mono timestamp, a 6px
tone square, then title and detail, both single-line with ellipsis.

## Non-negotiables for the port

- **Real data only.** Every number, commit, count and timestamp comes from the
  database or the health endpoints. The reference's figures (812, 46, 9d,
  e8fc965) are fixtures — never copy them.
- **If a fact is unavailable, drop the element**, do not substitute a placeholder.
  A missing metric tile is honest; an invented one is not.
- **Do not round a degraded value to a clean one.** The current Overview prints
  `100.0%` while embeddings are missing; that specific defect is in scope.
- The narrative sentence and the causal chain must be *derived* from the live
  worst condition, not hardcoded. If no condition is bad, the screen states the
  healthy conclusion and the primary action becomes the most useful next step.
