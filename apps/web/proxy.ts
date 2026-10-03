import { NextResponse, type NextRequest } from "next/server";
import { loginPage } from "./lib/login-page";
import { SESSION_COOKIE, authorizeWebRequest, sessionFrom, signIn, webAuthResponse } from "./lib/web-auth";

// Live data requires authentication. An unconfigured demo may be public.
// Browsers get a sign-in page and a session cookie; scripts can still send
// HTTP Basic credentials (curl -u user:password).

const LOGIN = "/__login";
const LOGOUT = "/__logout";
const MAX_AGE = 60 * 60 * 24 * 30;

// Only same-site paths survive the round trip through the sign-in form.
function safeNext(value: unknown) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/\\")) return "/";
  if (value.startsWith(LOGIN) || value.startsWith(LOGOUT)) return "/";
  return value;
}

function page(next: string, failed: boolean, user = "") {
  return new NextResponse(loginPage({ action: LOGIN, next, failed, user }), {
    status: 401,
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" },
  });
}

export async function proxy(req: NextRequest) {
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
    const result = signIn(typeof user === "string" ? user : "", typeof password === "string" ? password : "");
    if (result.status === 503) return webAuthResponse(503);
    if (result.status === 401) return page(next, true, typeof user === "string" ? user : "");
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

  const status = authorizeWebRequest(req.headers.get("authorization"), sessionFrom(req.headers.get("cookie")));
  if (status === 200) return pathname === LOGIN ? NextResponse.redirect(new URL("/", req.url), 303) : NextResponse.next();
  if (status === 503) return webAuthResponse(503);

  const wantsPage = (req.method === "GET" || req.method === "HEAD") && (req.headers.get("accept") ?? "").includes("text/html");
  return wantsPage ? page(pathname === LOGIN ? "/" : safeNext(pathname + search), false) : webAuthResponse(401);
}

export const config = { matcher: ["/((?!_next/static|_next/image|fonts/|icon.svg).*)"] };
