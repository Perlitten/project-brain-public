// Server-only client for the Project Brain API. The key is read from the
// server environment and attached here; nothing in this module may be
// imported by a client component. In team state the signed-in member's own
// credential (sealed in an httpOnly cookie, see lib/web-auth.ts) is sent
// instead, so Brain authorizes every call as that member.
import "server-only";
import { cookies } from "next/headers";
import { publicDemoProblem } from "./public-demo";
import { MEMBER_COOKIE, brainKeyFor, loginStateForMember } from "./web-auth";

// Block reads as well as mutations if an isolated public demo is misconfigured.
const blocked = publicDemoProblem();
const base = blocked ? undefined : process.env.BRAIN_API_URL?.trim();
const key = blocked ? undefined : process.env.BRAIN_API_KEY?.trim();

export const apiConfigured = Boolean(base && key);

type FetchOptions = {
  /** Fast-changing data (jobs, events, runs) skips the data cache. */
  fresh?: boolean;
  /** Seconds to keep a cached response; ignored when `fresh`. */
  revalidate?: number;
  /** Abort the request after this many ms (default 4000). Fan-out calls that
      block first paint (per-repo overviews) should pass a shorter budget. */
  timeoutMs?: number;
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
 * Build the request URL for an API `path`. The origin always comes from
 * BRAIN_API_URL: `path` can only extend its pathname (keeping any prefix such
 * as /brain), never replace the host or climb above the base. Returns null
 * when BRAIN_API_URL itself is not a valid URL or `path` tries to escape.
 */
function apiUrl(apiBase: string, path: string): URL | null {
  let url: URL;
  try {
    url = new URL(apiBase);
  } catch {
    return null;
  }
  const queryAt = path.indexOf("?");
  const pathPart = (queryAt === -1 ? path : path.slice(0, queryAt)).replace(/^\/+/, "");
  const segments = pathPart.split("/");
  if (segments.some((segment) => segment === "..")) return null;
  const basePath = url.pathname.endsWith("/") ? url.pathname : `${url.pathname}/`;
  url.pathname = `${basePath}${segments.join("/")}`;
  // WHATWG URL parsing also normalises percent-encoded dot segments
  // (`%2e%2e`). Re-check the serialised pathname so those cannot climb above
  // a configured prefix such as `/brain/`.
  if (!url.pathname.startsWith(basePath)) return null;
  url.search = queryAt === -1 ? "" : path.slice(queryAt);
  url.hash = "";
  return url;
}

/**
 * The X-API-Key for the request being served. Outside team state this is the
 * server key, exactly as before team mode. In team state it is only ever the
 * signed-in member's own credential from their sealed cookie (null without a
 * cookie that still opens: the server key is never sent), and the response
 * bypasses the data cache so one member's answers never serve another request.
 */
async function requestKey(): Promise<{ key: string | null; member: boolean }> {
  let sealed: string | null = null;
  try {
    sealed = (await cookies()).get(MEMBER_COOKIE)?.value ?? null;
  } catch {
    sealed = null; // outside a request (build): no member, so nothing is sent
  }
  if ((await loginStateForMember(sealed)).kind !== "team") return { key: key ?? null, member: false };
  return { key: await brainKeyFor(sealed), member: true };
}

const SIGNED_OUT = "Your sign-in is no longer valid. Sign in again.";

export interface SendResult<T = unknown> {
  ok: boolean;
  status: number;
  data: T | null;
  /** Human-readable reason on failure (API `detail`, HTTP status, network). */
  error?: string;
}

export type ReadFailure = "not_configured" | "not_found" | "timeout" | "network" | "http_error" | "invalid_response";
export type ReadResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; reason: ReadFailure };

export function readFailureNote(reason: ReadFailure): string {
  switch (reason) {
    case "not_found": return "Not available on this server";
    case "timeout": return "Brain did not answer in time";
    case "network": return "Brain is unreachable";
    case "invalid_response": return "Brain returned an unreadable response";
    case "http_error": return "Brain could not provide this measurement";
    case "not_configured": return "Brain is not connected";
  }
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
  const url = apiUrl(base, path);
  if (!url) return { ok: false, status: 0, data: null, error: "BRAIN_API_URL is not a valid URL." };
  const who = await requestKey();
  if (!who.key) return { ok: false, status: 401, data: null, error: SIGNED_OUT };
  try {
    const res = await fetch(url, {
      method,
      headers: { "X-API-Key": who.key, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
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
  if (status === 401 || status === 403) return "Brain refused this — the key in use lacks the permission for it.";
  if (status === 404) return "This Brain server doesn’t support that yet — update the server.";
  if (status === 409) return "Already in progress, or conflicts with the current state.";
  if (status >= 500) return `Brain hit an internal error (HTTP ${status}).`;
  return `Brain answered HTTP ${status}.`;
}

/**
 * GET a JSON document from the Brain API. Returns null on any failure
 * (not configured, network error, timeout, non-2xx, bad JSON) and never throws.
 */
export async function brainRead<T>(path: string, opts: FetchOptions = {}): Promise<ReadResult<T>> {
  if (!base || !key) return { ok: false, status: 0, reason: "not_configured" };
  const url = apiUrl(base, path);
  if (!url) {
    logOnce(path, "invalid BRAIN_API_URL");
    return { ok: false, status: 0, reason: "not_configured" };
  }
  const who = await requestKey();
  if (!who.key) return { ok: false, status: 401, reason: "http_error" };
  try {
    const res = await fetch(url, {
      headers: { "X-API-Key": who.key, Accept: "application/json" },
      signal: AbortSignal.timeout(opts.timeoutMs ?? 4000),
      ...(opts.fresh || who.member ? { cache: "no-store" as const } : { next: { revalidate: opts.revalidate ?? 30 } }),
    });
    if (!res.ok) {
      logOnce(path, `HTTP ${res.status}`);
      return { ok: false, status: res.status, reason: res.status === 404 ? "not_found" : "http_error" };
    }
    try {
      return { ok: true, status: res.status, data: (await res.json()) as T };
    } catch (err) {
      // A timeout while receiving the body still uses the request deadline.
      if (timeoutError(err)) throw err;
      logOnce(path, "invalid JSON");
      return { ok: false, status: res.status, reason: "invalid_response" };
    }
  } catch (err) {
    const reason = timeoutError(err) ? "timeout" : "network";
    logOnce(path, reason);
    return { ok: false, status: 0, reason };
  }
}

function timeoutError(err: unknown): boolean {
  return Boolean(err && typeof err === "object" && "name" in err &&
    (err.name === "TimeoutError" || err.name === "AbortError"));
}

/** Compatibility wrapper for callers that only need an unavailable fallback. */
export async function brainFetch<T>(path: string, opts: FetchOptions = {}): Promise<T | null> {
  const result = await brainRead<T>(path, opts);
  return result.ok ? result.data : null;
}
