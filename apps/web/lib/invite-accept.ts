// Redeems an invitation token with Brain for the /invite page (proxy.ts).
// The token is the credential: the request carries no X-API-Key, so the
// dashboard's server key never vouches for an anonymous visitor, and Brain
// alone decides (apps/api/routers/team_invitations.py, POST /team/invitations/accept).
// The visitor's address goes along in X-Forwarded-For so Brain's throttle counts
// each invitee separately instead of the dashboard as one client (lib/client-address.ts).
import { forwardedFor } from "./client-address";
import { acceptOutcome, looksLikeToken, type AcceptOutcome } from "./invitations";

const ACCEPT_PATH = "team/invitations/accept";

export async function acceptInvitation(
  token: string,
  { timeoutMs = 8000, client = null }: { timeoutMs?: number; client?: string | null } = {},
): Promise<AcceptOutcome> {
  const value = token.trim();
  if (!looksLikeToken(value)) return { kind: "malformed" };
  const base = process.env.BRAIN_API_URL?.trim();
  if (!base) return { kind: "unavailable" };
  let url: URL;
  try {
    url = new URL(ACCEPT_PATH, base.endsWith("/") ? base : `${base}/`);
  } catch {
    return { kind: "unavailable" };
  }
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json", ...forwardedFor(client) },
      body: JSON.stringify({ token: value }),
      cache: "no-store",
      redirect: "error",
      signal: AbortSignal.timeout(timeoutMs),
    });
    let data: unknown = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    return acceptOutcome(res.status, data);
  } catch {
    return { kind: "unavailable" };
  }
}
