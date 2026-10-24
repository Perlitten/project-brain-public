"use client";
// The Result cell of an agent task: its status, and the one thing a person can
// do about it. A live task can be stopped; a task that ended badly links to
// what Brain recorded about it, since the task itself carries no reason.
import Link from "next/link";
import { RunButton } from "./act";
import { Chip, JobChip } from "./ui";
import { cancelTask } from "@/lib/actions/tasks";
import type { AgentRun } from "@/lib/types";

const LIVE = new Set(["running", "queued", "retrying"]);
const ENDED_BADLY = new Set(["failed", "error", "timed_out"]);

export function TaskResult({ run }: { run: AgentRun }) {
  const shortId = run.id.length > 12 ? run.id.slice(0, 8) : run.id;
  if (LIVE.has(run.status)) {
    if (run.cancelRequested) return <Chip tone="warn">stopping</Chip>;
    // Demo rows carry no version; the click previews, nothing is sent.
    const taskId = run.taskId ?? run.id;
    const version = run.version ?? 1;
    return (
      <span className="task-result">
        <JobChip status={run.status} />
        <RunButton
          small
          variant="danger"
          action={() => cancelTask(taskId, version)}
          label="Stop"
          title={`Stop “${run.task}”`}
          confirm={{
            title: `Stop “${run.task}”?`,
            body: `${run.agent} is told to stop at its next check-in. Work it already did stays where it is; the task is marked cancelled.`,
            yes: "Stop task",
          }}
        />
      </span>
    );
  }
  if (ENDED_BADLY.has(run.status)) {
    return (
      <span className="task-result">
        <JobChip status={run.status} />
        <Link className="link sub" href={`/activity?view=events&q=${encodeURIComponent(shortId)}`} title="What Brain recorded about this task">
          what happened
        </Link>
      </span>
    );
  }
  return <JobChip status={run.status} />;
}
