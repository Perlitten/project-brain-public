# V3 app shell — verified parity gaps

Read from the design system's `ui_kits/cockpit/app-shell.js` (project
`ab40a0e5-c3a4-40af-9f9d-0d0ddba77a7d`). Everything below is quoted behaviour
from that file, not inference.

## Already correct in production

- Breakpoints: labelled rail ≥1040, node rail 760–1040, rail inside a Drawer
  below 760. Production matches.
- `Ctrl`/`⌘` + `K` opens the palette, and the visible hint matches the platform.
- ServiceBar at desktop, the collapsed health control below 1040.

## Gap 1 — `g`-prefixed jump keys (absent in production)

The shell arms on `g` and consumes the next key within 1200 ms. It ignores the
sequence while focus is in an `input`, `textarea`, `select` or a
`contenteditable`, and ignores it when Meta/Ctrl/Alt is held.

| keys | route |
|---|---|
| `g o` | overview |
| `g i` | insights |
| `g l` | logs |
| `g d` | graph (Dependencies) |
| `g x` | indexing |
| `g r` | reports |
| `g c` | context-packs |
| `g m` | memory |
| `g a` | orchestration |
| `g t` | mcp |

The route ids are identical to production's, so this is purely a missing
interaction. Implement in `apps/api/static/shell.js`, matching the existing
progressive-enhancement style (the page must stay usable with the file blocked).

## Gap 2 — the repository chip must be a switcher

Production renders a non-interactive teal label. The reference is a `<button>`
with `aria-expanded` opening a list of every repository, each row showing path,
branch, and commits behind. The trigger shows a tone dot (`--warn-fg` when
behind, `--ok-fg` when current), the active path (ellipsised, `max-width:240px`),
`−N` in `--warn-fg` when behind, and a chevron. The open list is
`--surface-raise` with `--shadow-popover`; the active row uses `--select-bg`
with `--teal-fg`. A full-screen transparent click-catcher closes it.

`brain/memory/repo_freshness.py::assess_repository_freshness` already supplies
behind-count and branch per repository — this needs no new data source, only a
route parameter to make the selection stick.

## Gap 3 — health control wording and detail

The reference label is `N down`, else `N degraded`, else **`all ok`** —
production says only `ok`. The control is tone-coloured on its border AND its
background (`--bad-soft` / `--warn-soft` / `--surface-row`), and its
`aria-label` reads `Service health: <label>`.

Opening it shows a **popover**, not an inline expansion: one row per service
with a `Signal`, the service name in a fixed 46px column, and a `detail` string
in `--text-3`, ellipsised. Production has no per-service detail string at all;
`/dashboard/health.json` already returns one per service.

## Gap 4 — the palette searches an index, not a route list

The shell passes `window.cockpitSearchIndex` to `CommandPalette`, not the nav.
Production's palette is a static list of ten routes. This is the audit's one
correctly-identified interaction defect. (Its two other "confirmed defects" —
that typing `index` does not filter, and that two options are simultaneously
selected — do not reproduce: filtering reduces ten options to one and exactly
one option carries `aria-selected="true"`.)

Making this real needs a backend search endpoint over files, symbols, decisions,
rules and context packs. `brain/search/code_search.py` already ranks files and
symbols; decisions and rules have their own stores.
