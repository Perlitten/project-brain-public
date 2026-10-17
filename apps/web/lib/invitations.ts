// Team invitations, shared by the Team page (admins issue and revoke them) and
// the /invite page an invitee opens (proxy.ts, lib/invite-page.ts). Pure: no
// server-only imports, so client components and tests can use it too.
//
// The invitation token (pbi_…) is a bearer secret. Links carry it in the URL
// fragment (#token=…), which browsers never send to a server, so neither the
// dashboard's nor a proxy's access log ever records it; the page moves it
// into a form field and clears the address bar.

/** Where an invitee redeems an invitation. Served by proxy.ts without sign-in. */
export const INVITE_PATH = "/invite";

/** brain/team/invitations.py: TOKEN_PREFIX + token_urlsafe(32); MAX_TOKEN_LENGTH 128. */
export const TOKEN_RE = /^pbi_[A-Za-z0-9_-]{8,124}$/;

export const looksLikeToken = (value: unknown): value is string => typeof value === "string" && TOKEN_RE.test(value.trim());

/** The link to send an invitee: the token rides in the fragment, never in the request. */
export function inviteLink(origin: string, token: string): string {
  return `${origin.replace(/\/+$/, "")}${INVITE_PATH}#token=${encodeURIComponent(token)}`;
}

/** The token from a link's fragment (`#token=pbi_…` or a bare `#pbi_…`), or null. */
export function tokenFromFragment(hash: string): string | null {
  const raw = hash.replace(/^#/, "");
  let value = raw;
  for (const part of raw.split("&")) {
    const at = part.indexOf("=");
    if (at > 0 && part.slice(0, at) === "token") value = part.slice(at + 1);
  }
  try {
    value = decodeURIComponent(value);
  } catch {
    return null;
  }
  return looksLikeToken(value) ? value.trim() : null;
}

/** Derived by Brain (brain/team/invitations.py derive_state); never stored. */
export const INVITATION_STATES = ["invited", "verified", "active", "expired", "revoked"] as const;
export type InvitationState = (typeof INVITATION_STATES)[number];

export const STATE_TONE: Record<InvitationState, "info" | "ok" | "warn" | "idle"> = {
  invited: "info",
  verified: "ok",
  active: "ok",
  expired: "warn",
  revoked: "idle",
};

export const STATE_NOTE: Record<InvitationState, string> = {
  invited: "Waiting for the invitee to accept it.",
  verified: "Accepted; the member key hasn’t been used yet.",
  active: "Accepted, and the member has used their key.",
  expired: "Not accepted in time. Revoke it and send a new one.",
  revoked: "Revoked, or the member it created was revoked.",
};

/** Accepted invitations can be revoked only together with the member they created (the API refuses otherwise). */
export const revokesMember = (state: InvitationState) => state === "verified" || state === "active";

/** Choices for the invitation's lifetime, in hours (API default 72, at most 720). */
export const INVITE_EXPIRY_CHOICES = [
  { hours: 24, label: "1 day" },
  { hours: 72, label: "3 days" },
  { hours: 168, label: "7 days" },
  { hours: 720, label: "30 days" },
] as const;

/** What the invitee sees after asking Brain to accept a token. */
export type AcceptOutcome =
  /** Accepted now: the member exists and `key` is shown this one time. */
  | { kind: "accepted"; name: string; key: string; admin: boolean; roles: { projectId: string; role: string }[] }
  /** The same token again after a success: Brain returns the member but never the key twice. */
  | { kind: "already"; name: string }
  /** Unknown, expired or revoked: Brain gives one answer for all three on purpose. */
  | { kind: "rejected" }
  /** Not shaped like an invitation token at all; Brain wasn't asked. */
  | { kind: "malformed" }
  | { kind: "throttled" }
  /** Brain explained why it can't accept right now (inviter no longer admin, name taken, restore fence, …). */
  | { kind: "conflict"; message: string }
  /** Brain didn't answer, or answered something unreadable: not a verdict on the token. */
  | { kind: "unavailable" };

/** Maps POST /team/invitations/accept (apps/api/routers/team_invitations.py) to an outcome. */
export function acceptOutcome(status: number, data: unknown): AcceptOutcome {
  const body = data && typeof data === "object" ? (data as Record<string, unknown>) : {};
  const detail = typeof body.detail === "string" ? body.detail : "";
  if (status === 200) {
    const member = body.member && typeof body.member === "object" ? (body.member as Record<string, unknown>) : {};
    const invitation = body.invitation && typeof body.invitation === "object" ? (body.invitation as Record<string, unknown>) : {};
    const name = typeof member.principal_name === "string" && member.principal_name
      ? member.principal_name
      : typeof invitation.principal_name === "string" && invitation.principal_name ? invitation.principal_name : "";
    if (!name) return { kind: "unavailable" };
    if (typeof body.credential === "string" && body.credential) {
      const roles = Array.isArray(body.project_memberships)
        ? body.project_memberships.flatMap((m) => {
            const row = m && typeof m === "object" ? (m as Record<string, unknown>) : {};
            return typeof row.project_id === "string" && typeof row.role === "string" ? [{ projectId: row.project_id, role: row.role }] : [];
          })
        : [];
      return { kind: "accepted", name, key: body.credential, admin: member.is_admin === true || invitation.is_admin === true, roles };
    }
    return { kind: "already", name };
  }
  if (status === 429) return { kind: "throttled" };
  // 403 "Invitation token is invalid, expired or revoked"; 422 is a token Brain can't even parse.
  if (status === 403 || status === 422) return { kind: "rejected" };
  if ((status === 404 || status === 409) && detail) return { kind: "conflict", message: detail };
  return { kind: "unavailable" };
}
