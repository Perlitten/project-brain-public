import { NextResponse, type NextRequest } from "next/server";
import { acceptInvitation } from "./lib/invite-accept";
import { invitePage, type InvitePageInput } from "./lib/invite-page";
import { INVITE_PATH } from "./lib/invitations";
import { loginPage } from "./lib/login-page";
import { OIDC_CALLBACK, OIDC_START, oidcCallback, oidcStart, ssoHref, ssoOffered } from "./lib/oidc";
import {
  MEMBER_COOKIE, MEMBER_MAX_AGE, SESSION_COOKIE, authorizeWebRequest, forgetLoginState, lockReason, loginState, memberFor, memberFrom,
  sessionFrom, signIn, signInMember, webAuthResponse,
} from "./lib/web-auth";

// Who may enter is decided in lib/web-auth.ts (WEB_BASIC_AUTH, or the login
// set on Settings → Access → Dashboard sign-in; with neither, open on a loopback-only dashboard or
// with WEB_OPEN_MODE=true, locked everywhere else).
// Browsers get a sign-in
// page and a session cookie; scripts can still send HTTP Basic credentials
// (curl -u user:password).
// In team state each member signs in with their own member key instead; the
// sealed key rides in the pb_member cookie and every request re-checks it.
// Members can also sign in through the team's identity provider (lib/oidc.ts,
// /auth/oidc/start and /auth/oidc/callback) when the Brain offers SSO.

const LOGIN = "/__login";
const LOGOUT = "/__logout";
const MAX_AGE = 60 * 60 * 24 * 30;

// Only same-site paths survive the round trip through the sign-in form.
function safeNext(value: unknown) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  if (value.startsWith(LOGIN) || value.startsWith(LOGOUT) || value.startsWith("/auth/oidc")) return "/";
  return value;
}

