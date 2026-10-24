import { capabilityGate } from "@/components/CapabilityGate";
import { SectionTabs } from "@/components/SectionNav";
import { TeamBoard, type BoardView } from "@/components/team/Board";
import { PageTabs, pickTab, type TabTone } from "@/components/PageTabs";
import { EmptyState, PageHead, Panel } from "@/components/ui";
import { DEMO_NOW } from "@/lib/board-demo";
import { getBoardView } from "@/lib/board-view";
import { claimMap, type ProjectBoard } from "@/lib/board-model";

export const metadata = { title: "Board" };

type Props = { searchParams: Promise<{ view?: string }> };
const VIEWS = ["plan", "paths", "queue"] as const satisfies readonly BoardView[];

// What each view holds, across every project shown, so a tab says it before it opens.
function viewBadges(boards: ProjectBoard[]) {
  // Tasks that do work: one that only groups subtasks shows through them (as on the plan).
  const working = boards.flatMap((b) => {
    const groups = new Set(b.tasks.map((t) => t.parentId).filter((id): id is string => id !== null));
    return b.tasks.filter((t) => t.column !== null && t.column !== "integrated" && !groups.has(t.id));
  });
  const stuck = working.filter((t) => t.column === "needs_rebase" || t.column === "stale_instructions").length;
  const claims = boards.flatMap((b) => claimMap(b));
  const shared = claims.filter((c) => c.overlap).length;
  const entries = boards.flatMap((b) => b.queue);
  const held = entries.filter((e) => e.status !== "queued").length;
  const tone = (problem: number, any: number): TabTone | undefined => (problem ? "warn" : any ? "info" : undefined);
  return [
    { id: "plan", label: "Plan", badge: stuck ? `${stuck} can’t land` : working.length, tone: tone(stuck, working.length) },
    { id: "paths", label: "Paths", badge: shared ? `${shared} shared` : claims.length, tone: tone(shared, claims.length) },
    { id: "queue", label: "Queue", badge: held ? `${held} held` : entries.length, tone: tone(held, entries.length) },
  ];
}

// The board opens on a sentence about what can't land and what is next; one
// view follows (the plan, the paths being changed or the queue), with what needs
// a person beside it.
export default async function Board({ searchParams }: Props) {
  const unsupported = await capabilityGate("team_board", "Board");
  if (unsupported) return unsupported;
  const [view, query] = await Promise.all([getBoardView(), searchParams]);
  const head = (eyebrow?: string, status?: Parameters<typeof PageHead>[0]["status"]) => (
    <>
      <SectionTabs section="team" active="/team/board" />
      <PageHead eyebrow={eyebrow} title="Board" status={status} />
    </>
  );

  if (view.kind === "solo") {
    return (
      <>
        {head()}
        <Panel id="board-solo" title="No team board" desc="This Brain has one owner.">
          <EmptyState title="The board starts with a team" body="Subtasks, leases and the integration queue coordinate several members working on one project. With one owner, tasks and runs work as before." />
        </Panel>
      </>
    );
  }
  if (view.kind === "unavailable") {
    return (
      <>
        {head()}
        <Panel id="board-unavailable" title="Board">
          <EmptyState title="Board unavailable" body="Brain didn’t say who you are just now, or this server predates team coordination. Reload in a moment." />
        </Panel>
      </>
    );
  }

  const all = view.projects;
  const tab = pickTab(VIEWS, query.view);
  const eyebrow = all.length === 1 ? all[0].name : all.length ? `${all.length} projects` : undefined;
  return (
    <>
      {head(eyebrow, view.demo ? { tone: "info", text: "Demo data: the dashboard isn’t connected to a Brain server. Controls are shown as a reviewer would see them." } : undefined)}
      {all.length ? (
        <>
          <PageTabs label="Board views" path="/team/board" current={tab} tabs={viewBadges(all)} />
          <div className="tb-projects">
            {all.map((p) => <TeamBoard key={p.id} board={p} selfId={view.selfId} live={!view.demo} clock={view.demo ? DEMO_NOW : null} named={all.length > 1} view={tab} />)}
          </div>
        </>
      ) : (
        <Panel id="board-empty" title="Board">
          <EmptyState title="No projects" body="You aren’t a member of any project yet. A team admin adds you to one on the Team page." />
        </Panel>
      )}
    </>
  );
}
