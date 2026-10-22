"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { getPulse, type Pulse } from "@/lib/actions/pulse";

const PulseContext = createContext<{ pulse: Pulse | null; enabled: boolean }>({ pulse: null, enabled: false });
export const usePulse = () => useContext(PulseContext);

/** One poller for the shell and every status consumer, including Help.
 * Readings stay in this signed-in shell, never in browser storage across users.
 */
export function PulseProvider({ enabled, repoSlug, children }: { enabled: boolean; repoSlug?: string; children: ReactNode }) {
  const [reading, setReading] = useState<{ scope: string | undefined; pulse: Pulse } | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let pending = false;
    let misses = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (stopped || pending || document.visibilityState !== "visible") return;
      pending = true;
      const pulse = await getPulse(repoSlug).catch(() => ({ at: Date.now(), reachable: false, down: [] } as Pulse));
      pending = false;
      if (stopped) return;
      misses = pulse && !pulse.reachable ? misses + 1 : 0;
      // A single missed probe does not replace a successful reading with an outage.
      if (pulse && (pulse.reachable || misses >= 2)) setReading({ scope: repoSlug, pulse });
      timer = setTimeout(tick, misses === 1 ? 3_000 : 20_000);
    };
    const wake = () => {
      if (document.visibilityState === "visible") { clearTimeout(timer); void tick(); }
    };
    void tick();
    document.addEventListener("visibilitychange", wake);
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener("visibilitychange", wake); };
  }, [enabled, repoSlug]);
  const pulse = enabled && reading?.scope === repoSlug ? reading?.pulse ?? null : null;
  return <PulseContext.Provider value={{ pulse, enabled }}>{children}</PulseContext.Provider>;
}
