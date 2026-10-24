import { capabilityGate } from "@/components/CapabilityGate";
import Link from "next/link";
import { utcTime } from "@/lib/time";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import { getRepositoryFeed } from "@/lib/data";
import {
  ACTIVITY_KINDS, ACTIVITY_PAGE_SIZE, activityActors, activityFilters, activityHref, activityLabel, activityProjects, activityRepositories,
  getActivity, getMeView, type ActivityRow, type FilterOption,
} from "@/lib/team-activity-view";

export const metadata = { title: "Team activity" };

const LEDE = "Who changed what, newest first: decisions and rules, learnings, team membership and invitations, tasks, runs and queued jobs. You see the projects you can read; membership and invitations are shown to team admins only.";

const when = utcTime;

export default async function TeamActivity({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const unsupported = await capabilityGate("team_activity", "Team activity");
  if (unsupported) return unsupported;
  const filters = activityFilters(await searchParams);
  const [result, me] = await Promise.all([getActivity(filters), getMeView()]);
  const [projects, feed, actors] = await Promise.all([
    activityProjects(me),
    me.kind === "ok" && me.allRepositories ? getRepositoryFeed() : null,
    activityActors(me, result.kind === "ok" ? result.rows : []),
  ]);
  const repositories = activityRepositories(me, feed?.available ? feed.repositories.filter((r) => r.id > 0) : []);
  const projectName = new Map(projects.map((p) => [p.id, p.name]));
  const repositoryName = new Map(repositories.map((r) => [r.id, r.name]));
  const filtered = Boolean(filters.project || filters.repository || filters.actor || filters.kind || filters.since);

  if (result.kind === "denied") return <>
    <SectionTabs section="team" active="/team/activity" />
    <PageHead title="Team activity" lede={LEDE} />
    <Panel id="activity-access" title="Permission required">
      <EmptyState title="Team activity is not available to this credential" body={`${result.reason} Check the credential under Access. Adding a scope does not create team membership or grant access to another project.`} action={<Link className="btn btn--neutral" href="/team/me">Check my identity and access</Link>} />
      {filtered && <Link className="btn btn--neutral" href="/team/activity">Clear filters</Link>}
    </Panel>
  </>;

  return (
    <>
      <SectionTabs section="team" active="/team/activity" />
      <PageHead title="Team activity" lede={LEDE} />

      <Panel id="activity-filters" title="Filters">
        <form className="form form--wide" method="get" action="/team/activity">
          <div className="form__row">
            <label className="field">
              <span className="field__label">Project</span>
              <select className="select" name="project" defaultValue={filters.project ?? ""}>
                <option value="">All you can read</option>
                {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            <IdPicker name="repository" label="Repository" all="All projects" options={repositories} value={filters.repository} />
            <IdPicker name="actor" label="Who" all="Anyone" options={actors} value={filters.actor} manual={me.kind === "ok" && !me.teamAdmin && !me.allRepositories} />
            <label className="field">
              <span className="field__label">Kind</span>
              <select className="select" name="kind" defaultValue={filters.kind ?? ""}>
                <option value="">Everything</option>
                {Object.entries(ACTIVITY_KINDS).map(([family, kinds]) => (
                  <optgroup key={family} label={family}>
                    <option value={family}>All {family.replace("_", " ")} events</option>
                    {kinds.map((k) => <option key={k} value={k}>{activityLabel(k)}</option>)}
                  </optgroup>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field__label">Since <em>UTC</em></span>
              <input className="input" type="date" name="since" defaultValue={filters.since ?? ""} />
            </label>
          </div>
          <div className="btn-row">
            <button type="submit" className="btn btn--primary">Apply filters</button>
            {filtered && <Link className="btn btn--ghost" href="/team/activity">Clear</Link>}
          </div>
        </form>
      </Panel>

      <Panel id="activity-events" title="Events" desc="History of membership, roles and queued jobs starts with the release that added this page." flush>
        {result.kind === "ok" ? (
          result.rows.length ? (
            <>
              <Table<ActivityRow>
                rows={result.rows}
                rowKey={(r) => r.id}
                caption="Team activity, newest first"
                fill
                columns={[
                  { head: "When", cell: (r) => <span className="num">{when(r.occurredAt)}</span> },
                  { head: "Who", cell: (r) => r.actor },
                  { head: "Kind", cell: (r) => <span title={r.kind}><Chip tone="idle" dot={false}>{activityLabel(r.kind)}</Chip></span> },
                  { head: "What", cell: (r) => (r.detail ? <>{r.summary}<br /><span className="text-faint">{r.detail}</span></> : r.summary) },
                  { head: "Where", cell: (r) => (r.projectId ? projectName.get(r.projectId) ?? "a project" : r.repositoryId ? repositoryName.get(r.repositoryId) ?? `repository ${r.repositoryId}` : "global") },
                ]}
              />
              <nav className="btn-row" aria-label="Activity pages">
                {result.offset > 0 && <Link className="btn btn--neutral btn--sm" scroll={false} href={activityHref(filters, Math.max(0, result.offset - ACTIVITY_PAGE_SIZE))}>Newer</Link>}
                {result.hasMore && result.nextOffset !== null && <Link className="btn btn--neutral btn--sm" scroll={false} href={activityHref(filters, result.nextOffset)}>Older</Link>}
              </nav>
            </>
          ) : (
            <EmptyState title={filtered ? "Nothing matches" : "No activity yet"} body={filtered ? "No events match these filters." : "Changes to memory, membership, tasks and jobs will show up here."} />
          )
        ) : result.kind === "invalid" ? (
          <EmptyState title="Filters not accepted" body={result.reason} action={<Link className="btn btn--neutral" href="/team/activity">Clear filters</Link>} />
        ) : (
          <EmptyState title="Activity unavailable" body="Brain didn’t return team activity just now, or this server predates it. Team activity needs full storage." />
        )}
      </Panel>
    </>
  );
}

/** Pick by name; the value Brain filters on stays the numeric id. Without a list to offer, the id is typed. */
function IdPicker({ name, label, all, options, value, manual = false }: { name: string; label: string; all: string; options: FilterOption[]; value?: number; manual?: boolean }) {
  if (!options.length || manual) return (
    <label className="field">
      <span className="field__label">{label} <em>id</em></span>
      <input className="input input--mono" name={name} inputMode="numeric" pattern="[0-9]*" list={options.length ? `${name}-options` : undefined} placeholder={all} defaultValue={value ?? ""} />
      {options.length > 0 && <datalist id={`${name}-options`}>{options.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}</datalist>}
    </label>
  );
  const listed = value === undefined || options.some((o) => o.id === value);
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <select className="select" name={name} defaultValue={value ?? ""}>
        <option value="">{all}</option>
        {!listed && <option value={value}>#{value}</option>}
        {options.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
      </select>
    </label>
  );
}
