import { capabilityGate } from "@/components/CapabilityGate";
import { SectionTabs } from "@/components/SectionNav";
import { TeamBoard } from "@/components/team/Board";
import { EmptyState, PageHead, Panel } from "@/components/ui";
import { DEMO_NOW } from "@/lib/board-demo";
import { getBoardView } from "@/lib/board-view";

export const metadata = { title: "Board" };

// The board opens on a sentence about what can't land and what is next; the
// plan, the paths being changed and the queue follow, with what needs a person
// beside them.
export default async function Board() {
  const unsupported = await capabilityGate("team_board", "Board");
  if (unsupported) return unsupported;
  const view = await getBoardView();
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
  const eyebrow = all.length === 1 ? all[0].name : all.length ? `${all.length} projects` : undefined;
  return (
    <>
      {head(eyebrow, view.demo ? { tone: "info", text: "Demo data: the dashboard isn’t connected to a Brain server. Controls are shown as a reviewer would see them." } : undefined)}
      {all.length ? (
        <div className="tb-projects">
          {all.map((p) => <TeamBoard key={p.id} board={p} selfId={view.selfId} live={!view.demo} clock={view.demo ? DEMO_NOW : null} named={all.length > 1} />)}
        </div>
      ) : (
        <Panel id="board-empty" title="Board">
          <EmptyState title="No projects" body="You aren’t a member of any project yet. A team admin adds you to one on the Team page." />
        </Panel>
      )}
    </>
  );
}
