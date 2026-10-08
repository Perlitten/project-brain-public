import Link from "next/link";
import { BackgroundJobs } from "@/components/BackgroundJobs";
import { MemoryMap } from "@/components/MemoryMap";
import { QualityEvidence } from "@/components/QualityEvidence";
import { RequestChart } from "@/components/RequestCharts";
import { Chip, EmptyState, Meter, Panel } from "@/components/ui";
import { dashboardPeriod, getQualityEvidence, getRequestTelemetry, measuredNumber } from "@/lib/dashboard";
import { dataSource, getCodeModules, getCorpus, getJobs, getReports, getRepositories } from "@/lib/data";
import { repositoryStatus } from "@/lib/repository-status";
import "./dashboard.css";

type Props = { searchParams: Promise<{ repo?: string; period?: string }> };
const periodWords = { "24h": "Last 24 hours", "7d": "Last 7 days", "30d": "Last 30 days" };
const when = (at: string) => new Date(at).toLocaleString("en-GB", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export default async function Dashboard({ searchParams }: Props) {
  const { repo: slug, period: inputPeriod } = await searchParams;
  const period = dashboardPeriod(inputPeriod);
  const repositories = await getRepositories();
  const selected = slug ? repositories.find((r) => r.slug === slug) : undefined;
  const projects = slug ? selected ? [selected] : [] : repositories;
  const [telemetry, snapshots, modules, reports, evidence] = await Promise.all([
    getRequestTelemetry(period, slug ? selected?.id ?? 0 : undefined),
    Promise.all(projects.map(async (repo) => {
      const [corpus, jobs] = await Promise.all([getCorpus(repo.slug), getJobs(repo.slug)]);
      return { repo, corpus, jobs };
    })),
    selected ? getCodeModules(selected.slug) : Promise.resolve([]),
    getReports(),
    getQualityEvidence(),
  ]);
  const jobs = snapshots.flatMap((s) => s.jobs.slice(0, 3)).slice(0, 6);
  const validCoverage = snapshots.filter((s) => s.corpus.available !== false && s.corpus.meters.some((m) => m.label === "Searchable"));
  const chunks = validCoverage.reduce((sum, s) => sum + (s.corpus.meters.find((m) => m.label === "Searchable")?.parts.reduce((n, p) => n + p.count, 0) ?? 0), 0);
  const searchable = validCoverage.reduce((sum, s) => sum + (s.corpus.meters.find((m) => m.label === "Searchable")?.parts.filter((p) => p.tone === "filled").reduce((n, p) => n + p.count, 0) ?? 0), 0);
  const coverage = chunks > 0 && validCoverage.length === projects.length ? searchable === chunks ? 100 : Math.min(99.9, Math.floor(searchable / chunks * 1000) / 10) : null;
  const attention = projects.filter((r) => repositoryStatus(r).tone !== "ok");
  const scope = slug ? selected?.name ?? "Unknown project" : "All projects";
  const withRepo = (path: string) => {
    const query = new URLSearchParams({ ...(slug ? { repo: slug } : {}), ...(path === "/logs" ? { period } : {}) });
    return `${path}${query.size ? `?${query}` : ""}`;
  };
  const observed = telemetry?.observed_at ?? new Date().toISOString();
  const total = telemetry?.total;
  const errorRate = total ? telemetry!.failed / total * 100 : null;
  const contextRate = telemetry?.context_total ? telemetry.context_success / telemetry.context_total * 100 : null;
  const metrics = [
    { label: "Agent requests", value: measuredNumber(total), detail: telemetry ? `${measuredNumber(telemetry.clients)} known identities · ${measuredNumber(telemetry.requests.unattributed)} unattributed attempts` : "Request measurement unavailable", href: "#traffic" },
    { label: "Context delivered", value: measuredNumber(contextRate, "%"), detail: telemetry ? `${measuredNumber(telemetry.context_success)} / ${measuredNumber(telemetry.context_total)} context requests · technical success` : "No delivery measurements", href: "#latency" },
    { label: "Failed requests", value: measuredNumber(telemetry?.failed), detail: telemetry ? `${measuredNumber(errorRate, "%")} of ${measuredNumber(total)} requests · includes timeouts` : "No failure measurements", href: "#traffic" },
    { label: "Searchable memory", value: measuredNumber(coverage, "%"), detail: chunks ? `${measuredNumber(searchable)} / ${measuredNumber(chunks)} eligible pieces · latest index` : "Coverage unavailable", href: "#projects" },
  ];
  return <div className="dashboard">
    <header className="dashboard__head"><div><h1>Dashboard</h1><p>{scope} <span className="text-faint">/ Memory, usage and retrieval</span></p></div><nav className="dashboard__period" aria-label="Measurement period">{(["24h", "7d", "30d"] as const).map((p) => <Link key={p} href={`/?${new URLSearchParams({ ...(slug ? { repo: slug } : {}), period: p })}`} aria-current={p === period ? "page" : undefined}>{p === "24h" ? "24 hours" : p === "7d" ? "7 days" : "30 days"}</Link>)}</nav></header>
    <div className="dashboard__status"><div><Chip tone={!projects.length ? "idle" : attention.length ? "warn" : "ok"}>{!projects.length ? "No project data" : attention.length ? "Check memory freshness" : "Memory is current"}</Chip><span>{attention.length ? `${attention.length} of ${projects.length} projects need a freshness check` : projects.length ? `${projects.length} connected project${projects.length === 1 ? "" : "s"}` : "Choose an existing project or connect a repository"}</span></div><span className="dashboard__observed">Observed {when(observed)} UTC</span></div>
    {dataSource === "demo" && <p className="dashboard__notice">Index state below uses demo data. Request telemetry is not simulated.</p>}
    {slug && !selected && <EmptyState title="Project not found" body="This repository is not in Brain. Choose an existing project using the project switcher." />}
    <div className="dashboard__metrics">{metrics.map((m) => <Link key={m.label} className="dashboard__metric" href={m.href}><span>{m.label}</span><strong className="num">{m.value}</strong><small>{m.detail}</small></Link>)}</div>
    <p className="dashboard__measurement">{periodWords[period]} for requests. Coverage uses the latest index, independently of this period. {telemetry?.collection_started_at ? `Request collection began ${when(telemetry.collection_started_at)} UTC.${telemetry.partial ? " This period is only partially measured." : ""}` : "Request history is unavailable from this server. It is not zero."}</p>
    <div className="dashboard__trends">
      <Panel id="traffic" title="Agent requests" desc={`${periodWords[period]} · Recorded HTTP / MCP search and context attempts · UTC`} actions={<Link className="link" href={withRepo("/logs")}>Activity</Link>}><RequestChart buckets={telemetry?.buckets ?? []} kind="requests" available={Boolean(telemetry)} /><p className="dashboard__legend"><span className="dot tone-ok" />Requests <span className="dot tone-bad" />Failures</p></Panel>
      <Panel id="latency" title="Response time" desc="Server handling time, including failures. Excludes auth and transport; relevance is measured separately."><div className="dashboard__latencies">{(["search", "context"] as const).map((kind) => <div key={kind}><span>{kind === "search" ? "Search" : "Context"}</span><strong className="num">{measuredNumber(telemetry?.latency[kind].p95_ms, " ms")}</strong><small>p95 · p50 {measuredNumber(telemetry?.latency[kind].p50_ms, " ms")} · {measuredNumber(telemetry?.latency[kind].samples)} samples</small></div>)}</div><RequestChart buckets={telemetry?.buckets ?? []} kind="latency" available={Boolean(telemetry)} /></Panel>
    </div>
    <div className="dashboard__lower">
      <Panel id="projects" title="Project memory" desc="Latest index state. Coverage is the share of searchable code, not answer correctness." actions={<Link className="link" href={withRepo("/projects")}>Projects</Link>}>
        {snapshots.length ? <ul className="dashboard__projects">{snapshots.map(({ repo, corpus }) => <li key={repo.id}><div className="dashboard__project-head"><Link className="link" href={`/projects?repo=${encodeURIComponent(repo.slug)}`}>{repo.name}</Link><Chip tone={repositoryStatus(repo).tone}>{repositoryStatus(repo).status}</Chip></div><p className="sub">Indexed revision <code>{repo.head || "unknown"}</code> · {repo.branch || "branch unknown"}</p>{corpus.meters.length ? <div className="meters">{corpus.meters.map((m) => <Meter key={m.label} meter={m} />)}</div> : <p className="sub">Coverage unavailable</p>}</li>)}</ul> : <EmptyState title="No project memory to show" body="Connect a repository and complete its first index." />}
      </Panel>
      <div className="stack"><Panel id="attention" title="Needs attention" desc="Current memory state, with a next step. Findings are recorded observations, not confirmed bugs.">{attention.length ? <ul className="dashboard__attention">{attention.slice(0, 4).map((r) => <li key={r.id}><b>{r.name}</b><p>{repositoryStatus(r).status}</p><Link className="link" href={`/indexing?repo=${encodeURIComponent(r.slug)}`}>Inspect indexing</Link></li>)}</ul> : <p className="text-dim">{projects.length ? "No freshness warning reported for the selected projects." : "Project state is unavailable."}</p>}<Link className="link dashboard__findings" href={withRepo("/insights")}>Review findings across Brain</Link></Panel>
      <Panel id="quality" title="Quality evidence" desc="Saved test artifacts across Brain. These are separate from live request success." actions={<Link className="link" href={withRepo("/quality")}>Quality</Link>}><QualityEvidence evidence={evidence} compact />{reports.length ? <ul className="dashboard__reports">{reports.slice(0, 3).map((r) => <li key={r.id}><Link href={`/reports/${encodeURIComponent(r.id)}`}>{r.title}</Link><span className="sub">{r.kind} · {r.createdAt}</span></li>)}</ul> : <EmptyState title="No saved evaluations" body="Run a search benchmark to collect quality evidence." />}<p className="dashboard__utility">Task usefulness and token, time or cost savings: <b>not measured</b>.</p></Panel></div>
    </div>
    {selected && modules.some((m) => m.chunks > 0) && <div className="dashboard__map"><MemoryMap modules={modules} repoName={selected.name} scanning={jobs.some((j) => j.status === "running" && j.kind.startsWith("index"))} /><p className="sub">Latest indexed snapshot. The visualization does not represent live agent activity.</p></div>}
    <Panel id="jobs" title="Recent background jobs" desc="Latest recorded jobs per selected project. Historical failures do not establish an unresolved incident." actions={<Link className="link" href={withRepo("/logs")}>All activity</Link>} flush><BackgroundJobs jobs={jobs} /></Panel>
  </div>;
}
