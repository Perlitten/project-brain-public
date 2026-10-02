# Project Brain Cockpit Operator UX Roadmap

**Date:** 2026-08-02  
**Status:** accepted implementation plan  
**Baseline:** production UX/UI audit score 42/100 (operator workflow NO-GO)

## Objective

Turn the Cockpit from route-specific data dumps into a consistent operator
product. The target is one shared query, table, pagination, detail, state, and
responsive contract across every long-list surface while preserving repository
scoping and the existing Project Brain visual language.

This is not a visual repaint. Retain the current typography, palette, rail,
Command screen, LFM verdict, MCP disclosures, and mobile navigation dialog.

## Inputs

- Production audit:
  <local-workspace>/brain-ux-audit-2026-08-02/AUDIT.md
- Current canonical source: master
- [Carbon data table usage](https://carbondesignsystem.com/components/data-table/usage/)
- [Carbon pagination usage](https://carbondesignsystem.com/components/pagination/usage/)
- [WAI-ARIA tabs pattern](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/)
- [WCAG 2.2](https://www.w3.org/TR/WCAG22/)
- [GOV.UK unexpected service-error pattern](https://design-system.service.gov.uk/patterns/problem-with-the-service-pages/)

## Research corrections to the initial audit plan

### 1. Bounded lists exist, but the contract is not shared

Commit 6e47ad2 already bounds Insights, Event log, and Index runs. The current
Insights query correctly applies SQL offset, shares one database session for
count and page fetch, and computes global counts independently from page rows.

The local branch wip/bounded-lists-2026-08-01 is stale and must not be merged.
Its earlier Insights implementation returned from list_recent_insights before
the pagination path and could return page one for page two. The corrected
implementation is already on master.

The remaining problem is architectural: each route still owns a different
pagination, filtering, count, and URL contract.

### 2. Event-log pagination still reads the complete file

read_recent_logs calls readlines, parses every record, builds an in-memory list,
and slices it after counting. It bounds rendered HTML but does not bound server
memory or latency. The shared contract needs reverse streaming or an indexed log
source before richer filters are added.

### 3. Current sorting is deliberately page-local

shell.js sorts only rows already delivered by the server and announces this
limit. Adding a shared sort control without server-side sort would make the
interface look global while changing only one page. Global sort is a backend
query concern, not a JavaScript enhancement.

### 4. Sticky headers exist but lack the required scroll owner

dashboard-utilities.css already gives table headers position: sticky and top: 0.
The production screenshots still lose the headers because the document scrolls,
not a bounded table container. The component must own a bounded scroller and a
tokenized sticky stack; another isolated sticky rule will not solve the problem.

### 5. Responsive table behavior is split across legacy and component CSS

Legacy dashboard.css turns rows into stacked cards at narrow widths while
components-data.css repairs side effects such as hidden rows becoming visible.
The shared component must own desktop and mobile rendering as one state model.
Remove the legacy global row transformation after all target routes migrate.

### 6. FastAPI cannot catch an upstream 502

The nginx template has a branded 401 path but no interception for 502, 503, or
504. When the API process is unavailable, Jinja and FastAPI cannot render a
recovery UI. Production needs an nginx-served static Cockpit error document with
Retry, timestamp or request identifier, safe navigation, and no infrastructure
jargon.

### 7. There is no reusable Jinja component boundary

The templates have navigation and one small indexing empty-state macro, but no
shared table, pagination, tabs, or state macros. Shared Jinja macros and typed
Python payloads are prerequisites for route migration.

## Target architecture

### Typed server query contract

Introduce a normalized query object for long-list routes:

    OperatorQuery
      repo
      q
      filters
      sort
      direction
      page
      page_size
      selected
      view

    OperatorPage[T]
      items
      total
      page
      page_size
      total_pages
      range_start
      range_end
      facets
      query

Rules:

- Parse, clamp, and validate query state in one helper.
- Preserve repository scope in every link, form, fetch, sort, page, detail, and
  Retry destination.
- Fail closed for unknown repositories.
- Apply search, filter, and sort before count and pagination.
- Use stable secondary ordering so adjacent pages cannot duplicate or skip rows.
- Return or redirect to canonical state when a page is out of range.
- Keep user-visible state in the URL so reload, back, forward, and shared links
  are deterministic.

### Shared Jinja component set

Add slot-based macros rather than a universal table renderer:

- operator_table
- operator_toolbar
- operator_pagination
- operator_tabs
- operator_state
- operator_detail_drawer

The route owns columns and record summaries. The shared layer owns semantics,
sticky behavior, query propagation, density, pagination, live-result status,
mobile collapse, and focus restoration.

Keep native table semantics on desktop. Do not introduce role=grid unless
cell-level keyboard navigation is required. Row navigation and actions remain
explicit links or buttons.

### Progressive enhancement

The no-JavaScript path supports filtering, sorting, paging, tab navigation, and
opening a record. JavaScript adds:

- debounced query submission;
- URL and history synchronization;
- column and density preferences;
- detail drawer behavior;
- focus movement after page or filter changes;
- live status announcements.

Client code must not re-implement a different filtering or sorting model over
only the current page.

### Token additions

Use the existing spacing and hit-target scales. Add only missing semantic tokens:

    --route-tabs-h
    --operator-toolbar-h
    --operator-pagination-h
    --operator-row-compact
    --operator-row-standard
    --operator-row-comfortable
    --operator-table-max-h
    --layer-route-tabs
    --layer-operator-toolbar
    --layer-table-head
    --mobile-action-bar-h

The sticky stack is masthead, route tabs, operator toolbar, then table header.
Every sticky layer must expose matching scroll-padding-top and scroll-margin-top
behavior so keyboard focus is not hidden.

## Delivery waves

### Wave 0A: production error resilience

1. Add a static Cockpit error page that does not depend on the API container.
2. Configure nginx interception for dashboard 502, 503, and 504 responses.
3. Preserve shell identity, provide Retry and Command or health destinations,
   and show a request or timestamp reference without upstream internals.
4. Add a deploy smoke that targets an unavailable upstream and proves raw nginx
   HTML is never visible.

This ships first because it protects every later rollout.

### Wave 0B: shared server contract

1. Add OperatorQuery and OperatorPage.
2. Add canonical query serialization and repository propagation.
3. Test filtering, global sorting, page bounds, stable ordering, selected-record
   preservation, and back or forward-safe URLs.
4. Replace Event log full-file materialization with reverse streaming or an
   indexed store that answers count, facet, and page queries within a fixed
   memory budget.

### Wave 0C: shared component foundation

1. Add Jinja macros, component CSS, and a progressive-enhancement controller.
2. Implement bounded scrolling, sticky stack, density, columns, page size,
   result range, pagination, and a detail drawer.
3. Implement loading, empty, partial, degraded, transient-error, and no-results
   states as one component family.
4. Add DOM and keyboard tests before migrating routes.

### Wave 1: highest-cost routes

Migrate in this order:

1. Context packs
2. Event log
3. Index runs
4. Decisions and rules

Per-route requirements:

- **Context packs:** server search, filter, and sort; task clamped to two lines;
  detail drawer; Library and Compose as URL-backed tabs; 25-row default.
- **Event log:** time, level, service, component, repository, job, and
  correlation filters; compact mobile summary; exact related-job or log links.
- **Index runs:** repository, date, commit, trigger, status, and duration query;
  run detail drawer; coverage and remediation outside the Runs dataset.
- **Decisions and rules:** Rules and Decisions tabs; compact empty state;
  branded transient failure; repository, status, date, and tag query.

### Wave 2: information architecture

1. Dependencies: Overview, Explore graph, Hotspots, Orphans, Schema.
2. LFM: Overview, Evaluation, Runtime, Identity and approval.
3. Agent runs: Overview, Jobs, Workflows, Actions.
4. Reports: searchable report metadata plus preview drawer.

Use tabs only for peer views. Implement tablist, tab, tabpanel, aria-selected,
aria-controls, Home or End, and arrow-key navigation. Prefer links with
URL-backed views when content is server-rendered or expensive. Add ARIA tab
behavior only when panels are preloaded and activation is immediate.

### Wave 3: mobile and accessibility hardening

1. Reduce the mobile masthead to Menu, H1, and overflow.
2. Move repository, global search, service health, and logout into a secondary
   sheet without removing accessible names.
3. Replace generic field-by-field table cards with route-specific compact
   summaries and one detail action.
4. Verify keyboard-only use, screen-reader output, contrast, text spacing,
   200-400% zoom, 320 CSS-pixel reflow, reduced motion, target size, and focus
   visibility or obscuration.

## Route migration matrix

| Route | Current backend | First change | Detail model |
|---|---|---|---|
| Context packs | bounded rows, fixed page size | shared query and clamp | drawer |
| Event log | paged after full-file read | bounded data source | drawer |
| Index runs | SQL paging, fixed page size | shared filters and sort | drawer |
| Decisions/rules | route-specific paging | split views and error state | drawer |
| Reports | full directory listing | metadata adapter and paging | preview drawer |
| Insights | correct SQL offset paging | adopt shared chrome last | existing split |
| Dependencies | graph plus stacked summaries | tabs, graph first | inspector |
| LFM | dossier | tabs, keep NO-GO summary | disclosures |
| Agent runs | mixed dashboard | split views | job drawer |
| MCP tools | disclosures work | search and capability filters | accordion |

## Acceptance gates

### Data correctness

- Search, filter, sort, count, and pagination operate over the same full dataset.
- Repository scope survives every interaction.
- No page-local control is presented as a global operation.
- No long-list request reads an unbounded dataset into memory.

### Operator efficiency

- Default page size is 25; choices are 10, 25, 50, and 100 where payload size
  permits. Heavy records may cap at 50 and explain that limit.
- Every list shows Showing X-Y of Z, current page, total pages, and attached
  Previous or Next controls.
- No unopened route exceeds three viewport heights because of a record list.
- Verbose cells are at most two lines; complete content is one action away.
- Filter, sort, page, density, columns, selected record, view, and repository
  survive reload and browser navigation.

### Accessibility

- Targets are at least 24 by 24 CSS pixels or meet a documented spacing
  exception; primary touch controls target 40-44 pixels.
- Tabs implement the WAI-ARIA keyboard and state contract.
- Focus is visible and not hidden by the sticky stack.
- Mobile reflow works at 320 CSS pixels without loss of information or function.
- Mobile transformations have one reading model and no duplicate header
  announcements.

### Resilience and release

- No dashboard request can expose a raw nginx, framework, or traceback page.
- Every failed or degraded state states cause, impact, last known time, and next
  action.
- Automated route captures and DOM assertions cover 390, 768, 1150, and 1440
  pixel widths.
- Each route migration ships independently and can roll back without reverting
  the shared query or component foundation.

## Explicit non-goals

- No React migration.
- No new palette, typography, icon language, or navigation model.
- No merge from wip/bounded-lists-2026-08-01.
- No client-only global sorting or filtering.
- No big-bang rewrite of every dashboard route in one release.

## Immediate sequence after the token release

1. Wave 0A nginx error resilience.
2. Wave 0B typed query and page contract plus bounded Event log source.
3. Wave 0C shared macros, controller, and CSS with a fixture route.
4. Context packs migration and measured page-height reduction.
5. Event log migration and mobile compact-record proof.

