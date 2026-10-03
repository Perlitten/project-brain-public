"use client";

import { useEffect } from "react";

// One pointer listener for the whole app. It feeds two CSS effects:
// --cx/--cy on <html> move the lens that lights up the blueprint grid behind
// the page, and --mx/--my on the surface under the pointer place its border
// spotlight. Writes are batched into one frame; touch input is ignored.
const LIT = ".panel, .verdict, .map, .card, .step, .palette__box";

export function Ambient() {
  useEffect(() => {
    const root = document.documentElement;
    let frame = 0;
    let last: PointerEvent | null = null;
    const paint = () => {
      frame = 0;
      const e = last;
      if (!e) return;
      root.style.setProperty("--cx", `${e.clientX}px`);
      root.style.setProperty("--cy", `${e.clientY}px`);
      const el = (e.target as Element | null)?.closest?.<HTMLElement>(LIT);
      if (el) {
        const r = el.getBoundingClientRect();
        el.style.setProperty("--mx", `${e.clientX - r.left}px`);
        el.style.setProperty("--my", `${e.clientY - r.top}px`);
      }
    };
    const onMove = (e: PointerEvent) => {
      if (e.pointerType === "touch") return;
      last = e;
      root.dataset.pointer = "";
      if (!frame) frame = requestAnimationFrame(paint);
    };
    const onLeave = () => delete root.dataset.pointer;
    window.addEventListener("pointermove", onMove, { passive: true });
    document.addEventListener("pointerleave", onLeave);
    return () => {
      window.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerleave", onLeave);
      cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <div className="ambient" aria-hidden="true">
      <div className="ambient__grid" />
      <div className="ambient__lens" />
    </div>
  );
}
