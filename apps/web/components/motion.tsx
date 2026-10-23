"use client";

import { Fragment, useEffect, useRef, useState, type CSSProperties } from "react";

// Render the actual number on the first frame. On updates, briefly highlight
// the new value rather than spinning through unrelated digits.
export function Odometer({ value, className = "" }: { value: string; className?: string }) {
  const previous = useRef(value);
  const [changed, setChanged] = useState(false);
  useEffect(() => {
    if (previous.current === value) return;
    previous.current = value;
    setChanged(true);
    const timer = setTimeout(() => setChanged(false), 320);
    return () => clearTimeout(timer);
  }, [value]);
  return <span className={`odo ${className}`}><span key={value} className={changed ? "odo__value--changed" : undefined}>{value}</span></span>;
}

// A headline that rises word by word out of a mask.
export function Rise({ text, delay = 0 }: { text: string; delay?: number }) {
  const words = text.split(" ");
  return (
    <span className="rise" aria-label={text} role="text">
      {words.map((w, i) => (
        <Fragment key={i}>
          <span className="rise__mask" aria-hidden="true">
            <span className="rise__word" style={{ "--w": i, "--rd": `${delay}ms` } as CSSProperties}>
              {w}
            </span>
          </span>
          {i < words.length - 1 ? " " : ""}
        </Fragment>
      ))}
    </span>
  );
}
