"use client";

import Link from "next/link";
import { usePulse } from "./PulseProvider";
import { pulseDestination, pulseHeadline } from "@/lib/pulse-presentation";
import { utcTime } from "@/lib/time";
import { useRestrictedMember } from "./MemberScope";

export function ServerPulse() {
  const { pulse, enabled } = usePulse();
  const restricted = useRestrictedMember();
  return <>
    <dt>Version</dt><dd>{pulse?.version ? <><b>v{pulse.version}</b>{pulse.build && <code> {pulse.build}</code>}</> : enabled ? pulse ? "Not reported by the API" : "Checking…" : "Demo data — not connected to a Brain server"}</dd>
    {enabled && <>
      <dt id="server-status">Server</dt>
      <dd aria-live="polite">{pulse ? <>
        <b>{pulseHeadline(pulse)}</b>
        {pulse.latencyMs !== undefined && <span> · API round trip {pulse.latencyMs} ms</span>}
        <span className="sub">Observed {utcTime(new Date(pulse.at).toISOString())}</span>
        {pulse.workerHint && <p>{pulse.workerHint}</p>}
        {!!pulse.down.length && <p>Services needing attention: {pulse.down.join(", ")}</p>}
        {pulse.indexAgeWarning && !pulse.freshness?.length && <p>Brain reported an index-age warning. This reading does not identify a source in the selected project. Ask an operator to inspect the freshness SLO.</p>}
        {!!pulse.freshness?.length && <p>Index age exceeds the configured limit: {pulse.freshness.join(", ")}</p>}
        {!!pulse.warnings?.length && <p>Overdue scheduled jobs: {pulse.warnings.join(", ")}</p>}
        {!pulse.reachable && <p>The last probes received no answer. Check API connectivity and logs; a timeout does not establish that the server has stopped.</p>}
        {!pulse.healthy && pulseDestination(pulse, restricted) !== "/help#server-status" && <Link className="link" href={pulseDestination(pulse, restricted)}>Investigate</Link>}
      </> : "Checking…"}</dd>
      <dt>Models</dt><dd>{pulse?.llm || pulse?.embedding ? `Language model: ${pulse.llm ?? "not set"} · Embeddings: ${pulse.embedding ?? "not set"}` : "Not reported"}</dd>
    </>}
  </>;
}
