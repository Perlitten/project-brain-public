import { Suspense } from "react";
import { getRegisteredSources } from "@/lib/project-sources";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import { syncRepository } from "@/lib/actions/git";
import { ConnectRepository } from "@/components/ConnectRepository";
import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Meter, PageHead, Panel, StatusLine, Table, type PageStatus } from "@/components/ui";
import { getCorpus, getPagedIndexRuns, getRepositoryFeed } from "@/lib/data";
import { canReindex, redactRemote, repositoryAction, repositoryStatus, repositorySummary, SOURCE_UNAVAILABLE_HELP } from "@/lib/repository-status";
import { restrictedMember } from "@/lib/team-session";
import type { OriginPoll, Repository } from "@/lib/types";

export const metadata = { title: "Projects" };

export default function Projects(props: { searchParams: Promise<{ repo?: string }> }) {
  return <><SectionTabs section="projects" active="/projects" /><PageHead title="Projects" lede="Repositories Brain has indexed for your agents, and how current each one is." actions={<ConnectRepository />} /><Suspense fallback={<p role="status" aria-live="polite">Loading measurements…</p>}><ProjectsData {...props} /></Suspense></>;
}
async function ProjectsData({ searchParams }: { searchParams: Promise<{ repo?: string }> }) {
  const { repo: selected } = await searchParams;
  // Registration is separate from the default repository used by agents.
  const [feed, member] = await Promise.all([getRepositoryFeed(), restrictedMember()]);
  const registered = member ? [] : await getRegisteredSources();
  const repositories = feed.repositories;
  const pendingSources = registered?.filter(source => source.indexedRepositoryId === null) ?? [];
  const current = selected ? repositories.find((r) => r.slug === selected || String(r.id) === selected || r.name.toLowerCase() === selected.toLowerCase()) : null;
  const detail = current && current.id > 0 ? current : null;
  const scope = detail ? `?repo=${encodeURIComponent(detail.slug)}` : "";
  const [corpus, history] = detail ? await Promise.all([getCorpus(detail.slug), getPagedIndexRuns(detail.slug, { size: 5 })]) : [null, null];
  const state = detail ? repositoryStatus(detail, corpus?.uncommitted) : null;
  const action = detail ? repositoryAction(detail) : null;
  const stale = repositories.filter((r) => repositoryStatus(r).tone !== "ok").length;
  const summary: PageStatus | undefined = state ? { tone: state.tone, text: state.tone === "ok" ? "Index matches the latest revision" : state.status, action: state.tone === "ok" || !action ? undefined : !member && canReindex(detail!) ? <RunButton variant="primary" action={async () => { "use server"; return startReindex({ repoPath: detail!.path }); }} label={`Re-index ${detail!.name}`} title={`Re-index ${detail!.name}`} /> : <Link className="btn btn--neutral" href={action.href}>{action.label}</Link> } : selected ? undefined : repositories.length ? { tone: stale ? "warn" : "ok", text: repositorySummary(repositories) } : undefined;
  return (
    <>
      {detail && (
        <nav className="project-switch" aria-label="Project">
          <Link className="project-switch__back" href="/projects">← All projects</Link>
          {repositories.length > 1 && (
            <span className="project-switch__list">
              {repositories.filter((r) => r.id > 0).map((r) => (
                <Link key={r.slug} href={`/projects?repo=${encodeURIComponent(r.slug)}`} aria-current={r.slug === detail.slug ? "page" : undefined}>{r.name}</Link>
              ))}
            </span>
          )}
          {repositories.length <= 1 && <span className="project-switch__current">{detail.name}</span>}
        </nav>
      )}
      {summary && <StatusLine {...summary} />}
      {registered === null && <p role="status">Registered source folders could not be read; indexed repositories are listed below.</p>}
      {!!pendingSources.length && <Panel id="registered-sources" title="Registered sources awaiting their first index"><ul>{pendingSources.map(source => <li key={source.path}><b>{source.name}</b><p className="mono">{source.path}</p><RunButton action={async () => { "use server"; return startReindex({ repoPath: source.path }); }} label="Index now" title={`Index ${source.name}`} /></li>)}</ul></Panel>}
      {repositories.some((r) => r.freshness === "source_missing") && <div className="callout tone-warn"><b>Source unavailable</b><span>{SOURCE_UNAVAILABLE_HELP}</span></div>}
      {!feed.available ? <Panel id="projects" title="Connected repositories"><EmptyState title="Projects unavailable" body={`${feed.note ?? "Brain didn’t return its project list"}. Reload in a moment.`} /></Panel> : selected && !detail ? <Panel id="unknown-project" title="Project not found"><EmptyState title="That project is unavailable" body="Choose a connected project from the list. Brain did not fall back to another repository." /></Panel> : detail ? (
        <Panel id="source" title="Index" desc={detail.path || "Repository path unavailable"} actions={<Link className="link" href={`/indexing${scope}`}>Index runs</Link>}>
          <div className="project-detail">
            <dl className="stats">
              <div className="stat"><dt className="stat__label">Indexed pieces</dt><dd className="stat__value num">{corpus?.available === false || !corpus ? "—" : corpus.chunks.toLocaleString("en-US")}</dd></div>
              <div className="stat"><dt className="stat__label">Latest revision</dt><dd className="stat__value num">{detail.head || "—"}</dd><dd className="stat__note">{detail.branch ? `branch ${detail.branch}` : "branch not recorded"}</dd></div>
              {detail.originPoll && <OriginStat p={detail.originPoll} />}
              <div className="stat"><dt className="stat__label">Latest index run</dt><dd className="stat__value num">{history?.available === false ? "—" : history?.items[0]?.startedAt ?? "None"}</dd><dd className="stat__note">{history?.items[0]?.status ?? (history?.available === false ? "History unavailable" : "Nothing recorded")}</dd></div>
            </dl>
            {corpus?.meters.length ? <div className="meters">{corpus.meters.map((m) => <Meter key={m.label} meter={m} />)}</div> : <p className="text-dim">Coverage unavailable</p>}
            <p className="text-faint project-detail__note">Coverage counts indexed code. It does not measure whether search results are relevant.</p>
          </div>
        </Panel>
      ) : <Panel id="projects" title="Connected repositories" flush>
        {repositories.length === 0 ? <EmptyState title="No projects connected" body={member ? "No project is shared with you yet. Ask a team admin to give you a role on one." : "Connect a repository to give Brain code it can index and search."} action={member ? undefined : <Link className="btn btn--primary" href="/setup">Connect a project</Link>} /> : (
          <Table caption="Connected repositories" rows={repositories} rowKey={(r) => r.slug} columns={[
            { head: "Project", cell: (r) => <><Link className="link" href={`/projects?repo=${encodeURIComponent(r.slug)}`}>{r.name}</Link><span className="sub mono">{r.path || "path unavailable"}</span></> },
            { head: "Freshness", cell: (r) => <Chip tone={repositoryStatus(r).tone}>{repositoryStatus(r).status}</Chip> },
            { head: "Branch", cell: (r) => <span className="mono">{r.branch || "—"}</span> },
            { head: "Origin", cell: (r) => <OriginCell r={r} canSync={!member} /> },
            { head: "Indexed revision", cell: (r) => r.head || "—", align: "right" },
            // Members index through their role on the project page; the owner re-indexes from the row.
            ...(member ? [] : [{ head: <span className="sr-only">Actions</span>, cell: (r: (typeof repositories)[number]) => canReindex(r) ? <RunButton small variant={repositoryStatus(r).tone === "ok" ? "neutral" : "primary"} action={async () => { "use server"; return startReindex({ repoPath: r.path }); }} label="Re-index" title={`Re-index ${r.name}`} /> : null, align: "right" as const }]),
          ]} />
        )}
      </Panel>}
    </>
  );
}

