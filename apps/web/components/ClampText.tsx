"use client";
// Card bodies run from one line to forty. The card grid stretches every card
// in a row to the tallest one, so a single long summary used to set the height
// of its whole row — 700px cards next to 200px ones. Bodies are clamped to a
// few lines here, and only the ones actually cut off offer to expand.

import { useEffect, useRef, useState, type CSSProperties } from "react";

export function ClampText({
  text,
  lines = 4,
  className = "card__body",
  label = "summary",
}: {
  text: string;
  /** Lines shown before clamping. */
  lines?: number;
  className?: string;
  /** Named in the toggle's accessible label, e.g. "Show more of this summary". */
  label?: string;
}) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [clipped, setClipped] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    // Measure only while clamped: once expanded the element no longer
    // overflows, which would otherwise hide the control that collapses it.
    if (open) return;
    const el = ref.current;
    if (!el) return;
    const measure = () => setClipped(el.scrollHeight - el.clientHeight > 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [text, open]);

  return (
    <>
      <p
        ref={ref}
        className={`${className}${open ? "" : " clamp"}`}
        style={{ "--clamp-lines": lines } as CSSProperties}
        // The full text stays reachable without expanding, for pointer users.
        title={open ? undefined : text}
      >
        {text}
      </p>
      {clipped && (
        <button type="button" className="clamp-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Show less" : "Show more"}
          <span className="sr-only"> of this {label}</span>
        </button>
      )}
    </>
  );
}
