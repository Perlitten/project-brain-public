import { createHash, createHmac, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "pb_session";

const digest = (value: string) => createHash("sha256").update(value).digest();
const same = (a: string, b: string) => timingSafeEqual(digest(a), digest(b));

// Who may open the dashboard, in order of precedence:
//   1. WEB_BASIC_AUTH="user:password" in the deployment environment — fixed,
//      changed only by editing that variable.
//   2. The login set on Settings → Sign-in, kept by the Brain API
//      (/api/web-login, scrypt hash in the server's .env).
//   3. Nothing set: sign-in is open. A fresh install starts here, and the
//      dashboard asks the owner to set a password.
// Without a Brain API (demo data) there is nothing to protect: open.
// A live dashboard that can't learn the state fails closed (503).

export type LoginState =
  | { kind: "env"; expected: string }
  | { kind: "brain"; mode: "open" | "password"; user: string | null; version: string }
  | { kind: "demo" }
  | { kind: "misconfigured" }
  | { kind: "unreachable" };

const STATE_TTL_MS = 10_000;
const BASIC_TTL_MS = 60_000;
let cached: { at: number; state: LoginState & { kind: "brain" } } | null = null;
const basicOk = new Map<string, number>();

const apiBase = () => process.env.BRAIN_API_URL?.trim();
const apiKey = () => process.env.BRAIN_API_KEY?.trim();
const live = () => Boolean(apiBase() && apiKey());

async function callBrain(method: "GET" | "POST", path: string, body?: unknown): Promise<{ status: number; data: Record<string, unknown> | null }> {
  const base = apiBase()!;
  const url = new URL(path.replace(/^\/+/, ""), base.endsWith("/") ? base : `${base}/`);
  const res = await fetch(url, {
    method,
    headers: { "X-API-Key": apiKey()!, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(6000),
  });
  let data: Record<string, unknown> | null = null;
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    data = null;
  }
  return { status: res.status, data };
}

/** Drop what this process remembers, after a change made here. */
export function forgetLoginState() {
  cached = null;
  basicOk.clear();
}

export async function loginState({ fresh = false } = {}): Promise<LoginState> {
  const expected = process.env.WEB_BASIC_AUTH?.trim();
  if (expected) {
    const separator = expected.indexOf(":");
    return separator < 1 || separator === expected.length - 1 ? { kind: "misconfigured" } : { kind: "env", expected };
  }
  if (!live()) return { kind: "demo" };
  if (!fresh && cached && Date.now() - cached.at < STATE_TTL_MS) return cached.state;
  try {
    const { status, data } = await callBrain("GET", "/api/web-login");
    if (status === 200 && data && (data.mode === "open" || data.mode === "password")) {
      const state: LoginState & { kind: "brain" } = {
        kind: "brain",
        mode: data.mode,
        user: typeof data.user === "string" ? data.user : null,
        version: typeof data.version === "string" ? data.version : "0",
      };
      cached = { at: Date.now(), state };
      return state;
    }
  } catch {
    // fall through to the last known state
  }
  // Brain is briefly away: keep the last answer rather than locking everyone out.
  return cached?.state ?? { kind: "unreachable" };
}

// The browser session cookie is an HMAC keyed by the credentials (env) or by
// the API key plus the login's version (Brain), so any change of login or
// password signs every browser out.
const envSession = (expected: string) => createHmac("sha256", expected).update("project-brain-web-session-v1").digest("base64url");
const brainSession = (user: string, version: string) =>
  createHmac("sha256", apiKey() ?? "").update(`project-brain-web-session-v2|${user}|${version}`).digest("base64url");

/** Session cookie value for a Brain-kept login (set after a change on Settings). */
export function sessionForLogin(user: string, version: string) {
  return brainSession(user, version);
}

/** Reads the session cookie from a raw Cookie header. */
export function sessionFrom(cookieHeader: string | null): string | null {
  for (const part of (cookieHeader ?? "").split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === SESSION_COOKIE) return rest.join("=");
  }
  return null;
}

