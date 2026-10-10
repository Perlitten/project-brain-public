import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, Meter, PageHead, Panel, Table } from "@/components/ui";
import { getCorpus, getPagedIndexRuns, getRepositories, getRepository } from "@/lib/data";
import { repositoryStatus } from "@/lib/repository-status";

export const metadata = { title: "Projects" };

export default async function Projects({ searchParams }: { searchParams: Promise<{ repo?: string }> }) {
  const { repo: selected } = await searchParams;
  const repositories = await getRepositories();
  const current = selected ? await getRepository(selected) : null;
  const detail = current && current.id > 0 ? current : null;
  const scope = detail ? `?repo=${encodeURIComponent(detail.slug)}` : "";
  const [corpus, history] = detail ? await Promise.all([getCorpus(detail.slug), getPagedIndexRuns(detail.slug, { size: 5 })]) : [null, null];
  const state = detail ? repositoryStatus(detail) : null;
  const stale = repositories.filter((r) => repositoryStatus(r).tone !== "ok").length;
  return (
    <>
      <PageHead
        title={detail?.name ?? "Projects"}
        lede={detail ? "What Brain holds for this repository and how current it is." : "Repositories Brain has indexed for your agents, and how current each one is."}
        status={state ? { tone: state.tone, text: state.tone === "ok" ? "Index matches the latest revision" : state.status, action: state.tone === "ok" ? undefined : <Link className="btn btn--neutral" href={`/indexing${scope}`}>Open index runs</Link> } : selected ? undefined : repositories.length ? { tone: stale ? "warn" : "ok", text: stale ? `${stale} of ${repositories.length} projects are behind their code` : `All ${repositories.length} projects are up to date` } : undefined}
        actions={<>{detail && <Link className="btn btn--neutral" href="/projects">All projects</Link>}<Link className="btn btn--primary" href="/setup#repo">Add project</Link></>}
      />
      <SectionTabs section="projects" active="/projects" />
      {selected && !detail ? <Panel id="unknown-project" title="Project not found"><EmptyState title="That project is unavailable" body="Choose a connected project from the list. Brain did not fall back to another repository." /></Panel> : detail ? (
        <Panel id="project-detail" title="Index" desc={detail.path || "Repository path unavailable"} actions={<Link className="link" href={`/indexing${scope}`}>Index runs</Link>}>
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
        {repositories.length === 0 ? <EmptyState title="No projects connected" body="Connect a repository to give Brain code it can index and search." action={<Link className="btn btn--primary" href="/setup">Connect a project</Link>} /> : (
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
