"use client";

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

/** Portalled help stays outside panel/table clipping and follows its trigger. */
export function Tooltip({ content, children, label, end = false, tip = false }: {
  content: ReactNode; children: ReactNode; label?: string; end?: boolean; tip?: boolean;
}) {
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const bubble = useRef<HTMLSpanElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  const cancel = useCallback(() => { if (timer.current) clearTimeout(timer.current); }, []);
  const show = () => { cancel(); setOpen(true); };
  const leave = () => {
    cancel();
    timer.current = setTimeout(() => {
      const active = document.activeElement;
      if (active !== trigger.current && !bubble.current?.contains(active)) setOpen(false);
    }, 120);
  };
  useEffect(() => () => cancel(), [cancel]);
  useLayoutEffect(() => {
    if (!open) { setPosition(null); return; }
    const place = () => {
      const a = trigger.current?.getBoundingClientRect();
      const b = bubble.current?.getBoundingClientRect();
      if (!a || !b) return;
      const vw = document.documentElement.clientWidth;
      const vh = window.innerHeight;
      const left = Math.max(8, Math.min(end ? a.right - b.width : a.left + a.width / 2 - b.width / 2, vw - b.width - 8));
      const above = a.top - b.height - 8;
      const top = Math.max(8, Math.min(above >= 8 ? above : a.bottom + 8, vh - b.height - 8));
      setPosition({ left, top });
    };
    place();
    const observer = new ResizeObserver(place);
    if (bubble.current) observer.observe(bubble.current);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    const escape = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("keydown", escape, true);
    return () => {
      observer.disconnect(); window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true); document.removeEventListener("keydown", escape, true);
    };
  }, [open, end]);
  return <span className={`term${tip ? " tip" : ""}`} onMouseEnter={show} onMouseLeave={leave}>
    <button ref={trigger} type="button" className={`term__word${tip ? " tip__btn" : ""}`} aria-label={label}
      aria-describedby={open ? id : undefined} onFocus={show} onClick={show}
      onBlur={(e) => { if (!bubble.current?.contains(e.relatedTarget)) leave(); }}>{children}</button>
    {open && createPortal(<span id={id} ref={bubble} role="tooltip" className="term__tip"
      style={{ left: position?.left ?? 8, top: position?.top ?? 8, visibility: position ? "visible" : "hidden" }}
      onMouseEnter={show} onMouseLeave={leave} onFocus={show}
      onBlur={(e) => { if (!bubble.current?.contains(e.relatedTarget) && e.relatedTarget !== trigger.current) leave(); }}>{content}</span>, trigger.current?.closest("dialog[open]") ?? document.body)}
  </span>;
}