// One table cell: how far behind origin a repository is, and a Sync button that
// fetches now (the same job the scheduled origin check runs).
function OriginCell({ r, canSync }: { r: Repository; canSync: boolean }) {
  const p = r.originPoll;
  const chip = !p ? <span className="text-faint">not checked</span>
    : p.status === "ok" ? (p.behind ? <Chip tone="warn">{p.behind} behind</Chip> : p.behind === 0 ? <Chip tone="ok">Up to date</Chip> : <Chip tone="idle">Checked</Chip>)
    : p.status === "unconfigured" ? <span className="text-faint">no origin</span>
    : <Chip tone="bad">Check failed</Chip>;
  return <span className="origin-cell">
    {chip}
    {canSync && r.path && <RunButton small action={async () => { "use server"; return syncRepository(r.path); }} label="Sync" title={`Sync ${r.name}`} />}
    {p?.error && <span className="sub text-bad">{redactRemote(p.error)}</span>}
  </span>;
}

// What the scheduled origin check last saw; set up in Settings → Git & origin sync.
function OriginStat({ p }: { p: OriginPoll }) {
  const value = p.status === "ok" ? (p.behind ? `${p.behind} new commit${p.behind === 1 ? "" : "s"}` : p.behind === 0 ? "Up to date" : "Checked") : p.status === "unconfigured" ? "No origin" : "Check failed";
  return (
    <div className="stat">
      <dt className="stat__label">Origin</dt>
      <dd className="stat__value">{value}</dd>
      <dd className="stat__note">{p.at ? `checked ${p.at}` : "not checked yet"}</dd>
      {p.remote && <dd className="stat__note mono">{redactRemote(p.remote)}</dd>}
      {p.error && <dd className="stat__note text-bad">{redactRemote(p.error)}</dd>}
    </div>
  );
}
