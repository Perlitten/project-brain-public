# ADR 013: In-process scheduler in the worker

## Status

Accepted — 2026-10-06. Supersedes [ADR 009](009-n8n-orchestration-layer.md).

## Context

n8n only did cron → `POST /jobs/*` → poll, plus an error workflow that queued
`self_diagnosis` and a webhook that turned a GitHub push into a reindex. All logic
already lived in the worker. n8n still needed its own container, volume,
encryption key, nginx vhost, Basic Auth and a systemd watchdog to stay alive,
and a dead n8n silently stopped every schedule.

## Decision

- `brain/workers/scheduler.py` runs inside every worker process. Schedules are
  code (same UTC cron times as the n8n exports); each tick enqueues due slots via
  `queue_for_job`, so jobs land in their pool.
- Exactly once per slot: Redis `SET NX` on `{prefix}:scheduler:slot:{job}:{slot}`.
  A missed slot fires once if within `SCHEDULER_CATCHUP_WINDOW_S`.
- Job layer: retries with exponential backoff up to `SCHEDULER_JOB_MAX_ATTEMPTS`;
  per job type `last_success_at` / `last_failure_at` / `last_error` in Redis;
  final failures enqueue `self_diagnosis` (`trigger.source=job_failure`).
- Visibility: `/health` is `degraded` when a job is overdue by more than
  `SCHEDULER_STALE_GRACE_S`; `GET /scheduler/jobs` (read-only) feeds the UI;
  optional `DEADMAN_URL_*` pings on success.
- Post-merge reindex: GitHub Actions calls `POST /webhooks/git-merge` on the API
  (token header, `refs/heads/master` only). `/jobs/*` stays for manual runs.
- `SCHEDULER_ENABLED=false` disables it.

## Relation to ADR 010

ADR 010 rejected an in-process scheduler because a process-local loop would
duplicate work across processes. The slot lock above is what makes that safe:
every worker evaluates every slot, exactly one wins the `SET NX`, and the job
itself goes through the same durable queue, leases and idempotency keys as a
manual `POST /jobs/*`.

## Consequences

- One less service, volume, vhost and watchdog; schedules ship and roll back with
  the code.
- Schedules can no longer be edited without a deploy (by design for now).
- With no worker running nothing fires — the same failure the n8n watchdog
  covered, now visible as `/health` → `degraded` and via dead-man pings.
