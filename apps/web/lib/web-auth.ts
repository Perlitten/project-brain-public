import { createCipheriv, createDecipheriv, createHash, createHmac, hkdfSync, randomBytes, timingSafeEqual } from "node:crypto";
import { publicDemoProblem } from "./public-demo";

export const SESSION_COOKIE = "pb_session";
/** Team state: the signed-in member's own credential, sealed (sealMember). */
export const MEMBER_COOKIE = "pb_member";
/** A member signs in again after a week; the seal's age is checked on every request too. */
export const MEMBER_MAX_AGE = 60 * 60 * 24 * 7;

const digest = (value: string) => createHash("sha256").update(value).digest();
const same = (a: string, b: string) => timingSafeEqual(digest(a), digest(b));

// Who may open the dashboard, in order of precedence:
//   1. WEB_BASIC_AUTH="user:password" in the deployment environment — fixed,
//      changed only by editing that variable.
//   2. The login set on Settings → Access → Dashboard sign-in, kept by the Brain API
//      (/api/web-login, scrypt hash in the server's .env).
//   3. Nothing set: open mode (anyone gets in and can set the first password
//      from Settings → Access → Dashboard sign-in) when the dashboard listens on loopback only
//      (`npm run dev` / `npm run start` default, see scripts/serve.mjs), or when
//      WEB_OPEN_MODE=true. Anywhere else the dashboard stays LOCKED (503
//      "unconfigured"), so an anonymous visitor can neither read data nor set
//      the password. WEB_OPEN_MODE=false locks a loopback dashboard too.
// Without a Brain API (both BRAIN_API_URL and BRAIN_API_KEY unset) the
// dashboard shows demo data and holds nothing to protect: open.
// Only one of BRAIN_API_URL / BRAIN_API_KEY set, a malformed WEB_BASIC_AUTH,
// or a Brain API reporting only one of WEB_LOGIN_USER / WEB_LOGIN_PASSWORD_HASH
// in its environment is a misconfiguration: fail closed (503), never demo.
// A live dashboard that can't learn the state fails closed (503).
//
// Team state (the Brain has live team members, so it refuses the shared server
// key with "Named credential required") replaces all of the above: every
// member signs in with their own named credential, which this server keeps in
// an encrypted httpOnly cookie and forwards as X-API-Key on each call
// (brainKeyFor, lib/api.ts). Every request re-asks Brain who the credential is
// (GET /api/web-session), so revocation locks the browser out on the next one.

export type LoginState =
  | { kind: "env"; expected: string }
  | { kind: "brain"; mode: "open" | "password"; user: string | null; version: string }
  | { kind: "demo" }
  | { kind: "team" }
  | { kind: "misconfigured"; reason: string }
  | { kind: "unreachable" };

/** Why a request got 503 instead of the dashboard. */
export type LockReason = "unconfigured" | "misconfigured" | "unreachable";

// A changed login is still noticed sooner: an unrecognised session cookie looks again.
const STATE_TTL_MS = 60_000;
const BASIC_TTL_MS = 60_000;
let cached: { at: number; state: LoginState & { kind: "brain" | "team" } } | null = null;
// One page load fans out into many requests; they share a single probe of Brain.
let probing: Promise<LoginState> | null = null;
const basicOk = new Map<string, number>();

const apiBase = () => process.env.BRAIN_API_URL?.trim();
const apiKey = () => process.env.BRAIN_API_KEY?.trim();
const live = () => Boolean(apiBase() && apiKey());

const TRUTHY = /^(1|true|yes|on)$/i;
const FALSY = /^(0|false|no|off)$/i;

/** Listening on loopback only (set by scripts/serve.mjs): nobody else can reach this dashboard. */
export const loopbackBind = () => process.env.WEB_LOOPBACK_BIND?.trim() === "1";

/** The request's Host header names this machine (localhost, 127.x.x.x, [::1]). */
export function localHost(host: string | null | undefined): boolean {
  const value = (host ?? "").trim().toLowerCase();
  const name = value.startsWith("[") ? value.slice(1, value.indexOf("]")) : value.replace(/:\d+$/, "");
  return name === "localhost" || name === "::1" || /^127(\.\d{1,3}){3}$/.test(name);
}

/**
 * No password set means anyone may enter: WEB_OPEN_MODE=true, or a loopback-only
 * dashboard opened by a loopback address, unless WEB_OPEN_MODE=false. A reverse
 * proxy on the same host forwards its public Host, so it stays locked.
 */
