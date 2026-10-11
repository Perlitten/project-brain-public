import Link from "next/link";
import { BackgroundJobs } from "@/components/BackgroundJobs";
import { MemoryMap } from "@/components/MemoryMap";
import { QualityEvidence } from "@/components/QualityEvidence";
import { RequestChart } from "@/components/RequestCharts";
import { Chip, EmptyState, Meter, PageHead, Panel, type PageStatus } from "@/components/ui";
import { dashboardPeriod, getAgentValue, getQualityEvidence, getRequestTelemetry, measuredNumber, measuredShare } from "@/lib/dashboard";
import { dataSource, getCodeModules, getCorpus, getJobs, getRepositories } from "@/lib/data";
import { repositoryStatus } from "@/lib/repository-status";
import type { Tone } from "@/lib/types";
import "./dashboard.css";

type Props = { searchParams: Promise<{ repo?: string; period?: string }> };
const periodWords = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days" };
const when = (at: string) => new Date(at).toLocaleString("en-GB", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const severity: Record<Tone, number> = { bad: 3, warn: 2, info: 1, idle: 1, ok: 0 };

// The home screen answers, in order: is project memory current (status line),
// what each project holds (memory rows), whether agents are using it (requests),
// and what ran recently. Measurement caveats live in one disclosure at the end.
export default async function Dashboard({ searchParams }: Props) {
  const { repo: slug, period: inputPeriod } = await searchParams;
  const period = dashboardPeriod(inputPeriod);
  const repositories = await getRepositories();
  const selected = slug ? repositories.find((r) => r.slug === slug) : undefined;
  const projects = slug ? selected ? [selected] : [] : repositories;
  const [telemetry, worthData, snapshots, modules, evidence] = await Promise.all([
    getRequestTelemetry(period, slug ? selected?.id ?? 0 : undefined),
    getAgentValue(period, slug ? selected?.id ?? 0 : undefined),
    Promise.all(projects.map(async (repo) => {
      const [corpus, jobs] = await Promise.all([getCorpus(repo.slug), getJobs(repo.slug)]);
      return { repo, corpus, jobs };
    })),
    selected ? getCodeModules(selected.slug) : Promise.resolve([]),
    getQualityEvidence(),
  ]);
  const jobs = [...new Map(snapshots.flatMap((s) => s.jobs.slice(0, 3)).map((j) => [j.id, j])).values()].slice(0, 6);
  const validCoverage = snapshots.filter((s) => s.corpus.available !== false && s.corpus.meters.some((m) => m.label === "Searchable"));
  const chunks = validCoverage.reduce((sum, s) => sum + (s.corpus.meters.find((m) => m.label === "Searchable")?.parts.reduce((n, p) => n + p.count, 0) ?? 0), 0);
  const searchable = validCoverage.reduce((sum, s) => sum + (s.corpus.meters.find((m) => m.label === "Searchable")?.parts.filter((p) => p.tone === "filled").reduce((n, p) => n + p.count, 0) ?? 0), 0);
  const coverage = chunks > 0 && validCoverage.length === projects.length ? searchable === chunks ? 100 : Math.min(99.9, Math.floor(searchable / chunks * 1000) / 10) : null;
  const attention = projects.map((repo) => ({ repo, ...repositoryStatus(repo) })).filter((s) => s.tone !== "ok").sort((a, b) => severity[b.tone] - severity[a.tone]);
  const withRepo = (path: string, extra: Record<string, string> = {}) => {
    const query = new URLSearchParams({ ...(slug ? { repo: slug } : {}), ...extra });
    return `${path}${query.size ? `?${query}` : ""}`;
  };
  const observed = telemetry?.observed_at ?? new Date().toISOString();
  const total = telemetry?.total;
  const contextRate = telemetry?.context_total ? telemetry.context_success / telemetry.context_total * 100 : null;

  const first = attention[0];
  const status: PageStatus = slug && !selected
    ? { tone: "warn", text: <>This project is not connected to Brain<small>Nothing below is shown for another project in its place.</small></>, action: <Link className="btn btn--neutral" href="/projects">Choose a project</Link> }
    : !projects.length
      ? { tone: "idle", text: <>No projects connected yet<small>Brain has nothing to give agents until a repository is indexed.</small></>, action: <Link className="btn btn--primary" href="/setup">Connect a project</Link> }
      : first
        ? {
          tone: first.tone,
          text: <>{projects.length === 1 ? `${first.repo.name}: ${first.status.toLowerCase()}` : `${attention.length} of ${projects.length} projects need attention`}<small>{projects.length === 1 ? "Agents may get context from older code until it is reindexed." : attention.map((a) => `${a.repo.name} — ${a.status.toLowerCase()}`).join(" · ")}</small></>,
          action: <Link className="btn btn--neutral" href={`/indexing?repo=${encodeURIComponent(first.repo.slug)}`}>Open {first.repo.name}</Link>,
        }
        : { tone: "ok", text: <>Memory is current {projects.length === 1 ? `for ${projects[0].name}` : `in all ${projects.length} projects`}<small>{coverage !== null ? `${measuredNumber(coverage, "%")} of eligible code is searchable. ` : ""}Agents get context from the latest indexed revision.</small></> };

  const packs = worthData?.packs;
  const ruleUse = worthData?.rules;
  // Four numbers in the order an owner asks them: does recorded memory reach
  // the agents, how many packs went out and to whom, how big is what an agent
  // has to read, and do the rules anybody wrote ever fire. A missing count
  // stays a dash: "not measured yet" and "zero" are different answers, and
  // only one of them is something to act on.
  const worth = worthData && [
    {
      label: "Memory reach",
      value: measuredShare(packs?.memory_reach),
      note: packs?.measured
        ? `${measuredNumber(packs.with_memory)} of ${measuredNumber(packs.measured)} packs carried a rule, decision or learning`
        : "No pack in this window has its contents recorded",
    },
    {
      label: "Packs delivered",
      value: measuredNumber(packs?.total),
      note: packs
        ? `${measuredNumber(packs.attributed)} attributed to an agent · ${measuredNumber(packs.unattributed)} without a known caller`
        : "Not recorded",
    },
    {
      label: "Typical pack",
      value: measuredNumber(packs?.tokens.p50, " tok"),
      note: `${measuredNumber(packs?.files.p50)} files at the median · ${measuredNumber(packs?.tokens.p95, " tok")} in the largest 5%`,
    },
    {
      label: "Rules reaching agents",
      value: ruleUse?.delivered_rules == null ? "—" : `${measuredNumber(ruleUse.delivered_rules)} of ${measuredNumber(ruleUse.active)}`,
      note: ruleUse?.measurement_started_at
        ? `${measuredNumber(ruleUse.flagged_events)} review${ruleUse.flagged_events === 1 ? "" : "s"} cited a rule · ${measuredNumber(ruleUse.unused)} reached nobody`
        : "Rule usage is not being recorded on this server yet",
    },
  ];

  const usage = telemetry && [
    { label: "Requests", value: measuredNumber(total), note: `from ${measuredNumber(telemetry.clients)} agents · ${measuredNumber(telemetry.requests.unattributed)} without a known agent` },
    { label: "Context delivered", value: measuredNumber(contextRate, "%"), note: `${measuredNumber(telemetry.context_success)} of ${measuredNumber(telemetry.context_total)} context pack requests answered` },
    { label: "Failed", value: measuredNumber(telemetry.failed), note: total ? `${measuredNumber(telemetry.failed / total * 100, "%")} of requests, incl. timeouts` : "Including timeouts" },
    { label: "Slowest searches", value: measuredNumber(telemetry.latency.search.p95_ms, " ms"), note: `95% of searches were faster · context packs ${measuredNumber(telemetry.latency.context.p95_ms, " ms")}` },
  ];

  return <div className="dashboard">
    <PageHead title="Dashboard" lede="Whether your agents get current project memory, and what to fix next." status={status} />
    {dataSource === "demo" && <p className="callout tone-info"><span><b>Demo data.</b> Projects and jobs below are examples. Agent requests and what they got are never simulated.</span></p>}

    {selected && modules.some((m) => m.chunks > 0) && <div className="dashboard__map"><MemoryMap modules={modules} repoName={selected.name} scanning={jobs.some((j) => j.status === "running" && j.kind.startsWith("index"))} /></div>}

    {projects.length > 0 && <Panel id="projects" title="Project memory" desc="How much of each project's code Brain can hand to agents right now." actions={<Link className="link" href={withRepo("/projects")}>Projects</Link>} flush>
      <div className="memory-rows" role="table" aria-label="Project memory">
        <div className="memory-rows__head" role="row"><span role="columnheader">Project</span><span role="columnheader">Status</span><span role="columnheader">Code agents can use now</span></div>
        {snapshots.map(({ repo, corpus }) => {
          const state = repositoryStatus(repo);
          const meter = (label: string) => corpus.available === false ? undefined : corpus.meters.find((m) => m.label === label);
          // One number per project: how much of its code is indexed at the
          // current revision. "Searchable" alone overstates it (old copies count).
          const ready = meter("Up to date") ?? meter("Searchable");
          const behind = ready?.parts.filter((p) => p.tone === "missing" && p.count > 0).map((p) => p.text).join(" · ");
          return <div key={repo.id} className="memory-rows__row" role="row">
            <span role="cell" className="memory-rows__name"><Link href={`/projects?repo=${encodeURIComponent(repo.slug)}`}>{repo.name}</Link><span className="sub mono">{[repo.branch, repo.head].filter(Boolean).join(" · ") || "revision not recorded"}</span></span>
            <span role="cell"><Chip tone={state.tone}>{state.status}</Chip></span>
            <span role="cell" data-label="Usable now" className="memory-rows__ready">{ready ? <><Meter meter={ready} compact /><span className="sub">{behind || "All indexed code is current"}</span></> : <span className="text-faint">Not measured</span>}</span>
          </div>;
        })}
      </div>
    </Panel>}

    {/* The dashboard's first question is adoption, not speed: a fast server
        nobody asks, carrying memory that reaches no pack, is worth nothing. */}
    <Panel id="value" title="Value to agents" desc={`Who uses Brain and whether the memory you recorded reaches them, ${periodWords[period]}${packs?.partial ? " (contents recorded for part of it)" : ""}`} actions={<Link className="link" href={withRepo("/packs")}>Context packs</Link>}>
      {worth ? <>
        <dl className="usage">{worth.map((w) => <div key={w.label}><dt>{w.label}</dt><dd className="num">{w.value}</dd><dd className="usage__note">{w.note}</dd></div>)}</dl>
        {worthData.agents.length ? <div className="agents" role="table" aria-label="Agents using Brain">
          <div className="agents__head" role="row">
            <span role="columnheader">Agent</span>
            <span role="columnheader">Requests</span>
            <span role="columnheader">Packs</span>
            <span role="columnheader">Pack rate</span>
            <span role="columnheader">With memory</span>
            <span role="columnheader">Last seen</span>
          </div>
          {worthData.agents.map((a) => <div key={a.agent} className="agents__row" role="row">
            <span role="cell" className="agents__name">
              {a.agent === "unattributed" ? <span>Callers without a name</span> : <span className="mono">{a.agent}</span>}
              <span className="sub">{[a.surfaces.join(" · ") || "surface not recorded", a.failures ? `${a.failures} failed` : ""].filter(Boolean).join(" · ")}</span>
            </span>
            <span role="cell" data-num="Requests">{measuredNumber(a.requests)}</span>
            <span role="cell" data-num="Packs">{measuredNumber(a.packs)}</span>
            {/* A dash, not 0%, when this caller made no context request: with
                no denominator there is no rate to report. */}
            <span role="cell" data-num="Pack rate">{measuredShare(a.pack_rate)}</span>
            <span role="cell" data-num="With memory">{measuredNumber(a.packs_with_memory)}</span>
            <span role="cell" data-num="Last seen">{a.last_seen_at ? when(a.last_seen_at) : "—"}</span>
          </div>)}
        </div> : <p className="text-faint">No agent asked Brain for anything in this period.</p>}
      </> : <div className="usage-off">
        <p className="usage-off__title">Not measured on this server</p>
        <p>Brain records who asked for each context pack, what went into it, and which rules a review cited. Until that is reachable this stays empty instead of claiming your memory reaches nobody.</p>
      </div>}
    </Panel>

    <div className="dashboard__lower">
      <Panel id="traffic" title="Agent requests" desc={`Search and context calls from agents, ${periodWords[period]}${telemetry?.partial ? " (partly measured)" : ""}`} actions={<nav className="segmented" aria-label="Measurement period">{(["24h", "7d", "30d"] as const).map((p) => <Link key={p} href={`/?${new URLSearchParams({ ...(slug ? { repo: slug } : {}), period: p })}`} aria-current={p === period ? "page" : undefined} scroll={false}>{p}</Link>)}</nav>}>
        {usage ? <>
          <dl className="usage">{usage.map((u) => <div key={u.label}><dt>{u.label}</dt><dd className="num">{u.value}</dd><dd className="usage__note">{u.note}</dd></div>)}</dl>
          <RequestChart buckets={telemetry.buckets} kind="requests" />
          <p className="dashboard__legend"><span className="dot tone-ok" />Requests <span className="dot tone-bad" />Failures <Link className="link" href={withRepo("/logs", { period })}>Every request</Link></p>
        </> : <div className="usage-off">
          <p className="usage-off__title">Not measured on this server</p>
          <p>Brain records each search and context call once request telemetry is reachable. Until then this stays empty instead of showing zero.</p>
        </div>}
      </Panel>
      <div className="stack">
        <Panel id="jobs" title="Background jobs" desc="Code reads and checks Brain ran recently." actions={<Link className="link" href={withRepo("/logs")}>Activity</Link>} flush><BackgroundJobs jobs={jobs} /></Panel>
        <Panel id="quality" title="Search quality" desc="How often search finds the right file, from the last saved test run." actions={<Link className="link" href="/quality">Quality</Link>}><QualityEvidence evidence={evidence} compact /></Panel>
      </div>
    </div>

    <details className="disclosure">
      <summary>How these numbers are measured</summary>
      <ul>
        <li><b>Agent requests</b> are recorded HTTP <code>/search</code>, <code>/context</code> and MCP <code>search_code</code>, <code>prepare_task_context</code> calls, failures and timeouts included. {telemetry?.collection_started_at ? `Collection began ${when(telemetry.collection_started_at)} UTC.` : "This server returned no request history."} A dash means not measured, never zero.</li>
        <li><b>Memory reach</b> is the share of context packs that carried at least one rule, decision or learning. Only packs whose contents Brain recorded are counted{packs?.measurement_started_at ? `; recording began ${when(packs.measurement_started_at)} UTC` : ", and nothing has been recorded yet"}. Older packs count as not measured, never as empty.</li>
        <li><b>Pack rate</b> is how many durable packs an agent got per context call. It is not a count of its tasks: an agent can take context without a pack being stored, which is a normal outcome.</li>
        <li><b>Rules reaching agents</b> counts rules that went into at least one pack. “Cited a rule” counts diff reviews that caught something against one{ruleUse?.measurement_started_at ? `, recorded since ${when(ruleUse.measurement_started_at)} UTC` : ""}. A review that failed to run records nothing, so it never reads as a clean diff.</li>
        <li><b>Context delivered</b> is technical success: the call returned context. It does not say whether the context was right.</li>
        <li><b>Response time</b> is server handling time, failures included; authentication and transport are excluded.</li>
        <li><b>Code agents can use now</b> counts indexed pieces of code that match the latest revision. It comes from the latest index and does not depend on the selected period.</li>
        <li><b>Slowest searches</b> is the 95th percentile: 95% of searches finished faster than this.</li>
        <li><b>Search quality</b> is a saved benchmark on a fixed question set. Task usefulness and token, time or cost savings are not measured.</li>
        <li>A failed background job is a record of what happened, not proof of an open incident.</li>
      </ul>
      <p className="text-faint">Observed {when(observed)} UTC.</p>
    </details>
  </div>;
}
