"use client";

import { useState } from "react";
import { Icon } from "./Icon";

export function CopyCommand({ command }: { command: string }) {
  const [done, setDone] = useState(false);
  return (
    <div className="cmd">
      <code>{command}</code>
      <button
        type="button"
        className={`copy${done ? " is-done" : ""}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(command);
            setDone(true);
            setTimeout(() => setDone(false), 1600);
          } catch {
            setDone(false);
          }
        }}
        aria-label={done ? "Copied" : `Copy command: ${command}`}
      >
        {done ? <CheckMark /> : <Icon name="copy" size={14} />}
        {done ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

function CheckMark() {
  return (
    <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="4 12.5 9.5 18 20 6" pathLength={1} />
    </svg>
  );
}
