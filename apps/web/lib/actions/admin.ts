"use server";
// Access: create agent keys, mint more, revoke, disable. A new key comes back
// exactly once in the action result and is never logged or cached.
import { mutate } from "./gate";
import type { ActionResult } from "./types";

const ACCESS = ["/admin", "/setup"];
const AGENT_SCOPES = ["core:write", "jobs:read"];
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 ._@-]{0,119}$/;
const SCOPE_RE = /^[a-z_]+:[a-z_]+$/;

export interface NewKey {
  apiKey: string;
  principalId?: number;
  name?: string;
  scopes: string[];
}

function cleanScopes(scopes: string[]): string[] | null {
  const s = [...new Set(scopes.map((x) => x.trim()).filter(Boolean))];
  return s.length && s.every((x) => SCOPE_RE.test(x)) ? s.sort() : null;
}

function cleanTtl(days?: number): number | undefined {
  return days && Number.isFinite(days) && days > 0 ? Math.min(3650, Math.round(days)) : undefined;
}

/** A new principal with its first key — the usual way to connect an agent. */
export async function createAgentKey(
  name: string,
  scopes: string[] = AGENT_SCOPES,
  opts: { kind?: "agent" | "service" | "human"; ttlDays?: number } = {},
): Promise<ActionResult<NewKey>> {
  const n = name.trim();
  if (!NAME_RE.test(n)) return { ok: false, message: "Name it with letters, digits, spaces, dots, dashes or @ — e.g. “claude-laptop”." };
  const sc = cleanScopes(scopes);
  if (!sc) return { ok: false, message: "Pick at least one permission." };
  const res = await mutate<{ api_key?: string; principal?: { id?: number; name?: string } }>("/admin/principals", {
    body: { name: n, kind: opts.kind ?? "agent", scopes: sc, ttl_days: cleanTtl(opts.ttlDays) },
    success: `Key for “${n}” created. Copy it now — Brain won’t show it again.`,
    revalidate: ACCESS,
  });
  if (!res.ok || !res.data?.api_key) return { ok: false, message: res.ok ? "Brain didn’t return a key." : res.message };
  return {
    ok: true,
    message: res.message,
    data: { apiKey: res.data.api_key, principalId: res.data.principal?.id, name: res.data.principal?.name ?? n, scopes: sc },
  };
}

/** Another key for an existing principal (e.g. rotating before revoking the old one). */
export async function mintKey(principalId: number, scopes: string[], ttlDays?: number): Promise<ActionResult<NewKey>> {
  if (!Number.isInteger(principalId) || principalId <= 0) return { ok: false, message: "That principal id doesn’t look right." };
  const sc = cleanScopes(scopes);
  if (!sc) return { ok: false, message: "Pick at least one permission." };
  const res = await mutate<{ api_key?: string }>(`/admin/principals/${principalId}/credentials`, {
    body: { scopes: sc, ttl_days: cleanTtl(ttlDays) },
    success: "New key created. Copy it now — Brain won’t show it again.",
    revalidate: ACCESS,
  });
  if (!res.ok || !res.data?.api_key) return { ok: false, message: res.ok ? "Brain didn’t return a key." : res.message };
  return { ok: true, message: res.message, data: { apiKey: res.data.api_key, principalId, scopes: sc } };
}

export async function revokeCredential(credentialId: number): Promise<ActionResult> {
  if (!Number.isInteger(credentialId) || credentialId <= 0) return { ok: false, message: "That key id doesn’t look right." };
  const res = await mutate(`/admin/credentials/${credentialId}/revoke`, {
    body: {},
    success: "Key revoked. Anything using it is locked out now.",
    revalidate: ACCESS,
  });
  return { ok: res.ok, message: res.message };
}

export async function disablePrincipal(principalId: number): Promise<ActionResult> {
  if (!Number.isInteger(principalId) || principalId <= 0) return { ok: false, message: "That principal id doesn’t look right." };
  const res = await mutate(`/admin/principals/${principalId}/disable`, {
    body: {},
    success: "Disabled — none of its keys work until you enable it again.",
    revalidate: ACCESS,
  });
  return { ok: res.ok, message: res.message };
}

export async function enablePrincipal(principalId: number): Promise<ActionResult> {
  if (!Number.isInteger(principalId) || principalId <= 0) return { ok: false, message: "That principal id doesn’t look right." };
  const res = await mutate(`/admin/principals/${principalId}/enable`, {
    body: {},
    success: "Enabled — its unrevoked keys work again.",
    revalidate: ACCESS,
  });
  return { ok: res.ok, message: res.message };
}
