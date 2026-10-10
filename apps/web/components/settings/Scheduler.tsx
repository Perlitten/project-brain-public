"use client";
// Scheduled jobs (GET /scheduler/jobs): when each runs, how the last run went,
// and a "Run now" that queues the same POST /jobs/* a schedule slot uses.

import { RunButton } from "@/components/act";
import { Chip, Table } from "@/components/ui";
import { runScheduledJob } from "@/lib/actions/scheduler";
import { cronToText } from "@/lib/cron";
import type { ScheduleStatus, ScheduledJob, SchedulerView } from "@/lib/scheduler-view";
import type { Tone } from "@/lib/types";

const STATUS: Record<ScheduleStatus, [Tone, string]> = {
  ok: ["ok", "ok"],
  degraded: ["warn", "last run failed"],
  stale: ["bad", "overdue"],
  pending: ["idle", "not run yet"],
  disabled: ["idle", "scheduler off"],
};

export function SchedulerLine({ s }: { s: SchedulerView | null }) {
  if (!s) return <p className="field__hint">Shown once the server is updated (needs GET /scheduler/jobs).</p>;
  const stale = s.jobs.filter((j) => j.status === "stale").length;
  return (
    <div className="status-line">
      <Chip tone={!s.enabled ? "idle" : stale ? "bad" : "ok"}>{!s.enabled ? "Off" : stale ? `${stale} overdue` : "On"}</Chip>
      <span className="muted">
        Brain runs these jobs by itself. A job is overdue when it hasn’t succeeded for its interval plus {s.graceHours} h.
        {!s.enabled && " Scheduling is switched off on the server (SCHEDULER_ENABLED=false)."}
      </span>
    </div>
  );
}

function LastFailure({ j }: { j: ScheduledJob }) {
  if (!j.lastFailure) return <span className="muted">—</span>;
  if (!j.lastError) return <>{j.lastFailure}</>;
  return (
    <details className="wf">
      <summary>{j.lastFailure}</summary>
      <p className="mono">{j.lastError}</p>
    </details>
  );
}

export function SchedulerTable({ s }: { s: SchedulerView }) {
  return (
    <div className="sched-scroll">
    <Table
      caption="Scheduled jobs"
      rows={s.jobs}
      rowKey={(j) => j.jobType}
      columns={[
        {
          head: "Job",
          cell: (j) => (
            <>
              <span>{j.description}</span>
              <span className="sub mono">{j.jobType}{j.pool ? ` · ${j.pool} queue` : ""}</span>
            </>
          ),
        },
        { head: "Schedule", cell: (j) => <span title={`cron: ${j.cron}`}>{cronToText(j.cron)}</span> },
        { head: "Next run", cell: (j) => j.nextRun ?? "—" },
        { head: "Last success", cell: (j) => j.lastSuccess ?? <span className="muted">never</span> },
        { head: "Last failure", cell: (j) => <LastFailure j={j} /> },
        { head: "Tries used", cell: (j) => (j.attempts ? `${j.attempts}/${j.maxAttempts}` : `—/${j.maxAttempts}`), align: "right" },
        {
          head: "Status",
          cell: (j) => {
            const [tone, label] = STATUS[j.status] ?? (["idle", j.status] as [Tone, string]);
            return <Chip tone={tone}>{label}</Chip>;
          },
        },
        {
          head: "",
          cell: (j) => <RunButton action={() => runScheduledJob(j.jobType)} label="Run now" icon="play" small title={j.description} />,
        },
      ]}
    />
    </div>
  );
}
