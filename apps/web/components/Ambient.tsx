"use client";

import { useEffect } from "react";

// Only the document lens is mutated. Writing inline styles onto streamed
// server-rendered surfaces before they hydrate causes attribute mismatches.

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
