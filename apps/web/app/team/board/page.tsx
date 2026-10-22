import { capabilityGate } from "@/components/CapabilityGate";
import { SectionTabs } from "@/components/SectionNav";
import { BoardColumns, LeasesTable, OverlapList, QueueTable } from "@/components/team/Board";
import { EmptyState, PageHead, Panel, Stat, fmt } from "@/components/ui";
import { getBoardView, type ProjectBoard } from "@/lib/board-view";

export const metadata = { title: "Board" };

const LEDE =
  "Every task and subtask in the projects you belong to, by state: what waits on dependencies, what is running, what waits for integration and what needs a rebase or a fresh run. Leases, overlap warnings and the integration queue are live; the controls you see are the ones your role allows.";

function ProjectPanels({ board, selfId }: { board: ProjectBoard; selfId: number | null }) {
  const notes = [
    board.missing.length ? `Brain didn’t return ${board.missing.join(", ")}; the rest of the board is current.` : null,
    board.truncated ? "This project has more tasks than the board loads; the oldest are not shown." : null,
  ].filter(Boolean);
  const repos = board.repositories.map((r) => r.name).join(", ");
  return (
    <>
      <Panel id={`board-${board.id}`} title={board.name} desc={`Your role: ${board.role}${repos ? ` · ${repos}` : ""}`} flush>
        {notes.length > 0 && (
          <div className="board-notes">
            {notes.map((n) => <div key={n} className="callout tone-warn">{n}</div>)}
          </div>
        )}
        {board.tasks.some((t) => t.column !== null) ? (
          <BoardColumns board={board} selfId={selfId} />
        ) : (
          <EmptyState title="No tasks yet" body="Tasks and subtasks created for this project show up here, in a column by state." />
        )}
      </Panel>
      {board.overlaps.length > 0 && (
        <Panel id={`overlaps-${board.id}`} title="Overlap warnings" desc="Active tasks whose declared scope overlaps another task or a live lease. Warnings only: nothing is blocked." flush>
          <OverlapList board={board} selfId={selfId} />
        </Panel>
      )}
      <Panel id={`leases-${board.id}`} title="Live leases" desc="Who holds which path, until when, and the fencing token their writes must carry." flush>
        {board.leases.length ? <LeasesTable board={board} selfId={selfId} /> : <EmptyState title="No live leases" body="Nobody holds a path in this project right now." />}
      </Panel>
      <Panel id={`queue-${board.id}`} title="Integration queue" desc="Pending entries in the order a reviewer integrates them. An earlier queued entry blocks the ones behind it." flush>
        {board.queue.length ? <QueueTable board={board} selfId={selfId} /> : <EmptyState title="The queue is empty" body="Runs queued for integration show up here, oldest first." />}
      </Panel>
    </>
  );
}

export default async function Board() {
  const unsupported = await capabilityGate("team_board", "Board");
  if (unsupported) return unsupported;
  const view = await getBoardView();
  const head = (status?: Parameters<typeof PageHead>[0]["status"]) => (
    <>
      <PageHead title="Board" lede={LEDE} status={status} />
      <SectionTabs section="team" active="/team/board" />
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
  const count = (pred: (p: ProjectBoard) => number) => all.reduce((n, p) => n + pred(p), 0);
  const conflicts = count((p) => p.queue.filter((e) => e.status !== "queued").length + p.overlaps.length);
  return (
    <>
      {head(view.demo ? { tone: "info", text: "Demo data: the dashboard isn’t connected to a Brain server. Controls are shown as a reviewer would see them." } : undefined)}
      <div className="stats">
        <Stat label="Projects" value={fmt(all.length)} note="you are a member of" />
        <Stat label="Waiting for integration" value={fmt(count((p) => p.queue.length))} note="pending queue entries" />
        <Stat label="Conflicts" value={fmt(conflicts)} note="overlaps, rebases and stale instructions" />
        <Stat label="Live leases" value={fmt(count((p) => p.leases.length))} />
      </div>
      {all.length ? (
        all.map((p) => <ProjectPanels key={p.id} board={p} selfId={view.selfId} />)
      ) : (
        <Panel id="board-empty" title="Board">
          <EmptyState title="No projects" body="You aren’t a member of any project yet. A team admin adds you to one on the Team page." />
        </Panel>
      )}
    </>
  );
}
