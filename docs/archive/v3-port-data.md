# v3 port — data components (markup contract)

`apps/api/static/components-data.css`, loaded after `tokens/*.css` and `components.css`. Ported from the design system's `components/data/FocusGraph.jsx` / `CausalChain.jsx`. Tone suffix on both: `ok|warn|bad|info|idle` (`info` is CausalChain only). Every `hx-*` below is illustrative; the CSS needs none of them.

## FocusGraph — canvas

Jinja owns the maths. For signed depth column `d`, row `i` of `rows`: `x = 28 + (d - min_d) * 196`, `y = canvas_h/2 + (i - (rows-1)/2) * 64 - 26`. Edge path: `M{x1} {y1} C{mx} {y1} {mx} {y2} {x2} {y2}` where `x1 = from.x + 168`, `y1 = from.y + 26`, `x2 = to.x`, `y2 = to.y + 26`, `mx = (x1+x2)/2`.

```jinja
<div class="pb-focusgraph" style="--pb-graph-h:460px">
 <div class="pb-focusgraph__viewport">
  <div class="pb-focusgraph__canvas" style="--pb-canvas-w:{{ w }}px;--pb-canvas-h:{{ h }}px">
   <svg class="pb-focusgraph__edges" width="{{ w }}" height="{{ h }}" aria-hidden="true">
    <defs><marker id="pb-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path class="pb-focusgraph__arrowhead" d="M0 0 L8 4 L0 8 z"/></marker>
    <marker id="pb-arrow-hot" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path class="pb-focusgraph__arrowhead pb-focusgraph__arrowhead--hot" d="M0 0 L8 4 L0 8 z"/></marker></defs>
    {% for e in edges %}<path class="pb-focusgraph__edge pb-focusgraph__edge--{{ e.type|lower }}{{ ' pb-focusgraph__edge--hot' if e.hot }}" d="{{ e.d }}" marker-end="url(#pb-arrow{{ '-hot' if e.hot }})"/>{% endfor %}
   </svg>
   {% for n in nodes %}
   <button type="button" style="--pb-x:{{ n.x }}px;--pb-y:{{ n.y }}px" aria-pressed="{{ 'true' if n.is_selected else 'false' }}" title="{{ n.label }} — {{ n.kind }}" hx-get="/graph/node/{{ n.id }}" hx-target="#detail"
     class="pb-focusgraph__node pb-focusgraph__node--{{ n.risk }}{{ ' pb-focusgraph__node--focus' if n.is_focus }}{{ ' pb-focusgraph__node--selected' if n.is_selected }}{{ ' pb-focusgraph__node--blast' if n.in_blast }}">
    <span class="pb-focusgraph__spine" aria-hidden="true"></span><span class="pb-focusgraph__glyph" aria-hidden="true">{{ n.glyph }}</span>{# ▪ file ◆ symbol ▲ test ■ module #}
    <span class="pb-focusgraph__text"><span class="pb-focusgraph__label">{{ n.label }}</span><span class="pb-focusgraph__meta">{{ n.kind }} · {{ n.degree }} refs</span></span>
   </button>
   {% endfor %}
   {% for c in clusters %}<button type="button" class="pb-focusgraph__cluster" style="--pb-x:{{ c.x }}px;--pb-y:{{ c.y }}px" hx-get="/graph?expand={{ c.depth }}">+{{ c.count }} more</button>{% endfor %}
  </div>
 </div>
 <div class="pb-focusgraph__zoom"><button type="button" class="pb-focusgraph__zoombtn" aria-label="Zoom out">−</button><button type="button" class="pb-focusgraph__zoombtn" aria-label="Zoom in">+</button><button type="button" class="pb-focusgraph__zoombtn" aria-label="Fit">⤢</button></div>
 <div class="pb-focusgraph__status" aria-live="polite"><span>◀ depends on this · this depends on ▶</span><span class="pb-focusgraph__status-item--folded">{{ folded }} folded</span><span class="pb-focusgraph__status-item--blast">blast radius {{ blast_n }}</span></div>
</div>
<div class="pb-graphlegend">{% for t in ['imports','calls','defines','tests'] %}<span class="pb-graphlegend__item"><svg width="26" height="8" aria-hidden="true"><line class="pb-graphlegend__line pb-graphlegend__line--{{ t }}" x1="0" y1="4" x2="26" y2="4"/></svg>{{ t|upper }}</span>{% endfor %}</div>
```

## FocusGraph — explorer (phone variant; replaces the canvas, needs no maths)

```jinja
<div class="pb-focusgraph pb-focusgraph--explorer">
 <div class="pb-focusgraph__focuscard"><span class="pb-focusgraph__spine pb-focusgraph__spine--{{ focus.risk }}" aria-hidden="true"></span>
  <div class="pb-focusgraph__eyebrow">Focus</div><div class="pb-focusgraph__focuslabel">{{ focus.label }}</div></div>
 {% for g in [outgoing, incoming] %}{# g.label = "Depends on" / "Depended on by"; g.dir = outgoing / incoming #}
 <div class="pb-focusgraph__group">
  <div class="pb-focusgraph__grouphead">{{ g.label }} · {{ g.rows|length }}</div>
  {% if not g.rows %}<div class="pb-focusgraph__empty">Nothing at this depth.</div>{% else %}
  <ul class="pb-focusgraph__list">{% for r in g.rows %}
   <li><button type="button" hx-get="/graph/node/{{ r.id }}" class="pb-focusgraph__row pb-focusgraph__row--{{ r.risk }}{{ ' pb-focusgraph__row--selected' if r.is_selected }}">
    <span class="pb-focusgraph__dot" aria-hidden="true"></span>
    <span class="pb-focusgraph__text"><span class="pb-focusgraph__label">{{ r.label }}</span><span class="pb-focusgraph__meta">{{ g.dir }} · {{ r.edge_type }} · {{ r.degree }} refs</span></span>
   </button></li>{% endfor %}</ul>{% endif %}
 </div>
 {% endfor %}
</div>
```

## CausalChain

Stage label is positional. The arrow glyph, the orientation flip and the hiding of the last connector are all CSS — the template never decides them. Add `pb-causalchain--vertical` to force stacking (it stacks on its own below 760px); swap the `<button>` for `<div class="pb-causalchain__card">` when steps are not clickable.

```jinja
<ol class="pb-causalchain{{ ' pb-causalchain--confirming' if confirming }}">
 {% for s in steps %}
 <li class="pb-causalchain__step pb-causalchain__step--{{ s.tone }}">
  <button type="button" class="pb-causalchain__card" hx-get="{{ s.to }}" hx-target="#detail">
   <span class="pb-causalchain__spine" aria-hidden="true"></span>
   <span class="pb-causalchain__stage"><span class="pb-causalchain__dot" aria-hidden="true"></span><span class="pb-causalchain__stagelabel">{{ 'Root cause' if loop.first else 'Symptom' if loop.last else 'Effect' }}</span></span>
   <span class="pb-causalchain__title">{{ s.title }}</span>
   {% if s.detail %}<span class="pb-causalchain__detail">{{ s.detail }}</span>{% endif %}
  </button>
  <span class="pb-causalchain__link" aria-hidden="true"></span>
 </li>
 {% endfor %}
</ol>
```
