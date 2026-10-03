import { createHash, createHmac, timingSafeEqual } from "node:crypto";

export const SESSION_COOKIE = "pb_session";

const digest = (value: string) => createHash("sha256").update(value).digest();
const same = (a: string, b: string) => timingSafeEqual(digest(a), digest(b));

// Live data requires authentication; an unconfigured demo may be public.
// Either a fixed answer (open: 200, misconfigured: 503) or the
// "user:password" pair every check compares against.
function gate(): { status: 200 | 503 } | { expected: string } {
  const expected = process.env.WEB_BASIC_AUTH?.trim();
  const live = Boolean(process.env.BRAIN_API_URL?.trim() && process.env.BRAIN_API_KEY?.trim());
  if (!expected) return { status: live ? 503 : 200 };
  const separator = expected.indexOf(":");
  if (separator < 1 || separator === expected.length - 1) return { status: 503 };
  return { expected };
}

// The browser session cookie is an HMAC keyed by the credentials, so changing
// WEB_BASIC_AUTH signs every browser out.
const sessionFor = (expected: string) => createHmac("sha256", expected).update("project-brain-web-session-v1").digest("base64url");

/** Reads the session cookie from a raw Cookie header. */
export function sessionFrom(cookieHeader: string | null): string | null {
  for (const part of (cookieHeader ?? "").split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === SESSION_COOKIE) return rest.join("=");
  }
  return null;
}

export function authorizeWebRequest(header: string | null, session: string | null = null): 200 | 401 | 503 {
  const g = gate();
  if ("status" in g) return g.status;
  if (session && same(session, sessionFor(g.expected))) return 200;
  const match = /^Basic\s+(\S+)$/i.exec(header ?? "");
  if (!match) return 401;
  try {
    return same(Buffer.from(match[1], "base64").toString("utf8"), g.expected) ? 200 : 401;
  } catch {
    return 401;
  }
}

/** Checks a sign-in form; on success returns the session cookie value to set. */
export function signIn(user: string, password: string): { status: 401 } | { status: 503 } | { status: 200; session: string | null } {
  const g = gate();
  if ("status" in g) return g.status === 200 ? { status: 200, session: null } : { status: 503 };
  return same(`${user}:${password}`, g.expected) ? { status: 200, session: sessionFor(g.expected) } : { status: 401 };
}

export function webAuthResponse(status: 401 | 503): Response {
  return new Response(status === 401 ? "Authentication required" : "Live dashboard authentication is not configured", {
    status,
    headers: {
      "Cache-Control": "no-store",
      ...(status === 401 ? { "WWW-Authenticate": 'Basic realm="Project Brain"' } : {}),
    },
  });
}
