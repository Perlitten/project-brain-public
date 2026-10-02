# Component contracts

The cockpit's component vocabulary, written as contracts: what each component
promises, which classes/attributes are the API surface, and which states it
must survive. `/dashboard/catalog` renders every contract below using the
production classes — if a state cannot be produced there, the contract is
wrong, not the page.

Template macros for the shared components live in
`apps/api/templates/macros.html` (`{% import "macros.html" as ui %}`).

## Conventions

- **Semantic tokens are authoritative.** Components consume `--text-*`,
  `--surface-*`, `--border-*`, `--ok-line`, `--action-*`, `--s-*` spacing, and
  `--hit-*` targets. Raw lengths/colors in templates are a
  `design_system_guard` violation; the allowed fallbacks list is in the guard
  script.
- **States are attributes, not classes**, wherever the platform gives us one:
  `disabled`, `aria-busy`, `aria-current`, `aria-pressed`, `aria-invalid`,
  `[hidden]`. CSS draws state from the same attribute assistive technology
  reads, so the picture and the announcement cannot disagree.
- **Progressive enhancement.** Every control works as a link or a form post
  without JS; the shell modules only add shortcuts, popovers and live updates.

## Buttons — `pb-btn`

| Modifier | Use |
|---|---|
| `pb-btn--primary` | The one action the screen exists to offer |
| `pb-btn--danger` | Destructive/irreversible |
| `pb-btn--ghost` | Inline close/dismiss, icon buttons |
| `pb-btn--sm` | Secondary rows, table actions |

States: `:disabled` (also sets `aria-disabled` on action buttons that JS may
toggle), `aria-busy="true"` for pending (components.css draws the spinner;
`prefers-reduced-motion` slows it), link variant via `<a class="pb-btn">`.
Contract: a button that does nothing must not render — render a `disabled`
button with a reason in the hint, or omit it.

## Fields — `pb-field`

Structure: `pb-field` → `pb-field__label` → `pb-field__control` →
`pb-field__input` (+ optional `pb-field__hint`). Widths via
`pb-field--grow-{160,200,240}` modifiers, sizes via `pb-field--sm|lg`.
Error state: `pb-field--error` on the wrapper + `aria-invalid="true"` on the
input; the hint explains the fix, not just the failure. Secrets never enter a
field's `value` — config surfaces write only non-secret keys
(`brain/onboarding/envfile.py` enforces the allowlist).

## Status — `pb-chip`

Tones `pb-chip--ok | warn | bad | info`; `pb-chip--sm`; optional
`pb-chip__dot`. Job lifecycle → tone is **one** mapping:
`ui.job_chip(status)` / `ui.job_tone(status)` in macros.html — completed=ok;
failed/error=bad; queued/skipped/degraded/cancelled=warn;
running/retrying=info. Do not re-derive per page.

## Panels — `pb-panel`

`pb-panel` → `pb-panel__head` (`pb-panel__title` + `pb-panel__desc` +
`pb-panel__actions`) → `pb-panel__body`. Severity-bearing cards add
`pb-panel--spine` + `pb-panel--ok|bad` (and a presentational
`pb-panel__spine` element). `pb-panel--inset` for nested panels.

## Data lists / tables — `pb-table`

`<table class="pb-table">` with `<th scope="col">`. Opt-in behaviours:
`data-pb-sort` (client-side re-order of the delivered rows — announced and
capped to what the page holds, never pretending to sort the whole store),
`data-pb-sort-skip` on action columns, `data-pb-copy` on liftable values.
Empty tables render `ui.empty_state`, not zero rows.

## Empty / error / loading

- Empty: `ui.empty_state(title, body, action_href, action_label)` — the body
  says what would be here and the action starts the sequence that produces
  it; omit the action when nothing on this screen can.
- Error/inline consequence: `pb-notice--{ok,warn,bad,info}`.
- Loading: `pb-skeleton` placeholders (shimmer respects
  `prefers-reduced-motion`) and `aria-busy` on the loading region.

## Dialogs — `pb-dialog`

Markup contract (used by the nav drawer and Find in Brain):
`pb-dialog` (`role=dialog`, `aria-modal`, `aria-labelledby`, `hidden`) →
`pb-dialog__scrim` (`data-dialog-scrim`) → `pb-dialog__panel` → `__head`
(title + `data-dialog-close` button, `data-dialog-initial` marks initial
focus) → `__body`. `shell-core.js`'s `createDialog` owns open/close, focus
trap, Esc, and focus return to the opener. Do not hand-roll `<dialog>` or
another overlay primitive.

## Navigation

`nav_groups` in `base.html` is the single source for rail, drawer, palette and
jump keys. A new destination is one tuple; labels exist once. Jump keys read
`data-route` off the anchors — there is no second route table.

## Task progress

Two honest forms: `pb-stepper` for staged flows (current step
`aria-current="step"`; steps are links only when a route exists — otherwise
`<div>`s), and the job lifecycle badge (`ui.job_chip`) + stepper card on Agent
runs, which shows `queued → running → done` and reports terminal failures by
badge, never by pretending the job finished. Queued jobs offer `Cancel`;
terminal failures offer `Retry`; both go through `data-dashboard-action`.

## JavaScript lifecycle rules

- The shell is split per feature: `shell-core` (dialog/popover primitives on
  `window.__pb`), `shell-speech` (`__pb.say` live region), `shell-palette`,
  `shell-keys`, `shell-popovers`, `shell-tables`, `shell-graph`,
  `shell-sort`, `shell-copy`. `defer` preserves document order — core and
  speech load before consumers.
- Each file is one IIFE; all listeners are bound at module scope. Pages are
  full navigations (HTMX swaps never re-run these files), so there is no
  teardown API — a module must not keep per-swap state. Anything a swapped-in
  fragment needs must live in the fragment's own `<script>` or in `hx-on`.
- Page scripts (inside `content` blocks) own their own listeners and their
  own timers: clear intervals/timeouts on teardown or guard them on element
  presence, and write row visibility through `window.pbRowVisibility` so
  filters compose as AND instead of racing `hidden`.
