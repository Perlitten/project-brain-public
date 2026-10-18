import { isIP } from "node:net";

// The visitor's address for Brain's per-client throttles (invitation acceptance
// and single sign-on). Those calls come from this server, so without it Brain
// counts every visitor as the dashboard. The address is the one the dashboard's
// own front proxy saw (Vercel, or nginx with $proxy_add_x_forwarded_for): the
// rightmost X-Forwarded-For entry, which that proxy wrote, else X-Real-IP.
// Brain believes the forwarded value only from an address listed in its
// BRAIN_TRUSTED_PROXY_IPS (docs/operations/TEAM_INVITATIONS.md).

export function clientAddress(headers: Headers): string | null {
  const chain = (headers.get("x-forwarded-for") ?? "").split(",").map((hop) => hop.trim()).filter(Boolean);
  for (const candidate of [chain.at(-1), headers.get("x-real-ip")?.trim()]) {
    if (candidate && isIP(candidate)) return candidate;
  }
  return null;
}

/** Request headers that pass the visitor's address on to Brain (none when it is unknown). */
export const forwardedFor = (address: string | null | undefined): Record<string, string> =>
  address ? { "X-Forwarded-For": address } : {};
