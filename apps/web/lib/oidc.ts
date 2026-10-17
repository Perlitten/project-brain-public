import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";
import { MEMBER_COOKIE, MEMBER_MAX_AGE, SESSION_COOKIE, forgetLoginState, signInMember } from "./web-auth";

// Team single sign-on (OpenID Connect) for the dashboard; docs/operations/TEAM_SSO.md.
//
//   GET|POST /auth/oidc/start[?next=/path][&invitation=pbi_...]
//   GET      /auth/oidc/callback?state=...&code=...   (the provider sends the browser here)
//
// The Brain API runs the protocol (POST /team/oidc/start and /team/oidc/callback,
// brain/team/oidc.py). This side only keeps the browser's half: a random binding
// secret and the returned `state`, in an httpOnly cookie scoped to /auth/oidc.
// The API stores only their hashes and redeems a code only together with the
// binding, so a code that leaks from the provider's redirect (logs, history,
// Referer) is useless in another browser. On success the API returns a member
// credential, which is sealed into the usual pb_member cookie exactly like a
// pasted member key (signInMember), so the session model and scopes are the same.
// These calls carry no X-API-Key: the routes are public by design and the
// dashboard's server key has no business vouching for a person.

export const OIDC_START = "/auth/oidc/start";
export const OIDC_CALLBACK = "/auth/oidc/callback";
export const OIDC_COOKIE = "pb_oidc";
const COOKIE_PATH = "/auth/oidc";
/** Matches the API's ten-minute authorization request. */
const OIDC_MAX_AGE = 600;
const CONFIG_TTL_MS = 30_000;
const MAX_INVITATION = 512;

type Flow = { s: string; b: string; n: string };
type Brain = { status: number; data: Record<string, unknown> | null };
export type OidcFailure = (next: string, message: string, status: number) => Response | Promise<Response>;

const apiBase = () => process.env.BRAIN_API_URL?.trim();
const digest = (value: string) => createHash("sha256").update(value).digest();
const same = (a: string, b: string) => timingSafeEqual(digest(a), digest(b));

