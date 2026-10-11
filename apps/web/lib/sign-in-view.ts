// What Access → Dashboard sign-in shows: where the login comes from and
// whether it can be changed here.
import "server-only";
import { brainFetch } from "./api";
import type { SignInMode } from "./actions/types";
import { loginState } from "./web-auth";

export interface SignInView {
  mode: SignInMode;
  user: string | null;
  /** WEB_LOGIN_* also set in the API's process environment: it wins on restart. */
  pinned: string[];
}

export async function signInMode(): Promise<SignInMode> {
  const state = await loginState();
  if (state.kind === "brain") return state.mode;
  if (state.kind === "env" || state.kind === "demo") return state.kind;
  return "unknown";
}

export async function getSignInView(): Promise<SignInView> {
  const state = await loginState({ fresh: true });
  if (state.kind !== "brain") return { mode: state.kind === "env" || state.kind === "demo" ? state.kind : "unknown", user: null, pinned: [] };
  const raw = await brainFetch<{ from_env?: unknown }>("/api/web-login", { fresh: true });
  const pinned = Array.isArray(raw?.from_env) ? raw.from_env.filter((k): k is string => typeof k === "string") : [];
  return { mode: state.mode, user: state.user, pinned };
}
