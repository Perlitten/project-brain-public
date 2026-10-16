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

/** How the dashboard is entered (lib/web-auth.ts). */
export type SignInMode =
  /** No API: demo data, nothing to protect. */
  | "demo"
  /** No password set yet and open mode (loopback-only, or WEB_OPEN_MODE=true): anyone with the address gets in. */
  | "open"
  /** Login and password set on Access → Dashboard sign-in. */
  | "password"
  /** Fixed by WEB_BASIC_AUTH in the deployment environment. */
  | "env"
  /** The Brain has team members: each signs in with their own member key. */
  | "team"
  /** Brain isn't answering, or the deployment is misconfigured. */
  | "unknown";

export interface JobSnapshot {
  id: string;
  status: string;
  /** Finished one way or the other: stop polling. */
  done: boolean;
  ok: boolean;
  detail?: string;
  progress?: number;
  /** Queued while no worker is running: it will not start on its own. */
  stalled?: boolean;
}
