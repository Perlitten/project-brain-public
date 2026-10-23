"use client";
import Link from "next/link";
import { useCapability } from "./Capabilities";
export function TraceLink({ id, repositoryId }: { id: string; repositoryId?: number }) {
  const supported = useCapability("request_traces");
  return supported ? <Link className="link mono" title={id} href={`/activity/requests/${encodeURIComponent(id)}${repositoryId ? `?repo=${repositoryId}` : ""}`}>{id.slice(0, 12)}</Link> : <code title={`${id} · Update the API to open request traces`}>{id.slice(0, 12)}</code>;
}
