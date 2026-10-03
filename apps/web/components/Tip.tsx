import type { ReactNode } from "react";

// A small "?" that explains the thing next to it on hover, focus or tap —
// help that stays out of the way until asked for. Same pure-CSS tooltip as Term.
export function Tip({ children, label = "What is this?", end }: { children: ReactNode; label?: string; /** Opens leftward, for tips at a right edge. */ end?: boolean }) {
  return (
    <span className={`term tip${end ? " tip--end" : ""}`}>
      <button type="button" className="term__word tip__btn" aria-label={label}>
        ?
      </button>
      <span className="term__tip" role="tooltip">
        {children}
      </span>
    </span>
  );
}