async function page(next: string, failed: boolean | "throttled", user = "", team = false, message?: string, status = 401) {
  // The SSO button is offered only to a team whose Brain has single sign-on on.
  const sso = team && (await ssoOffered()) ? ssoHref(next) : undefined;
  return new NextResponse(loginPage({ action: LOGIN, next, failed, user, team, sso, message }), {
    status,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

// A failed single sign-on lands on the member sign-in page with the reason.
const ssoFailure = (next: string, message: string, status: number) => page(next, true, "", true, message, status);

// The member session, set by the sign-in form and by an accepted invitation alike.
function setMemberCookie(res: NextResponse, req: NextRequest, cookie: string) {
  res.cookies.set(MEMBER_COOKIE, cookie, {
    httpOnly: true,
    secure: req.nextUrl.protocol === "https:",
    sameSite: "lax",
    path: "/",
    maxAge: MEMBER_MAX_AGE,
  });
  res.cookies.delete(SESSION_COOKIE);
}

const wantsHtml = (req: NextRequest) => (req.headers.get("accept") ?? "").includes("text/html");

// 503: a readable page for browsers, JSON {error, detail} for everyone else.
async function locked(req: NextRequest) {
  const lock = await lockReason(req.headers.get("host"));
  if (!wantsHtml(req)) return webAuthResponse(503, lock);
  const notice =
    lock?.reason === "unconfigured"
      ? { eyebrow: "Not set up", title: "No sign-in is configured.", detail: lock.detail }
      : lock?.reason === "misconfigured"
        ? { eyebrow: "Misconfigured", title: "This dashboard is misconfigured.", detail: lock.detail }
        : { eyebrow: "Unavailable", title: "Brain isn’t answering.", detail: lock?.detail ?? "Sign-in can’t be checked right now. Try again in a minute." };
  return new NextResponse(loginPage({ action: LOGIN, next: "/", failed: false, notice }), {
    status: 503,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

// A live (or half-configured) dashboard is private. Static pages bake their
// <meta name=robots> at build time, so the header is the runtime guarantee.
const pureDemo = () => !process.env.BRAIN_API_URL?.trim() && !process.env.BRAIN_API_KEY?.trim();

export async function proxy(req: NextRequest) {
  const res = await gate(req);
  if (!pureDemo()) res.headers.set("X-Robots-Tag", "noindex, nofollow");
  return res;
}

async function gate(req: NextRequest): Promise<Response> {
  const { pathname, search } = req.nextUrl;

  if (pathname === LOGOUT) {
    const res = NextResponse.redirect(new URL("/", req.url), 303);
    res.cookies.delete(SESSION_COOKIE);
    res.cookies.delete(MEMBER_COOKIE);
    return res;
  }

  // Single sign-on runs before authorization: it is how a signed-out member gets in.
  if (pathname === OIDC_START) return oidcStart(req, safeNext, ssoFailure);
  if (pathname === OIDC_CALLBACK) return oidcCallback(req, safeNext, ssoFailure);

  if (pathname === INVITE_PATH) return invite(req);

  if (pathname === LOGIN && req.method === "POST") {
    const form = await req.formData().catch(() => null);
    const next = safeNext(form?.get("next"));
    const credential = form?.get("credential");
    if (typeof credential === "string" || (await loginState()).kind === "team") {
      const member = await signInMember(typeof credential === "string" ? credential : "");
      if (member.status === 503) return locked(req);
      if (member.status === 401) return page(next, true, "", true);
      const res = NextResponse.redirect(new URL(next, req.url), 303);
      setMemberCookie(res, req, member.cookie);
      return res;
    }
    const user = form?.get("username");
    const password = form?.get("password");
    const result = await signIn(typeof user === "string" ? user : "", typeof password === "string" ? password : "", req.headers.get("host"));
    if (result.status === 503) return locked(req);
    if (result.status === 401 || result.status === 429) {
      return page(next, result.status === 429 ? "throttled" : true, typeof user === "string" ? user : "");
    }
    const res = NextResponse.redirect(new URL(next, req.url), 303);
    if (result.session) {
      res.cookies.set(SESSION_COOKIE, result.session, {
        httpOnly: true,
        secure: req.nextUrl.protocol === "https:",
        sameSite: "lax",
        path: "/",
        maxAge: MAX_AGE,
      });
    }
    return res;
  }

  const cookie = req.headers.get("cookie");
  const status = await authorizeWebRequest(req.headers.get("authorization"), sessionFrom(cookie), req.headers.get("host"), memberFrom(cookie));
  if (status === 200) return pathname === LOGIN ? NextResponse.redirect(new URL("/", req.url), 303) : NextResponse.next();
  if (status === 503) return locked(req);

  const wantsPage = (req.method === "GET" || req.method === "HEAD") && wantsHtml(req);
  if (!wantsPage) return webAuthResponse(401);
  const team = (await loginState()).kind === "team";
  // A cookie that no longer opens (revoked, expired, re-keyed) is cleared so the browser stops sending it.
  const res = await page(pathname === LOGIN ? "/" : safeNext(pathname + search), false, "", team);
  if (memberFrom(cookie)) res.cookies.delete(MEMBER_COOKIE);
  return res;
}

// ── /invite: an invitee redeems their token and is signed in as the new member ──
// Public on purpose (the token is the credential), and answered here, before
// any layout, server action or server key is involved. Brain decides the
// invitation (lib/invite-accept.ts sends no API key); this only shows the
// outcome and, on success, signs the browser in exactly like the member-key
// form above. The page is extendable with other ways to join through
// `alternatives` (lib/invite-page.ts).

function invitePageResponse(input: InvitePageInput) {
  const { status, html } = invitePage(input);
  return new NextResponse(html, {
    status,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer", "x-frame-options": "DENY" },
  });
}

// A cross-site form must not sign a browser into someone else's membership
// (login CSRF), so the POST has to come from this very page.
function sameOrigin(req: NextRequest) {
  const origin = req.headers.get("origin");
  if (!origin) return req.headers.get("sec-fetch-site") === "same-origin";
  const host = (req.headers.get("x-forwarded-host") ?? req.headers.get("host") ?? req.nextUrl.host).split(",")[0].trim().toLowerCase();
  const protocol = (req.headers.get("x-forwarded-proto") ?? req.nextUrl.protocol).split(",")[0].trim().toLowerCase().replace(/:$/, "");
  if (protocol !== "http" && protocol !== "https") return false;
  try {
    return new URL(origin).origin.toLowerCase() === new URL(`${protocol}://${host}`).origin.toLowerCase();
  } catch {
    return false;
  }
}

async function inviteNotice(): Promise<InvitePageInput["notice"]> {
  const state = await loginState();
  if (state.kind === "demo") {
    return { eyebrow: "Demo data", title: "Invitations need a live Brain.", detail: "This dashboard shows demo data and isn’t connected to a Brain, so there is no team to join here. Open the link your admin sent on the dashboard they use." };
  }
  if (state.kind === "misconfigured") return { eyebrow: "Misconfigured", title: "This dashboard is misconfigured.", detail: state.reason };
  if (process.env.BRAIN_WEB_ACTIONS?.trim() === "0") {
    return { eyebrow: "Switched off", title: "Invitations can’t be accepted here.", detail: "Changes from this dashboard are switched off (BRAIN_WEB_ACTIONS=0). Accept the invitation with the CLI instead: brain team invite accept --api-url <your Brain URL>." };
  }
  return undefined;
}

async function invite(req: NextRequest): Promise<Response> {
  if (req.method !== "GET" && req.method !== "HEAD" && req.method !== "POST") {
    return new NextResponse(null, { status: 405, headers: { allow: "GET, HEAD, POST" } });
  }
  const notice = await inviteNotice();
  if (notice) return invitePageResponse({ notice });
  if (req.method !== "POST") return invitePageResponse({});
  if (!sameOrigin(req)) {
    return invitePageResponse({ notice: { status: 403, eyebrow: "Refused", title: "Accept the invitation from its own page.", detail: "This form came from another site. Open your invitation link in this browser and accept it there." } });
  }
  const form = await req.formData().catch(() => null);
  const raw = form?.get("token");
  const token = typeof raw === "string" ? raw.trim() : "";
  const outcome = await acceptInvitation(token);
  if (outcome.kind === "accepted") {
    // Brain may have just become a team: ask afresh rather than trust the cached solo answer.
    forgetLoginState();
    const member = await signInMember(outcome.key);
    if (member.status !== 200) return invitePageResponse({ outcome, signedIn: false });
    const res = invitePageResponse({ outcome, signedIn: true });
    setMemberCookie(res, req, member.cookie);
    return res;
  }
  if (outcome.kind === "already") {
    // A reload of the success page resubmits the form: say so if this browser is that member.
    const current = await memberFor(memberFrom(req.headers.get("cookie")));
    return invitePageResponse({ outcome, signedIn: current?.name === outcome.name });
  }
  const notTheToken = outcome.kind === "throttled" || outcome.kind === "unavailable" || outcome.kind === "conflict";
  return invitePageResponse({ outcome, token: notTheToken ? token : "" });
}

// robots.txt stays reachable so crawlers can read "Disallow" even on a locked site.
export const config = { matcher: ["/((?!_next/static|_next/image|fonts/|icon.svg|robots.txt).*)"] };
