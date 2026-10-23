import type { Insight } from "./types";
export interface FindingGroup { rep: Insight; count: number; members: Insight[] }
/** Same title in different modules is not evidence of a duplicate. Group only
 * this loaded page; the server's dedupe identity and recurrence count persist.
 */
export function groupFindings(findings: Insight[]): FindingGroup[] {
  const rank: Record<string, number> = { bad: 0, warn: 1, info: 2, ok: 3, idle: 4 };
  const groups = new Map<string, FindingGroup>();
  for (const finding of findings) {
    const key = finding.dedupeKey ?? `${finding.kind}\u0000${finding.title}\u0000${finding.module}\u0000${finding.evidence}`;
    const existing = groups.get(key);
    const count = finding.occurrences && finding.occurrences > 0 ? finding.occurrences : 1;
    if (!existing) groups.set(key, { rep: finding, count, members: [finding] });
    else {
      existing.count += count; existing.members.push(finding);
      if ((rank[finding.tone] ?? 9) < (rank[existing.rep.tone] ?? 9)) existing.rep = finding;
    }
  }
  return [...groups.values()];
}