async function callOidc(method: "GET" | "POST", path: string, body?: unknown): Promise<Brain> {
  const base = apiBase();
  if (!base) return { status: 404, data: null };
  const url = new URL(path.replace(/^\/+/, ""), base.endsWith("/") ? base : `${base}/`);
  const res = await fetch(url, {
    method,
    headers: { Accept: "application/json", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
    // The callback waits on the provider's token endpoint and key set.
    signal: AbortSignal.timeout(20_000),
  });
  let data: Record<string, unknown> | null = null;
  try {
    data = (await res.json()) as Record<string, unknown>;
  } catch {
    data = null;
  }
  return { status: res.status, data };
}

let offered: { at: number; enabled: boolean } | null = null;

/** Whether the Brain offers single sign-on (cached briefly; false when it can't be asked). */
export async function ssoOffered(): Promise<boolean> {
  if (offered && Date.now() - offered.at < CONFIG_TTL_MS) return offered.enabled;
  let enabled = false;
  try {
    const { status, data } = await callOidc("GET", "/team/oidc/config");
    enabled = status === 200 && data?.enabled === true;
  } catch {
    enabled = false;
  }
  offered = { at: Date.now(), enabled };
  return enabled;
}

/** Drop the cached answer (tests, or after a configuration change). */
export function forgetSso() {
  offered = null;
}

/** The sign-in page's "Sign in with SSO" link. */
export const ssoHref = (next: string) => (next && next !== "/" ? `${OIDC_START}?next=${encodeURIComponent(next)}` : OIDC_START);

function encodeFlow(flow: Flow) {
  return Buffer.from(JSON.stringify(flow), "utf8").toString("base64url");
}

function decodeFlow(value: string | undefined): Flow | null {
  if (!value || value.length > 2048) return null;
  try {
    const flow = JSON.parse(Buffer.from(value, "base64url").toString("utf8")) as Partial<Flow>;
    if (typeof flow.s !== "string" || typeof flow.b !== "string" || typeof flow.n !== "string" || !flow.s || !flow.b) return null;
    return { s: flow.s, b: flow.b, n: flow.n };
  } catch {
    return null;
  }
}

const secure = (req: NextRequest) => req.nextUrl.protocol === "https:";

function clearFlow(res: NextResponse, req: NextRequest) {
  res.cookies.set(OIDC_COOKIE, "", { httpOnly: true, secure: secure(req), sameSite: "lax", path: COOKIE_PATH, maxAge: 0 });
}

const detailOf = (data: Record<string, unknown> | null, fallback: string) =>
  typeof data?.detail === "string" && data.detail.length <= 300 ? data.detail : fallback;

/** /auth/oidc/start: ask Brain for an authorization request and send the browser to the provider. */
export async function oidcStart(req: NextRequest, safeNext: (value: unknown) => string, failure: OidcFailure): Promise<Response> {
  let invitation: unknown = req.nextUrl.searchParams.get("invitation");
  let nextValue: unknown = req.nextUrl.searchParams.get("next");
  if (req.method === "POST") {
    const form = await req.formData().catch(() => null);
    invitation = form?.get("invitation") ?? invitation;
    nextValue = form?.get("next") ?? nextValue;
  } else if (req.method !== "GET" && req.method !== "HEAD") {
    return new Response(null, { status: 405, headers: { Allow: "GET, POST" } });
  }
  const next = safeNext(nextValue);
  const token = typeof invitation === "string" ? invitation.trim() : "";
  if (token.length > MAX_INVITATION) return failure(next, "That invitation link is not valid.", 400);
  if (!apiBase()) return failure(next, "Single sign-on needs a live Brain API (BRAIN_API_URL).", 404);
  const binding = randomBytes(32).toString("base64url");
  let answer: Brain;
  try {
    answer = await callOidc("POST", "/team/oidc/start", { binding, ...(token ? { invitation: token } : {}) });
  } catch {
    return failure(next, "Brain isn’t answering, so single sign-on can’t start. Try again in a minute.", 503);
  }
  const { status, data } = answer;
  const target = typeof data?.authorization_url === "string" ? data.authorization_url : "";
  const state = typeof data?.state === "string" ? data.state : "";
  if (status !== 200 || !/^https?:\/\//i.test(target) || !state) {
    if (status === 404) return failure(next, "Single sign-on is not enabled on this Brain.", 404);
    if (status === 429) return failure(next, "Too many sign-in attempts. Wait a few minutes, then try again.", 429);
    return failure(next, detailOf(data, "Single sign-on could not start."), status >= 400 && status < 500 ? 403 : 502);
  }
  const res = NextResponse.redirect(target, 303);
  res.cookies.set(OIDC_COOKIE, encodeFlow({ s: state, b: binding, n: next }), {
    httpOnly: true,
    secure: secure(req),
    sameSite: "lax", // the provider brings the browser back with a top-level GET
    path: COOKIE_PATH,
    maxAge: OIDC_MAX_AGE,
  });
  // The invitation token may sit in this URL: never pass it on as a Referer.
  res.headers.set("Referrer-Policy", "no-referrer");
  res.headers.set("Cache-Control", "no-store");
  return res;
}

/** /auth/oidc/callback: finish with Brain and open a member session. */
export async function oidcCallback(req: NextRequest, safeNext: (value: unknown) => string, failure: OidcFailure): Promise<Response> {
  const params = req.nextUrl.searchParams;
  const flow = decodeFlow(req.cookies.get(OIDC_COOKIE)?.value);
  const next = safeNext(flow?.n);
  const fail = async (message: string, status: number) => {
    const page = await failure(next, message, status);
    const out = new NextResponse(page.body, { status: page.status, headers: page.headers });
    clearFlow(out, req);
    out.headers.set("Referrer-Policy", "no-referrer");
    out.headers.set("Cache-Control", "no-store");
    return out;
  };
  const error = params.get("error");
  if (error) {
    const code = /^[a-z_]{1,64}$/.test(error) ? error : "error";
    return fail(code === "access_denied"
      ? "Sign-in was cancelled at the identity provider."
      : `The identity provider did not sign you in (${code}).`, 401);
  }
  const state = params.get("state") ?? "";
  const code = params.get("code") ?? "";
  if (!flow || !state || !code || state.length > 256 || code.length > 2048 || !same(state, flow.s)) {
    return fail("This sign-in request expired or was started in another browser. Start again.", 401);
  }
  let answer: Brain;
  try {
    answer = await callOidc("POST", "/team/oidc/callback", { state, code, binding: flow.b });
  } catch {
    return fail("Brain isn’t answering, so single sign-on can’t finish. Start again in a minute.", 503);
  }
  const { status, data } = answer;
  const credential = typeof data?.credential === "string" ? data.credential : "";
  if (status !== 200 || !credential) {
    if (status === 404) return fail("Single sign-on is not enabled on this Brain.", 404);
    if (status === 429) return fail("Too many sign-in attempts. Wait a few minutes, then try again.", 429);
    return fail(detailOf(data, "Single sign-on did not succeed."), status >= 400 && status < 500 ? 403 : 502);
  }
  // An accepted first invitation may have just turned this Brain into a team.
  forgetLoginState();
  const member = await signInMember(credential);
  if (member.status !== 200) {
    return fail(member.status === 503
      ? "Signed in at the identity provider, but Brain can’t confirm the session right now. Try again in a minute."
      : "Signed in at the identity provider, but this dashboard can’t open a member session.", member.status);
  }
  const res = NextResponse.redirect(new URL(next, req.url), 303);
  res.cookies.set(MEMBER_COOKIE, member.cookie, {
    httpOnly: true,
    secure: secure(req),
    sameSite: "lax",
    path: "/",
    maxAge: MEMBER_MAX_AGE,
  });
  res.cookies.delete(SESSION_COOKIE);
  clearFlow(res, req);
  res.headers.set("Referrer-Policy", "no-referrer");
  res.headers.set("Cache-Control", "no-store");
  return res;
}
