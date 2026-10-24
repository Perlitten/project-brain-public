// Where an event's reference leads. Pure and client-safe: the Ledger renders
// refs as links, and lib/data.ts fills `href` for live events from the entity type.

const byType: Record<string, (id: string) => string> = {
  task: (id) => `/runs?q=${encodeURIComponent(id.slice(0, 8))}`,
  run: (id) => `/runs?q=${encodeURIComponent(id.slice(0, 8))}`,
  harness_task: (id) => `/runs?q=${encodeURIComponent(id.slice(0, 8))}`,
  context_pack: (id) => `/packs/${encodeURIComponent(id)}`,
  pack: (id) => `/packs/${encodeURIComponent(id)}`,
  decision: (id) => `/memory?q=${encodeURIComponent(id)}`,
  rule: (id) => `/memory?q=${encodeURIComponent(id)}`,
  proposal: () => "/review",
  job: () => "/activity#jobs",
  background_job: () => "/activity#jobs",
  index: () => "/indexing#runs",
  index_run: () => "/indexing#runs",
  repository: () => "/indexing",
  credential: () => "/admin",
  principal: () => "/admin",
  evaluation: () => "/quality#reports",
  eval: () => "/quality#reports",
};

/** Demo refs read like "r-2289", "pk-7f2a", "D-41", "j-8838", "run 412", "eval 88". */
function byRef(ref: string): string | null {
  const r = ref.trim();
  if (/^r-\d+$/i.test(r)) return `/runs?q=${encodeURIComponent(r)}`;
  if (/^pk-/i.test(r)) return `/packs?q=${encodeURIComponent(r.slice(3))}`;
  if (/^D-\d+$/i.test(r)) return `/memory?q=${encodeURIComponent(r)}`;
  if (/^R-\d+$/.test(r)) return `/memory?q=${encodeURIComponent(r)}`;
  if (/^j-\d+$/i.test(r)) return "/activity#jobs";
  if (/^run \d+$/i.test(r)) return "/indexing#runs";
  if (/^eval \d+$/i.test(r)) return "/quality#reports";
  if (/^in-\d+$/i.test(r)) return "/quality";
  return null;
}

export function eventHref(ref?: string, entityType?: string, entityId?: string): string | null {
  const type = (entityType ?? "").toLowerCase().replace(/[\s-]+/g, "_");
  if (type && entityId && byType[type]) return byType[type](entityId);
  return ref ? byRef(ref) : null;
}
