import { NextResponse, type NextRequest } from "next/server";
import { loginPage } from "./lib/login-page";
import { SESSION_COOKIE, authorizeWebRequest, lockReason, sessionFrom, signIn, webAuthResponse } from "./lib/web-auth";

// Who may enter is decided in lib/web-auth.ts (WEB_BASIC_AUTH, or the login
// set on Settings → Sign-in; with neither, locked unless WEB_OPEN_MODE=true).
// Browsers get a sign-in
// page and a session cookie; scripts can still send HTTP Basic credentials
// (curl -u user:password).

const LOGIN = "/__login";
const LOGOUT = "/__logout";
const MAX_AGE = 60 * 60 * 24 * 30;

// Only same-site paths survive the round trip through the sign-in form.
function safeNext(value: unknown) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  if (value.startsWith(LOGIN) || value.startsWith(LOGOUT)) return "/";
  return value;
}

function page(next: string, failed: boolean | "throttled", user = "") {
  return new NextResponse(loginPage({ action: LOGIN, next, failed, user }), {
    status: 401,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

const wantsHtml = (req: NextRequest) => (req.headers.get("accept") ?? "").includes("text/html");

// 503: a readable page for browsers, JSON {error, detail} for everyone else.
async function locked(req: NextRequest) {
  const lock = await lockReason();
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
    return res;
  }

  if (pathname === LOGIN && req.method === "POST") {
    const form = await req.formData().catch(() => null);
    const next = safeNext(form?.get("next"));
    const user = form?.get("username");
    const password = form?.get("password");
    const result = await signIn(typeof user === "string" ? user : "", typeof password === "string" ? password : "");
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

  const status = await authorizeWebRequest(req.headers.get("authorization"), sessionFrom(req.headers.get("cookie")));
  if (status === 200) return pathname === LOGIN ? NextResponse.redirect(new URL("/", req.url), 303) : NextResponse.next();
  if (status === 503) return locked(req);

  const wantsPage = (req.method === "GET" || req.method === "HEAD") && wantsHtml(req);
  return wantsPage ? page(pathname === LOGIN ? "/" : safeNext(pathname + search), false) : webAuthResponse(401);
}

// robots.txt stays reachable so crawlers can read "Disallow" even on a locked site.
export const config = { matcher: ["/((?!_next/static|_next/image|fonts/|icon.svg|robots.txt).*)"] };
