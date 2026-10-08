import type { Pulse } from "./actions/pulse";

export function pulseHeadline(pulse: Pick<Pulse, "reachable" | "healthy" | "down">): string {
  if (!pulse.reachable) return "Not answering";
  if (pulse.healthy) return "Healthy";
  return pulse.down.length ? `${pulse.down.join(", ")} down` : "Needs attention";
}
