import type { CSSProperties, ReactNode } from "react";
import { Term } from "@/components/Term";
import { Chip, EmptyState, PageHead } from "@/components/ui";
import { getInsights } from "@/lib/data";
import type { Insight } from "@/lib/types";

export const metadata = { title: "Findings" };

const kindWords: Record<Insight["kind"], ReactNode> = {
  drift: <Term k="drift">Structure drift</Term>,
  coupling: <Term k="coupling">Tangled modules</Term>,
  hotspot: <Term k="hotspot">Hotspot</Term>,
  freshness: "Outdated briefing",
  other: "Observation",
};

export default async function Insights() {
  const insights = await getInsights();
  const serious = insights.filter((i) => i.tone === "bad").length;
  return (
    <>
      <PageHead
        eyebrow="Findings"
        title={serious ? `${serious} serious problem${serious > 1 ? "s" : ""} to look at` : "Nothing serious found"}
        lede="Brain watches how your code changes over time and points out places that are getting harder to work with. Red means a rule your team set was broken."
      />
      {insights.length === 0 && (
        <EmptyState title="No findings yet" body="Brain hasn’t flagged anything in this code. Findings appear here as Brain notices risky changes." />
      )}
      <div className="cards">
        {insights.map((f, i) => (
          <article key={f.id} className={`card tone-${f.tone}`} style={{ "--i": i } as CSSProperties}>
            <div className="card__top">
              <Chip tone={f.tone}>{f.tone === "bad" ? "Rule broken" : f.tone === "warn" ? "Worth a look" : "FYI"}</Chip>
              <span className="text-dim">{kindWords[f.kind]}</span>
            </div>
            <h2 className="card__title">{f.title}</h2>
            <p className="card__body mono">{f.evidence}</p>
            <p className="card__meta">
              <span className="mono">{f.module}</span>
              <span>found {f.detectedAt}</span>
            </p>
          </article>
        ))}
      </div>
    </>
  );
}
