// The atlas: the shared grammar of the Dashboard and Memory screens. Memory is
// drawn as a place (regions per project, tiles per module or record) and the
// screen opens with one sentence that says what matters. Everything here is
// pure, so the sentences and tile rules are testable without React.
import type { CodeModule, Decision, Rule } from "./types";

/** ok: current/agreed · chg: changed or proposed · miss: not read or replaced. */
export type TileState = "ok" | "chg" | "miss";

/** A verdict sentence: plain text, a highlighted number, or a mention that points at a region or tile. */
export type Segment = string | { num: string; tone: "warn" | "bad" | "ok" } | { ref: string; label: string };

const WORDS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
/** Numbers up to ten read as words in a sentence, larger ones as digits. */
export function numberWord(n: number): string {
  return Number.isInteger(n) && n >= 0 && n <= 10 ? WORDS[n] : n.toLocaleString("en-US");
}
export const plural = (n: number, one: string, many = `${one}s`) => (n === 1 ? one : many);
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------------------------------------------------------------------------
// Dashboard: projects as regions, code modules as tiles.

export interface ModuleTile {
  key: string;
  name: string;
  state: TileState;
  span: 2 | 3;
  chunks: number;
  changed: number;
  unread: number;
  /** Set on the closing tile that stands for modules not drawn one by one. */
  more?: number;
}

export interface ModuleTotals { current: number; outdated: number; missing: number; excluded: number }

/** A module is changed when most of its stale code changed, unread when most of it was never read. */
export function moduleState(m: Pick<CodeModule, "outdated" | "missing">): TileState {
  if (m.outdated + m.missing === 0) return "ok";
  return m.outdated >= m.missing ? "chg" : "miss";
}

export function moduleTotals(modules: CodeModule[]): ModuleTotals {
  return modules.reduce((t, m) => ({ current: t.current + m.current, outdated: t.outdated + m.outdated, missing: t.missing + m.missing, excluded: t.excluded + m.excluded }), { current: 0, outdated: 0, missing: 0, excluded: 0 });
}

/**
 * At most `max` tiles per project: modules that need reading first, then the
 * biggest. What does not fit folds into one closing tile, so the region still
 * accounts for all of the project's code.
 */
export function moduleTiles(modules: CodeModule[], max = 8): ModuleTile[] {
  const drawable = modules.filter((m) => m.current + m.outdated + m.missing > 0);
  const ranked = [...drawable].sort((a, b) => (b.outdated + b.missing) - (a.outdated + a.missing) || b.chunks - a.chunks || a.name.localeCompare(b.name));
  const shown = ranked.length > max ? ranked.slice(0, max - 1) : ranked;
  const rest = ranked.slice(shown.length);
  const big = [...shown].map((m) => m.chunks).sort((a, b) => b - a)[Math.max(0, Math.ceil(shown.length / 3) - 1)] ?? 0;
  const tiles: ModuleTile[] = shown.map((m) => ({ key: m.name, name: m.name, state: moduleState(m), span: m.chunks >= big && big > 0 ? 3 : 2, chunks: m.chunks, changed: m.outdated, unread: m.missing }));
  if (rest.length) {
    const sum = moduleTotals(rest);
    tiles.push({ key: "__more", name: `${rest.length} more`, state: moduleState(sum), span: 2, chunks: sum.current + sum.outdated + sum.missing, changed: sum.outdated, unread: sum.missing, more: rest.length });
  }
  return tiles;
}

/** What a tile says under its name: the problem when there is one, else its size. */
export function moduleNote(t: Pick<ModuleTile, "changed" | "unread" | "chunks">): string {
  if (t.changed && t.changed >= t.unread) return `${t.changed.toLocaleString("en-US")} changed`;
  if (t.unread) return `${t.unread.toLocaleString("en-US")} not read`;
  return t.chunks.toLocaleString("en-US");
}

export interface VerdictProject { slug: string; name: string; behind: number | null; freshness?: string; ok: boolean; status: string }

/**
 * The Dashboard's opening sentence, worst project first:
 * "billing-service is 41 changes behind its code. project-brain, 3. The other two are current."
 */
