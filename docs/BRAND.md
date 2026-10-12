# Brand: Project Brain

The source of truth for every UI in the project: the web front end `apps/web`
(Next.js, Vercel). (The legacy server-rendered dashboard under
`apps/api/templates` has been removed.) An agent reads this file in full before
generating any screen, component or media. Any deviation from it is flagged in
the PR and explained.

Token values come from the V2 "Ledger" design system, implemented as CSS custom
properties in `apps/web/app/globals.css` (originally synced with
claude.ai/design, where the contrast measurements and the rationale for every
step live). This file does not repeat the rationale; it records the rules.

## Character

**An instrument panel anyone can understand — and that makes you gasp.**
Clear, precise, alive, honest, dark.

### Clarity first

- Every screen answers three questions in plain words within 3 seconds:
  **what is this**, **is everything OK**, **what should I do**. If someone
  seeing the product for the first time cannot answer, the screen is not ready.
- Human sentence first, term second. "Brain remembers 96% of your code" large,
  and `18,491 / 18,903 chunks vectorised` small, monospace, on a second line.
- Every specialist term (chunk, embedding, context pack, reranker, MCP) gets a
  dotted underline and explains itself in one sentence on hover or tap.
- Navigation uses the user's words ("Overview", "Activity", "Code map"), not
  the names of internal subsystems.
- Every number has a caption saying what it means, and a tone: good or bad.

### Wow effect — in measured doses

- The main screen has one **signature scene**: the "memory map". All of the
  repository's code is shown as a mosaic of cells; the cells appear in a
  diagonal wave on load (once), highlight a module on hover, and while indexing
  is running a scan line slowly sweeps across the map.
- The wow comes from precision and moving data, not decoration: no gradients,
  particles, glows or 3D for the sake of looks.
- **Amendment 2026-10-02**:
  **light** is allowed, but only as feedback or as data. That means a dotted
  blueprint background lit by a lens around the cursor; a panel edge that
  catches the light near the cursor; the verdict's tonal light and its "pulse";
  particles on code-map links (their count matches the number of calls);
  "neural" flashes on the memory map over the cells Brain remembers; and a
  splash screen on the first visit of a session. Decoration without data is
  still forbidden.