function basicPair(header: string | null): [string, string] | null {
  const match = /^Basic\s+(\S+)$/i.exec(header ?? "");
  if (!match) return null;
  try {
    const decoded = Buffer.from(match[1], "base64").toString("utf8");
    const separator = decoded.indexOf(":");
    return separator < 0 ? null : [decoded.slice(0, separator), decoded.slice(separator + 1)];
  } catch {
    return null;
  }
}

async function verifyWithBrain(user: string, password: string): Promise<{ ok: boolean; version?: string; open?: boolean; throttled?: boolean; down?: boolean }> {
  try {
    const { status, data } = await callBrain("POST", "/api/web-login/verify", { user, password });
    if (status === 429) return { ok: false, throttled: true };
    if (status !== 200 || !data) return { ok: false, down: true };
    // Removed elsewhere since this process last looked: sign-in is open now.
    if (data.mode === "open") return { ok: true, open: true };
    return { ok: data.ok === true, version: typeof data.version === "string" ? data.version : undefined };
  } catch {
    return { ok: false, down: true };
  }
}

export async function authorizeWebRequest(header: string | null, session: string | null = null): Promise<200 | 401 | 503> {
  const state = await loginState();
  if (state.kind === "demo") return 200;
  if (state.kind === "misconfigured" || state.kind === "unreachable") return 503;
  if (state.kind === "env") {
    if (session && same(session, envSession(state.expected))) return 200;
    const match = /^Basic\s+(\S+)$/i.exec(header ?? "");
    if (!match) return 401;
    try {
      return same(Buffer.from(match[1], "base64").toString("utf8"), state.expected) ? 200 : 401;
    } catch {
      return 401;
    }
  }
  if (state.mode === "open") return 200;
  if (session) {
    if (state.user && same(session, brainSession(state.user, state.version))) return 200;
    // The login may have just changed in another instance: look again once.
    const now = await loginState({ fresh: true });
    if (now.kind === "brain" && (now.mode === "open" || (now.user && same(session, brainSession(now.user, now.version))))) return 200;
  }
  const pair = basicPair(header);
  if (!pair) return 401;
  const key = createHash("sha256").update(`${header}|${state.version}`).digest("hex");
  const seen = basicOk.get(key);
  if (seen && Date.now() - seen < BASIC_TTL_MS) return 200;
  const result = await verifyWithBrain(pair[0], pair[1]);
  if (result.down) return 503;
  if (!result.ok) return 401;
  if (result.open) {
    forgetLoginState();
    return 200;
  }
  basicOk.set(key, Date.now());
  return 200;
}

export type SignInResult = { status: 401 } | { status: 429 } | { status: 503 } | { status: 200; session: string | null };

/** Checks a sign-in form; on success returns the session cookie value to set. */
export async function signIn(user: string, password: string): Promise<SignInResult> {
  const state = await loginState();
  if (state.kind === "demo") return { status: 200, session: null };
  if (state.kind === "misconfigured" || state.kind === "unreachable") return { status: 503 };
  if (state.kind === "env") return same(`${user}:${password}`, state.expected) ? { status: 200, session: envSession(state.expected) } : { status: 401 };
  if (state.mode === "open") return { status: 200, session: null };
  const result = await verifyWithBrain(user, password);
  if (result.throttled) return { status: 429 };
  if (result.down) return { status: 503 };
  if (!result.ok) return { status: 401 };
  if (result.open) {
    forgetLoginState();
    return { status: 200, session: null };
  }
  return { status: 200, session: brainSession(user, result.version ?? state.version) };
}

export function webAuthResponse(status: 401 | 503): Response {
  return new Response(status === 401 ? "Authentication required" : "Sign-in can't be checked right now: Brain is not answering, or WEB_BASIC_AUTH is malformed", {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(status === 401 ? { "WWW-Authenticate": 'Basic realm="Project Brain"' } : {}),
    },
  });
}
