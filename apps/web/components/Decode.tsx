"use client";

import { useEffect, useRef } from "react";

const GLYPHS = "abcdefghijklmnopqrstuvwxyz0123456789/<>#_";

// Short labels resolve from random glyphs into the real word, left to right,
// the first time they come into view. The real text is in the DOM from the
// start (server render, screen readers, no-JS); only the painted glyphs churn.
export function Decode({ text, delay = 0 }: { text: string; delay?: number }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el || matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let raf = 0;
    let timer = 0;
    const run = () => {
      const start = performance.now();
      const span = 380 + text.length * 22;
      const tick = (now: number) => {
        const t = (now - start) / span;
        if (t >= 1) {
          el.textContent = text;
          return;
        }
        const fixed = Math.floor(t * text.length);
        let out = "";
        for (let i = 0; i < text.length; i++) {
          const ch = text[i];
          out += i < fixed || ch === " " ? ch : GLYPHS[(Math.random() * GLYPHS.length) | 0];
        }
        el.textContent = out;
        raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
    };
    // Waits for real visibility, so a label hidden behind the start-up screen
    // decodes when the screen appears rather than unseen.
    const io = new IntersectionObserver(([e]) => {
      if (!e.isIntersecting) return;
      io.disconnect();
      timer = window.setTimeout(run, delay);
    });
    io.observe(el);
    return () => {
      io.disconnect();
      clearTimeout(timer);
      cancelAnimationFrame(raf);
      el.textContent = text;
    };
  }, [text, delay]);

  return (
    <span ref={ref} className="decode">
      {text}
    </span>
  );
}
