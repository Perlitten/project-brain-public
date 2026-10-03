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

export interface SendResult<T = unknown> {
  ok: boolean;
  status: number;
  data: T | null;
  /** Human-readable reason on failure (API `detail`, HTTP status, network). */
  error?: string;
}

/**
 * Send a mutating request (POST/PATCH/DELETE) to the Brain API. Never throws;
 * failures come back with the API's own `detail` so the screen can say why.
 */
export async function brainSend<T = unknown>(
  method: "POST" | "PATCH" | "PUT" | "DELETE",
  path: string,
  body?: unknown,
  timeoutMs = 30_000,
): Promise<SendResult<T>> {
  if (!base || !key) return { ok: false, status: 0, data: null, error: "The Brain API is not connected (BRAIN_API_URL / BRAIN_API_KEY)." };
  let url: URL;
  try {
    url = new URL(path.replace(/^\/+/, ""), base.endsWith("/") ? base : `${base}/`);
  } catch {
    return { ok: false, status: 0, data: null, error: "BRAIN_API_URL is not a valid URL." };
  }
  try {
    const res = await fetch(url, {
      method,
      headers: { "X-API-Key": key, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
    let data: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = text;
      }
    }
    if (!res.ok) {
      return { ok: false, status: res.status, data: data as T, error: describeError(res.status, data) };
    }
    return { ok: true, status: res.status, data: data as T };
  } catch (err) {
    const timeout = err instanceof Error && (err.name === "TimeoutError" || err.name === "AbortError");
    return { ok: false, status: 0, data: null, error: timeout ? "Brain didn’t answer in time." : "Brain is unreachable right now." };
  }
}

function describeError(status: number, data: unknown): string {
  const detail = data && typeof data === "object" ? (data as Record<string, unknown>).detail : undefined;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    return detail
      .map((d) => (d && typeof d === "object" ? `${((d as { loc?: unknown[] }).loc ?? []).slice(1).join(".")}: ${(d as { msg?: string }).msg}` : String(d)))
      .join("; ");
  }
  if (status === 401 || status === 403) return "Brain refused this — the dashboard’s API key lacks the permission for it.";
  if (status === 404) return "This Brain server doesn’t support that yet — update the server.";
  if (status === 409) return "Already in progress, or conflicts with the current state.";
  if (status >= 500) return `Brain hit an internal error (HTTP ${status}).`;
  return `Brain answered HTTP ${status}.`;
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