export function todayVerdict(projects: VerdictProject[]): Segment[] {
  if (!projects.length) return [];
  const mention = (p: VerdictProject): Segment => ({ ref: p.slug, label: p.name });
  const missing = projects.filter((p) => p.freshness === "source_missing");
  const behind = projects.filter((p) => p.freshness !== "source_missing" && (p.behind ?? 0) > 0).sort((a, b) => (b.behind ?? 0) - (a.behind ?? 0));
  const other = projects.filter((p) => !p.ok && !missing.includes(p) && !behind.includes(p));
  const out: Segment[] = [];
  const say = (...parts: Segment[]) => { if (out.length) out.push(" "); out.push(...parts); };
  behind.slice(0, 2).forEach((p, i) => {
    const n = p.behind ?? 0;
    const tone = n > 20 ? "bad" : "warn";
    if (i === 0) say(mention(p), " is ", { num: n.toLocaleString("en-US"), tone }, ` ${plural(n, "change")} behind its code.`);
    else say(mention(p), ", ", { num: n.toLocaleString("en-US"), tone }, ".");
  });
  if (behind.length > 2) say(`${cap(numberWord(behind.length - 2))} more ${plural(behind.length - 2, "is", "are")} behind.`);
  if (missing.length) {
    const list: Segment[] = missing.slice(0, 3).flatMap((p, i) => (i ? [", ", mention(p)] : [mention(p)]));
    say("Brain can't read the source of ", ...list, missing.length > 3 ? ` and ${numberWord(missing.length - 3)} more.` : ".");
  }
  other.slice(0, 2).forEach((p) => say(mention(p), `: ${p.status.charAt(0).toLowerCase()}${p.status.slice(1)}.`));
  if (other.length > 2) say(`${cap(numberWord(other.length - 2))} more need a freshness check.`);
  const fine = projects.length - behind.length - missing.length - other.length;
  if (!out.length) return projects.length === 1 ? [mention(projects[0]), " is current with its code."] : [`All ${numberWord(projects.length)} projects are current with their code.`];
  if (fine === 1) say("The other one is current.");
  else if (fine > 1) say(`The other ${numberWord(fine)} are current.`);
  return out;
}

// ---------------------------------------------------------------------------
// Memory: projects as regions, decisions and rules as tiles.

/** Key of the region for memory recorded for no single project. */
export const EVERY_PROJECT = "__every";

const norm = (p: string) => p.trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();

/**
 * Which project a stored record belongs to. Brain stores the repository path;
 * global memory has none (or the "system_global" marker) and lands in the
 * shared region. Demo rows carry the slug directly.
 */
export function regionFor(repoPath: string | null | undefined, demoRepo: string | undefined, repos: { slug: string; name: string; path: string }[]): string {
  if (demoRepo) return repos.some((r) => r.slug === demoRepo) ? demoRepo : EVERY_PROJECT;
  if (!repoPath || repoPath === "system_global") return EVERY_PROJECT;
  const path = norm(repoPath);
  const exact = repos.find((r) => r.path && norm(r.path) === path);
  if (exact) return exact.slug;
  const last = path.split("/").filter(Boolean).pop() ?? "";
  return repos.find((r) => r.slug.toLowerCase() === last || r.name.toLowerCase() === last)?.slug ?? EVERY_PROJECT;
}

export function decisionState(status: Decision["status"]): TileState {
  return status === "accepted" ? "ok" : status === "superseded" ? "miss" : "chg";
}

/** Rules that reach agents often get the wider tile; the rest are equal. */
export function ruleSpan(delivered: number | undefined, most: number): 2 | 3 {
  return delivered !== undefined && most > 0 && delivered >= most * 0.66 ? 3 : 2;
}

/** A rule caught broken in at least a quarter of the tasks it reached, given enough tasks to say so. */
export function worstRule(rules: Rule[]): Rule | null {
  const ratio = (r: Rule) => (r.caught30d ?? 0) / (r.delivered30d || 1);
  const bad = rules.filter((r) => (r.delivered30d ?? 0) >= 4 && ratio(r) >= 0.25);
  return bad.sort((a, b) => ratio(b) - ratio(a) || (b.caught30d ?? 0) - (a.caught30d ?? 0))[0] ?? null;
}

/** Rule usage is counted per rule; decisions have no usage record at all. */
export function memoryVerdict({ decisions, rules }: { decisions: Decision[]; rules: Rule[] }): Segment[] {
  const inUse = decisions.filter((d) => d.status !== "superseded").length;
  if (!inUse && !rules.length) return ["Nothing is recorded yet. Agents get only code until a decision or rule exists."];
  const out: Segment[] = [`${cap(numberWord(inUse))} ${plural(inUse, "decision")} and ${numberWord(rules.length)} ${plural(rules.length, "rule")} are in use.`];
  const tracked = rules.some((r) => r.delivered30d !== undefined);
  if (!tracked) return out;
  const given = rules.reduce((n, r) => n + (r.delivered30d ?? 0), 0);
  out.push(" Agents got the rules ", { num: given.toLocaleString("en-US"), tone: "ok" }, ` ${plural(given, "time")} in 30 days.`);
  const worst = worstRule(rules);
  if (worst) {
    const caught = worst.caught30d ?? 0;
    out.push(" ", { ref: worst.id, label: worst.id }, ` was caught broken ${numberWord(caught)} ${plural(caught, "time")} in the ${worst.delivered30d} tasks it reached.`);
    return out;
  }
  const unused = rules.filter((r) => r.delivered30d === 0).length;
  if (unused) out.push(` ${cap(numberWord(unused))} ${plural(unused, "rule")} reached no agent.`);
  return out;
}
