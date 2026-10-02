# v3 port — navigation components (markup contract)

`apps/api/static/components-nav.css`, loaded after `tokens/*.css` and `components.css`. Ported from the design system's `components/navigation/SplitList.jsx` / `Stepper.jsx` / `Disclosure.jsx`. The `.jsx` holds open/selected state in React; here the server already knows it, so state arrives as a class or a native attribute. No JavaScript is required. Every `hx-*` below is illustrative; the CSS needs none of them.

## SplitList

Two columns above 900px of its **own** width (a container query, not a viewport one), a one-pane-at-a-time flow below it. The extra `__panes` wrapper exists because an element cannot query the container it defines — `.pb-splitlist` is the container, `__panes` is the grid. The template's only job is the `--selected` modifier: below the breakpoint it picks which pane is on screen. List width is `--pb-list-w` (default `380px`).

```jinja
<div class="pb-splitlist{{ ' pb-splitlist--selected' if selected }}" style="--pb-list-w:380px">
 <div class="pb-splitlist__panes">
  <div class="pb-splitlist__list">
   <div class="pb-toolbar">{# optional toolbar #}</div>
   <section class="pb-splitlist__listbody" aria-label="{{ list_label }}">
    {% for r in rows %}<a class="pb-panel pb-panel--interactive" hx-get="/insights/{{ r.id }}" hx-target="#pane">{{ r.title }}</a>{% endfor %}
   </section>
  </div>
  <div class="pb-splitlist__detail" id="pane">
   {# Hidden by CSS on desktop; shown only in the narrow flow. #}
   <a class="pb-splitlist__back" href="{{ list_url }}" hx-get="{{ list_url }}" hx-target="#pane">{{ back_label|default('Back to list') }}</a>
   {% if selected %}{{ detail }}{% else %}<div class="pb-empty">Select an insight</div>{% endif %}
  </div>
 </div>
</div>
```

## Stepper

Four steps or fewer. Completed steps stay clickable and get a filled teal check; the current step is a teal wash carrying `aria-current="step"`; future steps are inert `<div>`s. The connector is CSS-only — the template never decides which one to fill or which to hide on the last item.

```jinja
<ol class="pb-stepper">
 {% for s in steps %}
 <li class="pb-stepper__item">
  {% if loop.index0 <= current %}
  <button type="button" hx-get="/flow/step/{{ loop.index0 }}" hx-target="#flow"
    class="pb-stepper__step{{ ' pb-stepper__step--done' if loop.index0 < current }}"
    {% if loop.index0 == current %}aria-current="step"{% endif %}>
  {% else %}
  <div class="pb-stepper__step">
  {% endif %}
    <span class="pb-stepper__marker" aria-hidden="true"><span class="pb-stepper__n">{{ loop.index }}</span></span>
    <span class="pb-stepper__label">{{ s }}</span>
  {% if loop.index0 <= current %}</button>{% else %}</div>{% endif %}
  <span class="pb-stepper__link" aria-hidden="true"></span>
 </li>
 {% endfor %}
</ol>
```

`pb-stepper__n` is the step number; CSS hides it and draws a check once the step is `--done`. If you cannot use `:has()`, add `pb-stepper__link--done` to the connector of every completed step to fill it explicitly.

## Disclosure

Native `<details>`/`<summary>`, so the toggle, the keyboard support and the open state are the browser's. Default closed — omit `open`. The chevron and its quarter-turn are pseudo-elements; the default disclosure triangle is suppressed for both Firefox and WebKit.

```jinja
<details class="pb-disclosure"{% if expanded %} open{% endif %}>
 <summary class="pb-disclosure__summary">
  <span class="pb-disclosure__text">{{ summary }}</span>
  {% if meta %}<span class="pb-disclosure__meta">{{ meta }}</span>{% endif %}
 </summary>
 <div class="pb-disclosure__body">
  <pre>{{ schema }}</pre>{# any body content; the DS example is a dense code block #}
 </div>
</details>
```

## Notes

- **Breakpoint conflict.** `SplitList.prompt.md` says 900px, `SplitList.jsx` defaults `breakpoint = 700`. The prompt.md figure is the documented spec and is what shipped. A container query condition cannot read a custom property, so 900px is fixed in CSS while `--pb-list-w` stays per-instance.
- **Not expressible in CSS.** `SplitList.jsx` moves focus to the Back control when the narrow detail view opens. Closest server-rendered equivalent: put `autofocus` on `.pb-splitlist__back` for a full page load, or `hx-on::after-settle="this.focus()"` for an htmx swap. Nothing in the stylesheet depends on it.
- **Focus.** `v2-overrides.css` rule 5 rings `a, button, input, select, textarea, [tabindex]`. `<summary>` and the Stepper's inert `<div>` steps are in none of those lists, so `components-nav.css` rings them explicitly on the same `--focus-*` tokens.
- **Legacy hazard — read this before composing any screen.** `dashboard.css` styles these bare element selectors: `a`, `aside`, `body`, `code`, `header`, `html`, `main`, `nav`, `pre`, `table`, `td`, `th`. Any V3 markup built on one of them silently inherits app-shell layout. Three shipped defects came from exactly this and nothing else: the masthead breadcrumb stacked one word per line (`nav{flex-direction:column}`), the seven-column run table rendered as seven rows per run (`td` restyled as a label/value grid below a breakpoint), and the sign-in card sat right of centre and pinned to the top (`main` carrying a 72px nav-rail margin, page padding and a full-height flex stretch). Rules of thumb: every `display:flex` must name its `flex-direction`; any component whose root is one of those elements must restate its own margin, padding, display and sizing.
