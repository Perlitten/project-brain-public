"use client";

import Form from "next/form";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { AtlasLegend, AtlasMap, AtlasVerdict, reveal, type AtlasRegionData, type AtlasTileData } from "./Atlas";
import { Icon } from "@/components/Icon";
import { DecisionActions, RecordHint, RuleActions } from "@/components/MemoryActions";
import { NewMemoryForm, type MemoryPrefill } from "@/components/NewMemory";
import { decisionState, EVERY_PROJECT, memoryVerdict, regionFor, ruleSpan, type TileState } from "@/lib/atlas";
import type { Decision, Rule } from "@/lib/types";

export type MemoryTab = "current" | "decisions" | "rules" | "replaced";
export interface MemoryProject { slug: string; name: string; path: string }

const statusWords: Record<Decision["status"], string> = { accepted: "agreed", proposed: "proposed", superseded: "retired or replaced", validated_unmerged: "verified, not merged yet" };
const severityWords: Record<Rule["severity"], string> = { block: "blocks the change", warn: "warns", advise: "suggests" };
const TABS: { id: MemoryTab; label: string }[] = [
  { id: "current", label: "Current" },
  { id: "decisions", label: "Decisions" },
  { id: "rules", label: "Rules" },
  { id: "replaced", label: "Replaced" },
];

type Item =
  | { key: string; kind: "decision"; region: string; record: Decision }
  | { key: string; kind: "rule"; region: string; record: Rule };

// A rule is worth a look when it reached nobody, or broke in a large share of the tasks it reached.
const ruleState = (r: Rule): TileState => r.delivered30d === 0 ? "chg" : "ok";
const ruleNoisy = (r: Rule) => (r.delivered30d ?? 0) >= 4 && (r.caught30d ?? 0) / (r.delivered30d || 1) >= 0.3;
const n = (v: number) => v.toLocaleString("en-US");
// Matches the atlas.css breakpoint where the inspector drops below the map.
const STACKED = "(max-width: 900px)";

function ruleUse(r: Rule): string {
  if (r.delivered30d === undefined) return "Brain does not count this rule's use yet.";
  if (r.delivered30d === 0) return "No agent got this rule in the last 30 days.";
  const caught = r.caught30d === undefined ? "" : r.caught30d === 0 ? ", never caught broken" : `, caught broken ${n(r.caught30d)} ${r.caught30d === 1 ? "time" : "times"}`;
  return `Given to agents ${n(r.delivered30d)} ${r.delivered30d === 1 ? "time" : "times"} in 30 days${caught}.`;
}

