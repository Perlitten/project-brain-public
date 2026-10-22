import type { ReadResult } from "./api";

export interface ReadProblem { title: string; detail: string; retry: boolean }

/** A failed observation says nothing about whether the server has been started. */
export function readProblem(result: Extract<ReadResult<unknown>, { ok: false }>): ReadProblem {
  if (result.status === 401) return { title: "Sign-in required", detail: "Your credential was refused. Sign in again; if this persists, ask an administrator to check the dashboard credential.", retry: false };
  if (result.status === 403) return { title: "Permission required", detail: "Brain answered, but this credential cannot read this information. Check its scopes and project role under Access or ask a team administrator.", retry: false };
  switch (result.reason) {
    case "timeout": return { title: "Brain is taking too long", detail: "This request timed out. Setup may already be complete; retry before changing or restarting services.", retry: true };
    case "network": return { title: "Connection failed", detail: "The dashboard could not reach Brain. Retry, then check the API address, network and server logs if it persists.", retry: true };
    case "not_configured": return { title: "Brain is not connected", detail: "An operator must configure the dashboard's Brain API address and credential.", retry: false };
    case "not_found": return { title: "Not available on this server", detail: "This server does not provide this endpoint. Check that the dashboard and API versions match.", retry: false };
    case "invalid_response": return { title: "Unreadable response", detail: "Brain returned an unexpected response. Retry and check the API logs if it persists.", retry: true };
    default: return { title: `Brain returned HTTP ${result.status}`, detail: "The request failed. Retry and check the API logs if it persists; this does not establish that Brain is stopped.", retry: result.status >= 500 };
  }
}
