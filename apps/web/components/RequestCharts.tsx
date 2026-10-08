import type { CSSProperties } from "react";
import { EmptyState } from "./ui";
import type { RequestBucket } from "@/lib/dashboard";
import { measuredNumber } from "@/lib/dashboard";

const date = (at: string) => new Date(at).toLocaleString("en-GB", { timeZone: "UTC", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

export function RequestChart({ buckets, kind, available = true }: { buckets: RequestBucket[]; kind: "requests" | "latency"; available?: boolean }) {
  const values = buckets.map((b) => kind === "requests" ? b.total : b.p95_ms);
  const observed = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (!observed.length) return <EmptyState title={!available ? "Measurements unavailable" : kind === "requests" ? "No measured request history yet" : "No latency samples yet"} body={!available ? "This server did not return request measurements. Check the API connection and telemetry service." : "Charts appear as Brain records search and context requests. Earlier periods remain unmeasured."} />;
  const max = Math.max(1, ...observed);
  return <>
    <div className="request-chart" role="img" aria-label={kind === "requests" ? "Requests per interval; exact values follow in the data table" : "95th percentile response time per interval; exact values follow in the data table"}>
      <span className="request-chart__max num">{measuredNumber(Math.max(...observed), kind === "latency" ? " ms" : "")}</span>
      <div className="request-chart__bars">
        {buckets.map((b, i) => <div key={b.at} className={`request-chart__column${values[i] === null ? " request-chart__column--unknown" : ""}`} title={`${date(b.at)} UTC: ${measuredNumber(values[i], kind === "latency" ? " ms" : " requests")}${values[i] === null ? " (not measured)" : b.partial ? " (partial interval)" : ""}`}>
          {values[i] !== null && <span className={`request-chart__bar request-chart__bar--${kind}`} style={{ "--bar-height": `${Math.max(values[i]! > 0 ? 2 : 0, values[i]! / max * 100)}%` } as CSSProperties} />}
          {kind === "requests" && b.failed !== null && b.failed > 0 && <span className="request-chart__errors" style={{ "--bar-height": `${b.failed / max * 100}%` } as CSSProperties} />}
        </div>)}
      </div>
    </div>
    <div className="request-chart__axis"><span>{date(buckets[0].at)}</span><span>{date(buckets.at(-1)!.at)} UTC</span></div>
    <details className="request-chart__data"><summary>View exact measurements</summary><div className="request-chart__table"><table><caption>Intervals in UTC. A dash means not measured.</caption><thead><tr><th>Interval</th><th>Requests</th><th>Failed</th><th>p95, ms</th></tr></thead><tbody>{buckets.map((b) => <tr key={b.at}><th>{date(b.at)}</th><td>{measuredNumber(b.total)}</td><td>{measuredNumber(b.failed)}</td><td>{measuredNumber(b.p95_ms)}</td></tr>)}</tbody></table></div></details>
  </>;
}
