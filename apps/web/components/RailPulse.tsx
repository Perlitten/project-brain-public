"use client";
// The rail footer as two or three short lines: whether Brain answers and how
// fast, how fresh its memory is, and — only while setup is unfinished — the
// way back to it. The build and models sit in the tooltip.

import Link from "next/link";
import { usePulse } from "./PulseProvider";
import { pulseDestination, pulseHeadline } from "@/lib/pulse-presentation";
import { stepTitle } from "@/lib/setup-copy";
import { uncommittedMessage } from "@/lib/repository-status";
import { useRestrictedMember } from "./MemberScope";

function ago(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const m = Math.round((Date.now() - t) / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

export function RailPulse({ withRepo, repoSlug }: { withRepo: (href: string) => string; repoSlug?: string }) {
  const restricted = useRestrictedMember();
  const { pulse } = usePulse();

  if (!pulse) {
    return (
      <div className="vitals" aria-busy="true">
        <p className="vitals__line vitals__line--head tone-idle">
          <span className="vitals__beat" aria-hidden="true" />
          Checking…
        </p>
      </div>
    );
  }

  const tone = !pulse.reachable ? "bad" : pulse.healthy ? "ok" : "warn";
  const headline = pulseHeadline(pulse);
  const warningOnly = pulse.reachable && !pulse.healthy && pulse.down.length === 0;
  const memory = !pulse.index
    ? "Never indexed"
    : pulse.index.status === "completed"
      ? `Indexed ${ago(pulse.index.at)}`
      : `Index ${pulse.index.status}`;
  const setupLeft = pulse.setup && pulse.setup.done < pulse.setup.total;
  const omitted = pulse.index?.uncommitted;
  const omittedNote = omitted !== undefined && omitted > 0 ? uncommittedMessage(omitted) : "";
  const tip = [
    pulse.latencyMs !== undefined ? `API round trip: ${pulse.latencyMs} ms` : "",
    pulse.version ? `Brain v${pulse.version}${pulse.build ? ` (${pulse.build})` : ""}` : "",
    pulse.llm || pulse.embedding ? `Models: ${pulse.llm ?? "—"} / ${pulse.embedding ?? "—"}` : "",
    !pulse.reachable ? "Retrying every 20 s" : "",
    pulse.workerHint ?? "",
    pulse.freshness?.length ? `Index age exceeds the configured limit: ${pulse.freshness.join(", ")}` : pulse.indexAgeWarning ? "Brain reported an index-age warning; details are in Help" : "",
    pulse.warnings?.length ? `Overdue jobs: ${pulse.warnings.join(", ")}` : warningOnly ? "Brain reported a warning; review scheduled jobs" : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className={`vitals tone-${tone}`}>
      <Link href={pulseDestination(pulse, restricted)} className="vitals__line vitals__line--head" title={tip || undefined}>
        <span className="vitals__beat" aria-hidden="true" />
        <span className="vitals__text">{headline}</span>
      </Link>
      {pulse.reachable && repoSlug !== "" && (
        <Link href={withRepo("/indexing")} className={`vitals__line${omittedNote ? " vitals__line--warning tone-warn" : ""}`} title={omittedNote ? `${memory} · Recorded at the latest index run, not a live checkout count` : "Indexing history"}>
          <span className="vitals__text">{omittedNote || memory}</span>
        </Link>
      )}
      {setupLeft && (
        <Link href="/setup" className="vitals__line vitals__line--next" title={`Next: ${stepTitle(pulse.setup!.nextId ?? "", pulse.setup!.next ?? "")}`}>
          <span className="vitals__text">Finish setup</span>
          <span className="num">
            {pulse.setup!.done}/{pulse.setup!.total} →
          </span>
        </Link>
      )}
    </div>
  );
}
