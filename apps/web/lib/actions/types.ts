// Shapes shared by server actions and the client components that call them.
// Kept out of the "use server" modules, which may only export async functions.

export interface ActionResult<T = unknown> {
  ok: boolean;
  /** One plain sentence for the toast: what happened, or why it didn't. */
  message: string;
  data?: T;
  /** Set when the action queued a background job the UI should follow. */
  jobId?: string;
}

export type ActionsMode =
  /** No API configured: the screens show demo data and nothing can run. */
  | "demo"
  /** API configured but the site is public: actions are locked. */
  | "locked"
  | "on";

export interface JobSnapshot {
  id: string;
  status: string;
  /** Finished one way or the other: stop polling. */
  done: boolean;
  ok: boolean;
  detail?: string;
  progress?: number;
}
