import Link from "next/link";
import { SectionNav } from "@/components/SectionNav";
import { Chip, EmptyState, Meter, PageHead, Panel } from "@/components/ui";
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
  return (
    <>
      <PageHead eyebrow="Projects" title={detail?.name ?? "Projects"} lede="Connect a repository, check its freshness, and inspect what Brain remembers." actions={<><Link className="btn btn--neutral" href="/projects">All projects</Link><Link className="btn btn--primary" href="/setup">Add project</Link></>} />
      <SectionNav label="Project views" items={[{ href: "/projects" + (detail ? `?repo=${detail.slug}` : ""), label: detail ? "Project overview" : "Projects", active: true }, ...(detail ? [{ href: `/indexing${scope}`, label: "Indexing" }, { href: `/graph${scope}`, label: "Code map" }] : []), { href: `/setup${scope}`, label: "Connection" }]} />
      {selected && !detail ? <Panel id="unknown-project" title="Project not found"><EmptyState title="That project is unavailable" body="Choose a connected project from the list. Brain did not fall back to another repository." /></Panel> : detail ? (
        <Panel id="project-detail" title={detail.name} desc={detail.path || "Repository path unavailable"}>
          <div className="stats">
            <div className="stat"><b>Freshness</b><span className="sub">{repositoryStatus(detail).status}</span></div>
            <div className="stat"><b>Indexed pieces</b><span className="sub">{corpus?.available === false ? "— unavailable" : corpus ? corpus.chunks.toLocaleString("en-US") : "—"}</span></div>
            <div className="stat"><b>Latest revision</b><span className="sub mono">{detail.head || "unknown"}</span></div>
            <div className="stat"><b>Latest index run</b><span className="sub">{history?.available === false ? "History unavailable" : history?.items[0] ? `${history.items[0].startedAt} · ${history.items[0].status}` : "None recorded"}</span></div>
          </div>
          {corpus?.meters.length ? <div className="meters">{corpus.meters.map((m) => <Meter key={m.label} meter={m} />)}</div> : <p className="sub">Coverage unavailable</p>}
          <p className="text-dim">Coverage describes indexed code. It does not measure retrieval relevance or agent task success.</p>
          <p><Link className="link" href={`/indexing${scope}`}>Open indexing history</Link> · <Link className="link" href={`/graph${scope}`}>Open code map</Link></p>
        </Panel>
      ) : <Panel id="projects" title="Connected repositories" flush>
        {repositories.length === 0 ? <EmptyState title="No projects connected" body="Connect a repository to give Brain code it can index and search." /> : (
          <ul className="ledger">
            {repositories.map((repo) => (
              <li key={repo.slug} className="ledger__row">
                <span className="ledger__time mono">#{repo.slug}</span>
                <span className="job-row__main"><Link className="link" href={`/projects?repo=${encodeURIComponent(repo.slug)}`}>{repo.name}</Link><span className="sub mono">{repo.path || "path unavailable"} · {repo.branch || "branch unknown"} · {repo.head || "revision unknown"}</span></span>
                <Chip tone={repositoryStatus(repo).tone}>{repositoryStatus(repo).status}</Chip>
              </li>
            ))}
          </ul>
        )}
      </Panel>}
    </>
  );
}
