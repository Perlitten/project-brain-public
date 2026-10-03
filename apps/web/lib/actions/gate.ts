// Server-only policy for anything that changes Brain. Every server action
// calls `mutate()` (or `guard()` first), so the rule lives in one place.
import "server-only";
import { revalidatePath } from "next/cache";
import { apiConfigured, brainSend } from "../api";
import type { ActionResult, ActionsMode } from "./types";

/**
 * Actions run only where the dashboard itself is protected: behind the
 * Basic-auth gate (WEB_BASIC_AUTH), in local development, or when the operator
 * opts in explicitly with BRAIN_WEB_ACTIONS=1 (e.g. behind their own SSO).
 * BRAIN_WEB_ACTIONS=0 switches them off everywhere.
 */
export function actionsMode(): ActionsMode {
  if (!apiConfigured) return "demo";
  const flag = process.env.BRAIN_WEB_ACTIONS?.trim();
  if (flag === "0") return "locked";
  if (flag === "1" || process.env.WEB_BASIC_AUTH || process.env.NODE_ENV === "development") return "on";
  return "locked";
}

export function guard(): ActionResult | null {
  const mode = actionsMode();
  if (mode === "demo") return { ok: false, message: "Connect the Brain API to run actions — this is demo data." };
  if (mode === "locked")
    return { ok: false, message: "Actions are locked: protect the dashboard with WEB_BASIC_AUTH (or set BRAIN_WEB_ACTIONS=1)." };
  return null;
}

interface MutateOptions<T> {
  method?: "POST" | "PATCH" | "PUT" | "DELETE";
  body?: unknown;
  /** Toast text on success; may read the response. */
  success: string | ((data: T) => string);
  /** Screens to refresh after a success. */
  revalidate?: string[];
  timeoutMs?: number;
}

/** Guarded call into the Brain API, shaped for a toast. */
export async function mutate<T = Record<string, unknown>>(path: string, opts: MutateOptions<T>): Promise<ActionResult<T>> {
  const blocked = guard();
  if (blocked) return blocked as ActionResult<T>;
  const res = await brainSend<T>(opts.method ?? "POST", path, opts.body, opts.timeoutMs);
  if (!res.ok || res.data === null) {
    return { ok: false, message: res.error ?? "Brain didn’t confirm the change." };
  }
  for (const p of opts.revalidate ?? []) revalidatePath(p);
  const data = res.data;
  const jobId = data && typeof data === "object" && typeof (data as unknown as { job_id?: unknown }).job_id === "string" ? (data as unknown as { job_id: string }).job_id : undefined;
  return {
    ok: true,
    message: typeof opts.success === "function" ? opts.success(data) : opts.success,
    data,
    jobId,
  };
}