- A screen first states a **one-sentence verdict** ("Index is 3 commits
  behind") and then shows the evidence. Numbers are evidence, not ornament.
- Not corporate SaaS, not "AI magic", not a game. No gushing copy, no
  "Unlock the power of…".
- Honesty beats beauty: a "no data" state is shown as "no data", and demo data
  is always labelled as demo.

## Colours

Dark theme only. This is a deliberate decision: the tool lives next to the
terminal and the IDE. There is no light theme and there will not be one
without a separate decision recorded in this file.

| Role | Token | Hex | Rule |
|---|---|---|---|
| Page | `--surface-canvas` | `#05080B` | the lowest layer |
| Chrome (rail, masthead) | `--surface-rail` | `#0E141A` | one bar, one value |
| Module / panel | `--surface-module` | `#151D27` | the main surface |
| Inset / row hover | `--surface-inset` | `#1F2936` | one step above the module |
| Raised | `--surface-raise` | `#283541` | popovers, active controls |
| Border | `--border-default` | `#3A4A5C` | surface outline |
| Divider | `--border-subtle` | `#2C3949` | rows inside a surface |
| Text 1 / 2 / 3 | `--text-1/2/3` | `#E6EDF3` / `#9FB0BD` / `#93A1AE` | all three pass AA on every surface |
| Accent | `--teal-fg` | `#2FE3C8` | **interaction only**: focus, selection, primary action, active navigation, links |
| ok / warn / bad / info | `--ok-fg` etc. | `#3DDC91` / `#F2B33D` / `#FF7A8A` / `#5CB8FF` | **status only**, never interaction; each has a `-soft` (fill) and a `-line` (outline) variant |

Depth = one value step + one line. No glass, no blur, no alpha layering.

**Forbidden:** gradients (any, including purple-blue), neon glow, teal as
decoration or status, status colours on buttons, arbitrary hex values outside
the tokens. The only glow is `--live-glow` on the live pulse (heartbeat).
*Amendment 2026-10-02:* low-density radial "light spots" (≤10% of the tone) are
allowed: the top background light, the cursor lens, the verdict's tonal light,
the spotlight edge on panels. A glow on the data itself is also allowed:
memory-map flashes, the pulse trail, a broken link. Purple-blue gradients,
glass and blur remain forbidden.

## Typography

- Body text and headings: **Outfit** 400/500/600 (self-hosted woff2).
- Numbers, paths, hashes, codes, kbd: **JetBrains Mono** 400/500, always
  `tabular-nums`.
- A seven-step scale with no in-between values:
  `11` caption · `12` label/chip/column header · `14` text and cells ·
  `16` panel heading · `20` verdict · `28` page H1 · `40` one hero number per
  screen · `72` display — only the memory map's main number.
- Line height is a role: `1.6` prose, `1.35` interface, `1.25` headings.
- Upper case only in eyebrow labels, tracking `0.06em`. Paragraphs are at most
  `68ch` wide.

## Grid and shape

- 4px step: `4 8 12 16 20 24 32 40 48 64 80`.
- Radii: `4` for small elements (chip, control), `8` for surfaces, `999` for
  pills. No other radii.
- Rail 200px wide (64px collapsed), chrome bar height 52px, page gutter 24px
  (16px on mobile).
- Density: comfortable by default (44px rows), compact for logs (32px rows).
- Shadows only on things that float: popover, palette, drawer, modal.

## Settings forms

- Group by the person's task: notifications, connection, working parameters.
- The main control, its label and one short explanatory line read together.
  Repeated `?` icons and badges showing where each value comes from are not
  placed in line with the controls.
- Internal keys and value sources live in an expandable "Server configuration"
  section. If the server may restore an old value after a restart, the warning
  appears next to the unsaved changes.
- Intervals are shown with units ("6 hours"), while still allowing a custom
  value. The connection of an already configured bot is collapsed; when
  credentials are missing, the fields are open straight away.
- Within one block every change is saved explicitly. Save/Discard appear after
  an edit; a permanent "Saved" button does not pretend to be an action.
- Expanding details and showing the save row: 120ms, opacity/transform, no size
  animation. With reduced motion the state changes instantly.

## Signature decisions

What sets this product apart from a template. Do not dilute.

0. **Memory map.** A mosaic of cells, one cell per chunk of code. A cell's tone
   is its state (remembered / stale / not read / excluded). The product's main
   scene and its richest motion (see "Choreography").
1. **Verdict line.** Every screen opens with a 20px status sentence with a tone
   on the left (spine). It is not a hero; it is the heading of a log entry.
2. **Segmented meter.** Coverage and freshness are drawn as 20 discrete cells,
   not a smooth progress bar: you can see how many "bricks" are missing. The
   legend sits under the meter and reads as words.
3. **Spine.** A card that carries weight (ok/bad) gets a 3px vertical bar on
   the left. No background fill and no smiley icon.
4. **Log as a ledger.** Events are rows with a monospace timestamp, hairline
   dividers and a tone shown as a dot. No bubbles, no avatars.
5. **Mark.** "A brain made of lines of code" (since 2026-10-02 it replaces the
   earlier "column, rows, arrow" mark): seven rows of "tokens" form the
   silhouette of a brain with a gap between the hemispheres in the middle.
   Teal tokens are signals. On the splash screen the tokens grow outwards from
   the gap and the signals run in turn; hovering over the brand makes the brain
   "think". Source: `BrandMark` in `apps/web/components/Icon.tsx`, favicon
   `apps/web/app/icon.svg`.
6. **Verdict pulse.** A cardiogram in the verdict's tone along the bottom edge.
   The rhythm is the state: calm (ok), faster (warn), rapid (bad), flat line
   (idle).

## Motion

Motion is part of the wow effect, but every movement communicates something:
data appearing, code being read, a transition, focus. Beautiful motion without
meaning is not done. (Expanded 2026-10-02 for much bolder animation —
the earlier minimalism was replaced by the system below.)

- Durations: `120ms` control · `170ms` route change · `180ms` surface ·
  `220ms` appearance · `420–700ms` data choreography · `≤1200ms` the overall
  "launch" of the main screen.
- Easing: `cubic-bezier(.2,.6,.35,1)`; on appearance
  `cubic-bezier(.16,.84,.44,1)`; for indicators settling into place
  `cubic-bezier(.22,1,.36,1)` (expo-out). No overshoot.
- Animate transform, opacity, clip-path, stroke-dashoffset and cell fill
  colour. Never animate width/height/top/left.

Choreography (signature, do not dilute):

1. **Screen launch.** Page sections enter in a cascade from below (8px, 60ms
   step). The verdict heading rises word by word from under a mask; the spine
   draws from top to bottom.
2. **Memory map.** Cells light up in a diagonal wave (≤900ms): each flashes a
   light "read" and settles into its state colour. Hovering a module sends a
   ripple from the cursor through that module's cells while the rest dim.
   While indexing is running, a scan line moves across the map and the cells
   under it light up.
3. **Odometer numbers.** Key numbers roll to their value like a drum (only on
   first display and on focus change), rather than ticking like a counter.
4. **Meters** fill brick by brick from left to right.
5. **Navigation.** The active-item indicator slides to the new item; the ⌘K
   palette expands from the point where it was invoked, and its rows enter in
   a cascade.
6. **Code map.** Links draw from source to target; a rule violation flows as a
   dashed line in the bad tone.
7. **Scrolling.** Panels below the first screen fade in as they enter the
   viewport (scroll-driven, no JS).
8. **Splash screen** (once per session, ≤1.5s, any click or key skips it). The
   mark assembles, 3–4 lines of real facts are typed out (number of
   repositories, lag), the meter fills, then the screen leaves with a curtain
   moving up.
9. **Canvas memory map.** On top of the cells' appearance wave, ring flashes
   spread over the "remembered" cells (Brain is recalling). Stale cells slowly
   "breathe" in the warn tone; cells near the cursor are magnified by a lens.
10. **Decoding.** Short eyebrow captions resolve once from random characters on
    first display.

- **Never:** bounce, parallax, rotation for its own sake, particles, glows
  (other than `--live-glow`), ticking counters, endless decorative loops. The
  only endless animations are: the heartbeat pulse, the scan line (indexing in
  progress) and the flow along a broken link on the code map. *Since
  2026-10-02 these are added:* memory-map flashes, the verdict pulse and
  particles on code-map links. All of them carry data and stop when off
  screen.
- `prefers-reduced-motion` removes all motion, leaving instant states and
  colour changes.

## Icons and illustrations

- Icons: a custom 24×24 sprite, stroke 1.75, `round` caps/joins, no fill
  (layers, database, graph, file, zap, search, chip, code, automation, vectors,
  key, flag). A new icon is drawn with the same geometry; emoji in place of
  icons are forbidden.
- One icon per route; icons are not repeated in the navigation.
- Illustrations and generated images are not used in the product. If they are
  ever needed (OG image, promo): a dark `#05080B` scene, cool neutral light,
  the only accent is teal `#2FE3C8`, geometry made of rectangles as in the
  mark. **No text in the image.** At least 40% of the frame is an empty dark
  area for a heading overlaid as vector.

## Voice

- English interface: short verb labels ("Reindex", "Open pack").
- Write as if explaining to a colleague without context: "Brain's copy of your
  code is 3 changes behind", not "Index trails master by 3 commits".
- Numbers with units: `3 commits behind`, `4h since index`.
- An empty state says what will be here and how to get it. No "Oops".

## References

- **Linear** (linear.app): we take its navigation density, keyboard as a
  first-class citizen, and the ⌘K palette.
- **Vercel dashboard**: we take its strict monospace typography for
  identifiers and deployment statuses.
- **Grafana / Honeycomb**: we take their honest "no data" representation and
  the event log as the main investigation screen.
- **Bloomberg Terminal**: we take the idea that a number always sits next to
  its interpretation, and its tabular density.

Added 2026-10-08 from design references:

- **[21st — Settings pages](https://news.21st.dev/blog/react-settings-page-components)**:
  rows with controls and explanations, a single save mode within a section,
  explicit Save/Discard states. Applied with our Ledger tokens, without
  borrowing the catalogue's palette or decorative effects.
- **[Amicro](https://amicro.vercel.app/)** and
  **[React Bits](https://www.reactbits.dev/get-started/index)**: references for
  short feedback on actions. Durations and reduced motion follow the "Motion"
  section; motion in Settings serves state changes.
- **[OpenDesign](https://github.com/nexu-io/open-design)**: a possible separate
  space for visual review with agents and a DESIGN.md contract. Its integration
  with **[HyperFrames](https://github.com/heygen-com/hyperframes)** suits future
  product videos and demos.

## Anti-references

- The landing-page template "gradient hero + three cards with icons + CTA".
- Glassmorphism, neon cyberpunk dashboards, "AI glow".
- Kanban boards and pastel SaaS panels with large avatars.
- Pie charts and speedometers for quantities that have a numerator and a
  denominator: the segmented meter exists for those.