export function MemoryBoard({ decisions, replaced, rules, projects, scoped, tab: initialTab, query, size, totals, repoPath, unavailable, prefill }: {
  decisions: Decision[];
  replaced: Decision[];
  rules: Rule[];
  projects: MemoryProject[];
  /** The page is filtered to one project: its region is the only one, global memory aside. */
  scoped: boolean;
  tab: MemoryTab;
  /**
   * The address: `q` searches ids, titles and text, and a record whose id it
   * names exactly is selected on arrival; `page` steps through records past
   * the first `size` of each kind.
   */
  query: { repo?: string; q: string; page: number };
  size: number;
  /** Every record Brain counts for the search, drawn or not. */
  totals: { decisions: number; rules: number; replaced: number };
  repoPath?: string;
  unavailable: { decisions: boolean; rules: boolean; replaced: boolean };
  /** Opens the record panel filled in, e.g. a finding turned into a rule. */
  prefill?: MemoryPrefill;
}) {
  const [tab, setTab] = useState<MemoryTab>(initialTab);
  const [hover, setHover] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [pulse, setPulse] = useState<string | null>(null);
  const [recording, setRecording] = useState(Boolean(prefill));
  const inspector = useRef<HTMLElement>(null);
  const pick = query.q;

  const region = (x: Decision | Rule) => regionFor(x.repoPath, x.repo, projects);
  const current: Item[] = [
    ...rules.map((r): Item => ({ key: `r:${r.id}`, kind: "rule", region: region(r), record: r })),
    ...decisions.map((d): Item => ({ key: `d:${d.id}`, kind: "decision", region: region(d), record: d })),
  ];
  const old: Item[] = replaced.map((d) => ({ key: `d:${d.id}`, kind: "decision", region: region(d), record: d }));
  const items = tab === "replaced" ? old : current;
  const all = [...current, ...old];
  const find = (key: string | null) => (key ? all.find((i) => i.key === key) : undefined);

  // Arriving from a link (?q=42 or ?q=R-014) opens that record; otherwise the one the sentence names.
  useEffect(() => {
    const byId = pick && all.find((i) => i.record.id === pick || i.record.id === `D-${pick}`);
    if (byId) { setSelected(byId.key); if (byId.kind === "decision" && byId.record.status === "superseded") setTab("replaced"); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pick]);
  // On a phone the inspector sits under the map: bring the chosen record up to where the reader is.
  useEffect(() => {
    if (selected && window.matchMedia(STACKED).matches) reveal(inspector.current);
  }, [selected]);
  useEffect(() => {
    if (!pulse) return;
    const t = setTimeout(() => setPulse(null), 1000);
    return () => clearTimeout(t);
  }, [pulse]);

  const most = Math.max(0, ...rules.map((r) => r.delivered30d ?? 0));
  const shownOn = (i: Item) => tab === "current" || tab === "replaced" || (tab === "rules" ? i.kind === "rule" : i.kind === "decision");
  const tile = (i: Item): AtlasTileData => i.kind === "rule"
    ? {
        key: i.key, name: i.record.rule, state: ruleState(i.record), span: ruleSpan(i.record.delivered30d, most),
        note: i.record.delivered30d === undefined ? i.record.id : `${i.record.id} · ${n(i.record.delivered30d)}×`,
        noteWarn: ruleNoisy(i.record),
        label: `Rule ${i.record.id}: ${i.record.rule}. ${ruleUse(i.record)}`,
        dim: !shownOn(i), ring: selected === i.key || hover === i.key, pulse: pulse === i.key,
      }
    : {
        key: i.key, name: i.record.title, state: decisionState(i.record.status), span: 2,
        note: `${i.record.id} · ${statusWords[i.record.status]}`,
        label: `Decision ${i.record.id}, ${statusWords[i.record.status]}: ${i.record.title}`,
        dim: !shownOn(i), ring: selected === i.key || hover === i.key, pulse: pulse === i.key,
      };

  // One region per project that has records, then memory shared by every project.
  const keys = [...projects.map((p) => p.slug), EVERY_PROJECT];
  const regions: AtlasRegionData[] = keys.flatMap((key) => {
    const own = items.filter((i) => i.region === key);
    if (!own.length) return [];
    const tiles = own.map(tile);
    const project = projects.find((p) => p.slug === key);
    const rulesHere = own.filter((i) => i.kind === "rule").length;
    const decisionsHere = own.length - rulesHere;
    return [{
      key,
      name: project?.name ?? "Every project",
      href: project && !scoped ? `/memory?repo=${encodeURIComponent(project.slug)}` : undefined,
      note: [decisionsHere ? `${decisionsHere} ${decisionsHere === 1 ? "decision" : "decisions"}` : "", rulesHere ? `${rulesHere} ${rulesHere === 1 ? "rule" : "rules"}` : ""].filter(Boolean).join(" · "),
      weight: Math.min(3, Math.max(1, tiles.reduce((s, t) => s + t.span, 0) / 6)),
      tiles,
    }];
  });
  const silent = scoped ? [] : projects.filter((p) => !current.some((i) => i.region === p.slug));

  const counts = tab === "replaced"
    ? [{ state: "miss" as const, count: old.length, label: "replaced" }]
    : [
        { state: "ok" as const, count: current.filter((i) => tile(i).state === "ok").length, label: "in use" },
        { state: "chg" as const, count: current.filter((i) => tile(i).state === "chg").length, label: "proposed, or reached no agent" },
      ];
  const shown = find(hover) ?? find(selected);
  const tabCount = (t: MemoryTab) => t === "replaced" ? totals.replaced : t === "rules" ? totals.rules : t === "decisions" ? totals.decisions : totals.rules + totals.decisions;
  // The sentence describes what is in view: the whole memory on its first page, otherwise the search or the page.
  const first = (query.page - 1) * size;
  const matches = tabCount("current") + totals.replaced;
  const segments = query.q
    ? [`${n(matches)} ${matches === 1 ? "record matches" : "records match"} “${query.q}”${query.page > 1 ? `, showing ${n(first + 1)}–${n(first + size)} of each kind` : ""}.`]
    : query.page > 1 ? [`Showing records ${n(first + 1)}–${n(first + size)} of each kind.`]
    : memoryVerdict({ decisions, rules });
  // Each kind pages on its own; the tab decides which of them the pager steps through.
  const shownTotal = Math.max(...(tab === "replaced" ? [totals.replaced] : tab === "rules" ? [totals.rules] : tab === "decisions" ? [totals.decisions] : [totals.rules, totals.decisions]));
  const pages = Math.max(1, Math.ceil(shownTotal / size));
  const href = (page: number, q = query.q) => {
    const p = new URLSearchParams();
    if (query.repo) p.set("repo", query.repo);
    if (tab !== "current") p.set("view", tab);
    if (q) p.set("q", q);
    if (page > 1) p.set("page", String(page));
    return `/memory${p.size ? `?${p}` : ""}`;
  };
  const tabUnavailable = (t: MemoryTab) => t === "replaced" ? unavailable.replaced : t === "rules" ? unavailable.rules : t === "decisions" ? unavailable.decisions : unavailable.rules && unavailable.decisions;

  return (
    <div className="atlas">
      <AtlasVerdict
        segments={segments}
        sub={unavailable.decisions || unavailable.rules
          ? `Brain did not return ${unavailable.decisions && unavailable.rules ? "decisions or rules" : unavailable.decisions ? "decisions" : "rules"} just now, so this count is partial. Reload in a moment.`
          : "Agents get the decisions and rules that match their task with every context pack."}
        actions={<button type="button" className="btn btn--neutral" aria-expanded={recording} onClick={() => setRecording((v) => !v)}>{recording ? "Hide" : "Record a decision or rule"}</button>}
        onMention={(ref) => setHover(ref ? `r:${ref}` : null)}
        onPick={(ref) => { setSelected(`r:${ref}`); setPulse(`r:${ref}`); setTab((t) => (t === "decisions" || t === "replaced" ? "current" : t)); }}
      />
      {recording && <div className="atlas-record" id="record">
        <section className="atlas-record__form" aria-label="Record a decision or rule">
          <NewMemoryForm projects={projects} repoPath={repoPath} prefill={prefill} onDone={() => setRecording(false)} />
        </section>
        <aside className="atlas-record__aside">
          <p className="atlas-record__lead">Agents record memory themselves, too:</p>
          <RecordHint kind="decision" repoPath={repoPath} />
          <RecordHint kind="rule" repoPath={repoPath} />
        </aside>
      </div>}

      <div className="atlas-split">
        <section aria-label="Recorded memory by project">
          <Form action="/memory" className="atlas-search" role="search">
            {query.repo && <input type="hidden" name="repo" value={query.repo} />}
            {tab !== "current" && <input type="hidden" name="view" value={tab} />}
            <label className="atlas-search__field">
              <Icon name="search" size={15} />
              <input className="input" type="search" name="q" defaultValue={query.q} key={query.q} placeholder="Search by id, title or text" aria-label="Search decisions and rules" />
            </label>
            {query.q && <Link className="atlas-search__clear" href={href(1, "")}>Clear search</Link>}
          </Form>
          <div className="atlas-tabs" role="group" aria-label="Memory views">
            {TABS.map((t) => (
              <button key={t.id} type="button" className="atlas-tab" aria-pressed={tab === t.id} onClick={() => { setTab(t.id); setHover(null); }}>
                {t.label}<span className="atlas-tab__count">{tabUnavailable(t.id) ? "—" : n(tabCount(t.id))}</span>
              </button>
            ))}
          </div>
          {regions.length ? <>
            <AtlasMap
              label={tab === "replaced" ? "Replaced decisions by project" : "Decisions and rules by project"}
              regions={regions}
              onHover={(_, key) => setHover(key)}
              onPick={(_, key) => setSelected((s) => (s === key ? null : key))}
            />
            <AtlasLegend items={counts} />
          </> : <p className="atlas-region__empty">
            {tabUnavailable(tab) ? "Brain did not return this view just now. Reload in a moment."
              : query.q ? <>No {tab === "replaced" ? "replaced decisions" : tab === "rules" ? "rules" : tab === "decisions" ? "decisions" : "decisions or rules"} match “{query.q}”{query.page > 1 ? " on this page" : ""}. <Link href={href(1, "")}>Clear search</Link></>
              : query.page > 1 ? <>Nothing more to show here. <Link href={href(1)}>Back to the first page</Link></>
              : tab === "replaced" ? "Decisions replaced by a newer version appear here."
              : "Nothing is recorded yet. Record a decision or rule and agents get it with their next task."}
          </p>}
          {pages > 1 && <nav className="atlas-pager" aria-label="Pages of records">
            {query.page > 1 ? <Link href={href(query.page - 1)} rel="prev">Previous {n(size)}</Link> : <span />}
            <span className="atlas-pager__where">{n(Math.min(first + 1, shownTotal))}–{n(Math.min(first + size, shownTotal))} of {n(shownTotal)}</span>
            {query.page < pages ? <Link href={href(query.page + 1)} rel="next">Next {n(size)}</Link> : <span />}
          </nav>}
          {silent.length > 0 && !query.q && query.page === 1 && <p className="atlas-foot">Nothing recorded for {silent.map((p) => p.name).join(", ")}.</p>}
        </section>

        <aside ref={inspector} aria-label="Selected record" aria-live="polite">
          {shown ? <Inspector item={shown} region={projects.find((p) => p.slug === shown.region)?.name ?? "Every project"} />
            : <div className="atlas-inspector atlas-inspector--idle">
                <p className="atlas-inspector__where">Point at a tile to read it. Click to keep it here, edit it or retire it.</p>
              </div>}
        </aside>
      </div>
    </div>
  );
}

function Inspector({ item, region }: { item: Item; region: string }) {
  if (item.kind === "rule") {
    const r = item.record;
    return (
      <div className="atlas-inspector" key={item.key}>
        <p className="atlas-inspector__id">{r.id} · rule · {severityWords[r.severity]}</p>
        <h2 className="atlas-inspector__title">{r.rule}</h2>
        <p className="atlas-inspector__where">{region} · <span className="mono">{r.scope}</span></p>
        <p className="atlas-inspector__use">{ruleUse(r)}</p>
        <div className="atlas-inspector__actions"><RuleActions rule={r} /></div>
      </div>
    );
  }
  const d = item.record;
  return (
    <div className="atlas-inspector" key={item.key}>
      <p className="atlas-inspector__id">{d.id} · decision · {statusWords[d.status]}</p>
      <h2 className="atlas-inspector__title">{d.title}</h2>
      <p className="atlas-inspector__where">{region} · <span className="mono">{d.scope}</span> · recorded {d.recordedAt}</p>
      {d.summary && <p className="atlas-inspector__body">{d.summary}</p>}
      <div className="atlas-inspector__actions"><DecisionActions decision={d} /></div>
    </div>
  );
}
