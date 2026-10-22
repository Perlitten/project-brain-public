import type { Pulse } from "./actions/pulse";

export function pulseHeadline(pulse: Pick<Pulse, "reachable" | "healthy" | "down">): string {
  if (!pulse.reachable) return "Not answering";
  if (pulse.healthy) return "Healthy";
  return pulse.down.length ? `${pulse.down.join(", ")} down` : "Needs attention";
}

/** Every destination names or displays the condition behind the status. */
export function pulseDestination(pulse: Pulse, restricted = false): string {
  if (pulse.reachable && !pulse.down.length && pulse.freshness?.length) return "/projects";
  if (!restricted && pulse.reachable && !pulse.down.length && pulse.warnings?.length) return "/settings#scheduler";
  return "/help#server-status";
}
