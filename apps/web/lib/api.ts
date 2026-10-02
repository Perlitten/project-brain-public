// Server-only client for the Project Brain API. The key is read from the
// server environment and attached here; nothing in this module may be
// imported by a client component.
import "server-only";

const base = process.env.BRAIN_API_URL?.trim();
const key = process.env.BRAIN_API_KEY?.trim();

export const apiConfigured = Boolean(base && key);

type FetchOptions = {
  /** Fast-changing data (jobs, events, runs) skips the data cache. */
  fresh?: boolean;
  /** Seconds to keep a cached response; ignored when `fresh`. */
  revalidate?: number;
};

// One line per failing path+reason per process, so a dead API does not flood
// the logs on every render.
const logged = new Set<string>();
function logOnce(path: string, reason: string) {
  const k = `${path} ${reason}`;
  if (logged.has(k)) return;
  logged.add(k);
  console.error(`[brain-api] ${path}: ${reason}`);
}

/**
 * GET a JSON document from the Brain API. Returns null on any failure
 * (not configured, network error, timeout, non-2xx, bad JSON) and never throws.
 */
export async function brainFetch<T>(path: string, opts: FetchOptions = {}): Promise<T | null> {
  if (!base || !key) return null;
  let url: URL;
  try {
    // Relative join keeps any path prefix on BRAIN_API_URL (e.g. /brain).
    url = new URL(path.replace(/^\/+/, ""), base.endsWith("/") ? base : `${base}/`);
  } catch {
    logOnce(path, "invalid BRAIN_API_URL");
    return null;
  }
  try {
    const res = await fetch(url, {
      headers: { "X-API-Key": key, Accept: "application/json" },
      signal: AbortSignal.timeout(8000),
      ...(opts.fresh ? { cache: "no-store" as const } : { next: { revalidate: opts.revalidate ?? 30 } }),
    });
    if (!res.ok) {
      logOnce(path, `HTTP ${res.status}`);
      return null;
    }
    return (await res.json()) as T;
  } catch (err) {
    logOnce(path, err instanceof Error ? `${err.name}: ${err.message}` : String(err));
    return null;
  }
}
