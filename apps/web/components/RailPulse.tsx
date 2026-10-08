"use client";
// The rail footer as two or three short lines: whether Brain answers and how
// fast, how fresh its memory is, and — only while setup is unfinished — the
// way back to it. The build and models sit in the tooltip.

import Link from "next/link";
import { useEffect, useState } from "react";
import { getPulse, type Pulse } from "@/lib/actions/pulse";
import { pulseHeadline } from "@/lib/pulse-presentation";

const POLL_MS = 20_000;
// The last reading, so a reload shows it at once instead of "Checking…"
// while the first poll is in flight. Per tab; optional.
const MEMO = "pb:pulse";
const MEMO_MAX_AGE = 5 * 60_000;

function ago(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const m = Math.round((Date.now() - t) / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} h ago`;
  return `${Math.round(h / 24)} days ago`;
}

export function RailPulse({ withRepo, repoSlug }: { withRepo: (href: string) => string; repoSlug?: string }) {
  const [pulse, setPulse] = useState<Pulse | null>(null);

  useEffect(() => {
    setPulse(null);
    const memoKey = `${MEMO}:${repoSlug ?? "default"}`;
    let stop = false;
    let h: ReturnType<typeof setTimeout>;
    try {
      const memo = JSON.parse(sessionStorage.getItem(memoKey) ?? "null") as Pulse | null;
      if (memo && Date.now() - memo.at < MEMO_MAX_AGE) setPulse(memo);
    } catch {}
    const tick = async () => {
      if (document.visibilityState === "visible") {
        const p = await getPulse(repoSlug).catch(() => null);
        if (stop) return;
        if (p) {
          setPulse(p);
          try {
            sessionStorage.setItem(memoKey, JSON.stringify(p));
          } catch {}
        }
      }
      h = setTimeout(tick, POLL_MS);
    };
    tick();
    const wake = () => document.visibilityState === "visible" && (clearTimeout(h), tick());
    document.addEventListener("visibilitychange", wake);
    return () => {
      stop = true;
      clearTimeout(h);
      document.removeEventListener("visibilitychange", wake);
    };
  }, [repoSlug]);

  if (!pulse) {
    return (
      <div className="vitals" aria-busy="true">
        <p className="vitals__line vitals__line--head tone-idle">
          <span className="vitals__beat" aria-hidden="true" />
          Checking…
        </p>
      </div>
    );
  }

  const tone = !pulse.reachable ? "bad" : pulse.healthy ? "ok" : "warn";
  const headline = pulseHeadline(pulse);
  const warningOnly = pulse.reachable && !pulse.healthy && pulse.down.length === 0;
  const memory = !pulse.index
    ? "Never indexed"
    : pulse.index.status === "completed"
      ? `Indexed ${ago(pulse.index.at)}`
      : `Index ${pulse.index.status}`;
  const setupLeft = pulse.setup && pulse.setup.done < pulse.setup.total;
  const tip = [
    pulse.version ? `Brain v${pulse.version}${pulse.build ? ` (${pulse.build})` : ""}` : "",
    pulse.llm || pulse.embedding ? `Models: ${pulse.llm ?? "—"} / ${pulse.embedding ?? "—"}` : "",
    !pulse.reachable ? "Retrying every 20 s" : "",
    pulse.warnings?.length ? `Overdue jobs: ${pulse.warnings.join(", ")}` : warningOnly ? "Brain reported a warning; review scheduled jobs" : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className={`vitals tone-${tone}`}>
      <Link href={withRepo(warningOnly ? "/settings" : "/")} className="vitals__line vitals__line--head" title={tip || undefined}>
        <span className="vitals__beat" aria-hidden="true" />
        <span className="vitals__text">{headline}</span>
        {pulse.reachable && pulse.latencyMs !== undefined && <span className="vitals__ms num">{pulse.latencyMs} ms</span>}
      </Link>
      {pulse.reachable && repoSlug !== "" && (
        <Link href={withRepo("/indexing")} className="vitals__line" title="Indexing history">
          <span className="vitals__text">{memory}</span>
        </Link>
      )}
      {setupLeft && (
        <Link href="/setup" className="vitals__line vitals__line--next" title={`Next: ${pulse.setup!.next}`}>
          <span className="vitals__text">Finish setup</span>
          <span className="num">
            {pulse.setup!.done}/{pulse.setup!.total} →
          </span>
        </Link>
      )}
    </div>
  );
}