export const openMode = (host?: string | null) => {
  const flag = process.env.WEB_OPEN_MODE?.trim() ?? "";
  if (TRUTHY.test(flag)) return true;
  if (FALSY.test(flag)) return false;
  return loopbackBind() && localHost(host);
};

// One server-side line per distinct problem, so a broken deployment says why
// in the logs without flooding them on every request.
const warned = new Set<string>();
function warnOnce(message: string) {
  if (warned.has(message)) return;
  warned.add(message);
  console.error(`[web-auth] ${message}`);
}

async function callBrain(method: "GET" | "POST", path: string, body?: unknown, key = apiKey()!): Promise<{ status: number; data: Record<string, unknown> | null }> {
  const base = apiBase()!;
  const url = new URL(path.replace(/^\/+/, ""), base.endsWith("/") ? base : `${base}/`);
  const res = await fetch(url, {
    method,
    headers: { "X-API-Key": key, Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
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
  probing = null;
  basicOk.clear();
}

export async function loginState({ fresh = false } = {}): Promise<LoginState> {
  const demoProblem = publicDemoProblem();
  if (demoProblem) {
    warnOnce(demoProblem);
    return { kind: "misconfigured", reason: demoProblem };
  }
  const expected = process.env.WEB_BASIC_AUTH?.trim();
  if (expected) {
    const separator = expected.indexOf(":");
    if (separator < 1 || separator === expected.length - 1) {
      const reason = "WEB_BASIC_AUTH must look like user:password; the dashboard stays locked until it does.";
      warnOnce(reason);
      return { kind: "misconfigured", reason };
    }
    // A team Brain refuses the server key: members sign in as themselves instead.
    return live() && (await brainLogin(fresh)).kind === "team" ? { kind: "team" } : { kind: "env", expected };
  }
  if (Boolean(apiBase()) !== Boolean(apiKey())) {
    const reason = `${apiBase() ? "BRAIN_API_KEY" : "BRAIN_API_URL"} is missing: set both BRAIN_API_URL and BRAIN_API_KEY for live data, or neither for demo data. The dashboard stays locked until then.`;
    warnOnce(reason);
    return { kind: "misconfigured", reason };
  }
  if (!live()) return { kind: "demo" };
  return brainLogin(fresh);
}

// "Named credential required" (auth.py) and "Team administrator required"
// (team_access.py) are the Brain refusing the shared server key because the
// team has members: team state, not an outage.
const TEAM_REFUSAL = /^(Named credential required|Team administrator required)/;

async function brainLogin(fresh: boolean): Promise<LoginState> {
  if (!fresh && cached && Date.now() - cached.at < STATE_TTL_MS) return cached.state;
  if (probing) return probing;
  const probe = probeBrainLogin().finally(() => {
    if (probing === probe) probing = null;
  });
  probing = probe;
  return probe;
}

async function probeBrainLogin(): Promise<LoginState> {
  try {
    const { status, data } = await callBrain("GET", "/api/web-login");
    if (status === 200 && data && data.mode === "open") {
      // Brain reports "open" when only one of WEB_LOGIN_USER / WEB_LOGIN_PASSWORD_HASH
      // is set. That is a half-configured login, not a request to be open.
      const fromEnv = Array.isArray(data.from_env) ? data.from_env : [];
      const pair = ["WEB_LOGIN_USER", "WEB_LOGIN_PASSWORD_HASH"];
      const present = pair.filter((name) => fromEnv.includes(name));
      if (present.length === 1) {
        const missing = pair.find((name) => !present.includes(name));
        const reason = `The Brain API has ${present[0]} but not ${missing}: set both (or neither) on the Brain server and restart it. The dashboard stays locked until then.`;
        warnOnce(reason);
        return { kind: "misconfigured", reason };
      }
    }
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
    if ((status === 401 || status === 403) && typeof data?.detail === "string" && TEAM_REFUSAL.test(data.detail)) {
      const state = { kind: "team" } as const;
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

function cookieFrom(cookieHeader: string | null, wanted: string): string | null {
  for (const part of (cookieHeader ?? "").split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === wanted) return rest.join("=");
  }
  return null;
}

/** Reads the session cookie from a raw Cookie header. */
export function sessionFrom(cookieHeader: string | null): string | null {
  return cookieFrom(cookieHeader, SESSION_COOKIE);
}

/** Reads the sealed member credential from a raw Cookie header. */
export function memberFrom(cookieHeader: string | null): string | null {
  return cookieFrom(cookieHeader, MEMBER_COOKIE);
}

// The member cookie is AES-256-GCM over {k: credential, iat}, keyed from the
// dashboard's server key, so the browser holds an opaque value it can neither
// read nor forge, and rotating BRAIN_API_KEY signs every member out.
const sealKey = () => Buffer.from(hkdfSync("sha256", apiKey() ?? "", "project-brain-web-member", "v1", 32));

/** Cookie value carrying a member's credential; only this server can open it. */
export function sealMember(credential: string, now = Date.now()): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", sealKey(), iv);
  cipher.setAAD(Buffer.from(MEMBER_COOKIE));
  const body = Buffer.concat([cipher.update(JSON.stringify({ k: credential, iat: Math.floor(now / 1000) }), "utf8"), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), body]).toString("base64url");
}

/** The credential inside a member cookie, or null when it is missing, forged, sealed by another key or too old. */
export function openMember(value: string | null, now = Date.now()): string | null {
  if (!value || !apiKey()) return null;
  try {
    const raw = Buffer.from(value, "base64url");
    if (raw.length < 29) return null;
    const decipher = createDecipheriv("aes-256-gcm", sealKey(), raw.subarray(0, 12));
    decipher.setAAD(Buffer.from(MEMBER_COOKIE));
    decipher.setAuthTag(raw.subarray(12, 28));
    const payload = JSON.parse(Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8")) as { k?: unknown; iat?: unknown };
    const age = Math.floor(now / 1000) - Number(payload.iat);
    if (typeof payload.k !== "string" || !payload.k || !Number.isFinite(age) || age < -60 || age > MEMBER_MAX_AGE) return null;
    return payload.k;
  } catch {
    return null;
  }
}

export interface WebMember {
  id: number;
  name: string;
  /** Offered the Team page's administration (Brain still decides every call). */
  admin: boolean;
}

type WhoIs = { status: 200; member: WebMember } | { status: 401 | 503; solo?: boolean };

/** Who a member credential belongs to right now. Asked of Brain on every call, never cached, so revocation is immediate. */
async function whoIs(credential: string | null): Promise<WhoIs> {
  if (!credential) return { status: 401 };
  try {
    const { status, data } = await callBrain("GET", "/api/web-session", undefined, credential);
    if (status === 401 || status === 403) return { status: 401 };
    const principal = data?.principal as { id?: unknown; name?: unknown } | undefined;
    if (status !== 200 || !principal || typeof principal.name !== "string") return { status: 503 };
    // The last member left since this process looked: the owner's login applies again.
    if (data?.mode !== "team") return { status: 401, solo: true };
    return { status: 200, member: { id: Number(principal.id), name: principal.name, admin: data.team_admin === true } };
  } catch {
    return { status: 503 };
  }
}

function settle(who: WhoIs): WhoIs {
  if (who.status !== 200 && who.solo) forgetLoginState();
  return who;
}

/** The signed-in member for a sealed cookie value, or null (not team state, not signed in, revoked). */
export async function memberFor(cookieValue: string | null): Promise<WebMember | null> {
  if ((await loginStateForMember(cookieValue)).kind !== "team") return null;
  const who = settle(await whoIs(openMember(cookieValue)));
  return who.status === 200 ? who.member : null;
}

/**
 * The key to send to Brain for a request carrying this member cookie: in team
 * state the member's own credential (null without one, so nothing is
 * forwarded), otherwise the dashboard's server key, exactly as before.
 */
export async function brainKeyFor(cookieValue: string | null): Promise<string | null> {
  if ((await loginStateForMember(cookieValue)).kind === "team") return openMember(cookieValue);
  return apiKey() ?? null;
}

/** Another web process may still cache solo after bootstrap. Only a valid
 * server-sealed member cookie prompts a fresh probe; it never supplies authority. */
export async function loginStateForMember(cookieValue: string | null): Promise<LoginState> {
  const state = await loginState();
  return state.kind !== "team" && openMember(cookieValue) ? loginState({ fresh: true }) : state;
}

/**
 * Logout: ask Brain to end the member's single sign-on session. Brain revokes
 * the credential only when SSO minted it, so a pasted member key (also used by
 * the CLI and MCP clients) stays live. Best effort: the cookies are cleared anyway.
 */
export async function signOutMember(cookieValue: string | null): Promise<void> {
  const credential = openMember(cookieValue);
  if (!credential || !apiBase()) return;
  try {
    await callBrain("POST", "/team/oidc/logout", undefined, credential);
  } catch {
    // Brain unreachable: the session still expires on its own.
  }
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

/** Brain keeps no login and WEB_OPEN_MODE is off: nobody gets in until one is configured. */
function unconfigured(state: LoginState, host: string | null): boolean {
  return state.kind === "brain" && state.mode === "open" && !openMode(host);
}

/** Why the dashboard is answering 503 right now (null: it isn't). */
export async function lockReason(host: string | null = null): Promise<{ reason: LockReason; detail: string } | null> {
  const state = await loginState();
  if (state.kind === "misconfigured") return { reason: "misconfigured", detail: state.reason };
  if (state.kind === "unreachable") return { reason: "unreachable", detail: "Brain is not answering, so sign-in can't be checked." };
  if (unconfigured(state, host)) return { reason: "unconfigured", detail: UNCONFIGURED_DETAIL };
  return null;
}

export const UNCONFIGURED_DETAIL =
  "No dashboard sign-in is configured. Set WEB_BASIC_AUTH=user:password in the dashboard's environment, or set a login through the Brain API (PUT /api/web-login), or open the dashboard on this machine (npm run dev / npm run start listen on 127.0.0.1) to set one from Settings → Access → Dashboard sign-in.";

export async function authorizeWebRequest(header: string | null, session: string | null = null, host: string | null = null,
  member: string | null = null): Promise<200 | 401 | 503> {
  const state = await loginStateForMember(member);
  if (state.kind === "demo") return 200;
  if (state.kind === "team") return settle(await whoIs(openMember(member))).status;
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
  if (state.mode === "open") {
    if (openMode(host)) return 200;
    warnOnce("No dashboard sign-in is configured and the dashboard is reachable beyond this machine: it stays locked.");
    return 503;
  }
  if (session) {
    if (state.user && same(session, brainSession(state.user, state.version))) return 200;
    // The login may have just changed in another instance: look again once.
    const now = await loginState({ fresh: true });
    if (now.kind === "brain" && now.mode === "open") return openMode(host) ? 200 : 503;
    if (now.kind === "brain" && now.user && same(session, brainSession(now.user, now.version))) return 200;
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
    return openMode(host) ? 200 : 503;
  }
  basicOk.set(key, Date.now());
  return 200;
}

export type SignInResult = { status: 401 } | { status: 429 } | { status: 503 } | { status: 200; session: string | null };
export type MemberSignInResult = { status: 401 } | { status: 503 } | { status: 200; cookie: string; member: WebMember };

/** Team state: checks a member's own credential with Brain; on success returns the sealed cookie value to set. */
export async function signInMember(credential: string): Promise<MemberSignInResult> {
  const state = await loginState();
  if (state.kind === "misconfigured" || state.kind === "unreachable") return { status: 503 };
  const key = credential.trim();
  if (state.kind !== "team" || !key || key.length > 512) return { status: 401 };
  const who = settle(await whoIs(key));
  if (who.status !== 200) return { status: who.status };
  return { status: 200, cookie: sealMember(key), member: who.member };
}

/** Checks a sign-in form; on success returns the session cookie value to set. */
export async function signIn(user: string, password: string, host: string | null = null): Promise<SignInResult> {
  const state = await loginState();
  if (state.kind === "demo") return { status: 200, session: null };
  if (state.kind === "misconfigured" || state.kind === "unreachable") return { status: 503 };
  // A team Brain no longer takes the owner login: members use signInMember.
  if (state.kind === "team") return { status: 401 };
  if (state.kind === "env") return same(`${user}:${password}`, state.expected) ? { status: 200, session: envSession(state.expected) } : { status: 401 };
  if (state.mode === "open") return openMode(host) ? { status: 200, session: null } : { status: 503 };
  const result = await verifyWithBrain(user, password);
  if (result.throttled) return { status: 429 };
  if (result.down) return { status: 503 };
  if (!result.ok) return { status: 401 };
  if (result.open) {
    forgetLoginState();
    return openMode(host) ? { status: 200, session: null } : { status: 503 };
  }
  return { status: 200, session: brainSession(user, result.version ?? state.version) };
}

/** JSON refusal `{error, detail}` for scripts and fetch callers. */
export function webAuthResponse(status: 401 | 503, lock: { reason: LockReason; detail: string } | null = null): Response {
  const body =
    status === 401
      ? { error: "auth_required", detail: "Sign in, or send HTTP Basic credentials." }
      : { error: lock?.reason ?? "unavailable", detail: lock?.detail ?? "Sign-in can't be checked right now: Brain is not answering, or the dashboard is misconfigured." };
  return Response.json(body, {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(status === 401 ? { "WWW-Authenticate": 'Basic realm="Project Brain"' } : {}),
    },
  });
}
