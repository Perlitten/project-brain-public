import type { ReactNode } from "react";
import { Tooltip } from "./Tooltip";

// A small "?" that explains the thing next to it on hover, focus or tap —
// help that stays out of the way until asked for. Shared portalled tooltip with Term.
export function Tip({ children, label = "What is this?", end }: { children: ReactNode; label?: string; /** Opens leftward, for tips at a right edge. */ end?: boolean }) {
  return <Tooltip content={children} label={label} end={end} tip>?</Tooltip>;
}
