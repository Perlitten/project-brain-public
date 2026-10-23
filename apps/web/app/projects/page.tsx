import { Suspense } from "react";
import { getRegisteredSources } from "@/lib/project-sources";
import { RunButton } from "@/components/act";
import { startReindex } from "@/lib/actions/jobs";
import { AddProject } from "@/components/AddProject";
import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Meter, PageHead, Panel, StatusLine, Table, type PageStatus } from "@/components/ui";
import { getCorpus, getPagedIndexRuns, getRepositoryFeed } from "@/lib/data";
import { repositoryAction, repositoryStatus, repositorySummary, SOURCE_UNAVAILABLE_HELP } from "@/lib/repository-status";
import { restrictedMember } from "@/lib/team-session";

export const metadata = { title: "Projects" };

export default function Projects(props: { searchParams: Promise<{ repo?: string }> }) {
  return <><PageHead title="Projects" lede="Repositories Brain has indexed for your agents, and how current each one is." actions={<AddProject />} /><SectionTabs section="projects" active="/projects" /><Suspense fallback={<p role="status" aria-live="polite">Loading measurements…</p>}><ProjectsData {...props} /></Suspense></>;
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
  const summary: PageStatus | undefined = state ? { tone: state.tone, text: state.tone === "ok" ? "Index matches the latest revision" : state.status, action: state.tone === "ok" || !action ? undefined : <Link className="btn btn--neutral" href={action.href}>{action.label}</Link> } : selected ? undefined : repositories.length ? { tone: stale ? "warn" : "ok", text: repositorySummary(repositories) } : undefined;
  return (
    <>
      {summary && <StatusLine {...summary} />}
      {registered === null && <p role="status">Registered source folders could not be read; indexed repositories are listed below.</p>}
      {!!pendingSources.length && <Panel id="registered-sources" title="Registered sources awaiting their first index"><ul>{pendingSources.map(source => <li key={source.path}><b>{source.name}</b><p className="mono">{source.path}</p><RunButton action={async () => { "use server"; return startReindex({ repoPath: source.path }); }} label="Index now" title={`Index ${source.name}`} /></li>)}</ul></Panel>}
      {detail && <Link className="btn btn--neutral" href="/projects">All projects</Link>}
      {repositories.some((r) => r.freshness === "source_missing") && <div className="callout tone-warn"><b>Source unavailable</b><span>{SOURCE_UNAVAILABLE_HELP}</span></div>}
      {!feed.available ? <Panel id="projects" title="Connected repositories"><EmptyState title="Projects unavailable" body={`${feed.note ?? "Brain didn’t return its project list"}. Reload in a moment.`} /></Panel> : selected && !detail ? <Panel id="unknown-project" title="Project not found"><EmptyState title="That project is unavailable" body="Choose a connected project from the list. Brain did not fall back to another repository." /></Panel> : detail ? (
        <Panel id="source" title="Index" desc={detail.path || "Repository path unavailable"} actions={<Link className="link" href={`/indexing${scope}`}>Index runs</Link>}>
          <div className="project-detail">
            <dl className="stats">
              <div className="stat"><dt className="stat__label">Indexed pieces</dt><dd className="stat__value num">{corpus?.available === false || !corpus ? "—" : corpus.chunks.toLocaleString("en-US")}</dd></div>
              <div className="stat"><dt className="stat__label">Latest revision</dt><dd className="stat__value num">{detail.head || "—"}</dd><dd className="stat__note">{detail.branch ? `branch ${detail.branch}` : "branch not recorded"}</dd></div>
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
            { head: "Indexed revision", cell: (r) => r.head || "—", align: "right" },
          ]} />
        )}
      </Panel>}
    </>
  );
}
