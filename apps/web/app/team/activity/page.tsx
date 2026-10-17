import Link from "next/link";
import { SectionTabs } from "@/components/SectionNav";
import { Chip, EmptyState, PageHead, Panel, Table } from "@/components/ui";
import {
  ACTIVITY_KINDS, ACTIVITY_PAGE_SIZE, activityFilters, activityHref, activityProjects, getActivity, getMeView, type ActivityRow,
} from "@/lib/team-activity-view";

export const metadata = { title: "Team activity" };

const LEDE = "Who changed what, newest first: decisions and rules, learnings, team membership and invitations, tasks, runs and queued jobs. You see the projects you can read; membership and invitations are shown to team admins only.";

const when = (iso: string | null) => (iso ? iso.replace("T", " ").slice(0, 16) + " UTC" : "unknown time");

export default async function TeamActivity({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const filters = activityFilters(await searchParams);
  const [result, me] = await Promise.all([getActivity(filters), getMeView()]);
  const projects = await activityProjects(me);
  const projectName = new Map(projects.map((p) => [p.id, p.name]));
  const filtered = Boolean(filters.project || filters.repository || filters.actor || filters.kind || filters.since);

  return (
    <>
      <PageHead title="Team activity" lede={LEDE} />
      <SectionTabs section="settings" active="/team/activity" />

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
            <label className="field">
              <span className="field__label">Repository <em>id</em></span>
              <input className="input input--mono" name="repository" inputMode="numeric" pattern="[0-9]*" defaultValue={filters.repository ?? ""} />
            </label>
            <label className="field">
              <span className="field__label">Actor <em>principal id</em></span>
              <input className="input input--mono" name="actor" inputMode="numeric" pattern="[0-9]*" defaultValue={filters.actor ?? ""} />
            </label>
            <label className="field">
              <span className="field__label">Kind</span>
              <select className="select" name="kind" defaultValue={filters.kind ?? ""}>
                <option value="">Everything</option>
                {Object.entries(ACTIVITY_KINDS).map(([family, kinds]) => (
                  <optgroup key={family} label={family}>
                    <option value={family}>All {family.replace("_", " ")} events</option>
                    {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
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
                columns={[
                  { head: "When", cell: (r) => <span className="num">{when(r.occurredAt)}</span> },
                  { head: "Who", cell: (r) => r.actor },
                  { head: "Kind", cell: (r) => <Chip tone="idle" dot={false}>{r.kind}</Chip> },
                  { head: "What", cell: (r) => r.summary },
                  { head: "Where", cell: (r) => (r.projectId ? projectName.get(r.projectId) ?? "a project" : r.repositoryId ? `repository ${r.repositoryId}` : "global") },
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
        ) : result.kind === "denied" || result.kind === "invalid" ? (
          <EmptyState title={result.kind === "denied" ? "Not allowed" : "Filters not accepted"} body={result.reason} action={<Link className="btn btn--neutral" href="/team/activity">Clear filters</Link>} />
        ) : (
          <EmptyState title="Activity unavailable" body="Brain didn’t return team activity just now, or this server predates it. Team activity needs full storage." />
        )}
      </Panel>
    </>
  );
}
