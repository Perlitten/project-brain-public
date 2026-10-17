"use server";
// Settings → Access → Dashboard sign-in: set, change or remove the dashboard's login. The Brain
// API keeps it (scrypt hash in the server's .env); this browser gets a fresh
// session cookie so it stays signed in while every other browser is signed out.
import { cookies, headers } from "next/headers";
import { revalidatePath } from "next/cache";
import { apiConfigured, brainSend } from "../api";
import { SESSION_COOKIE, forgetLoginState, loginState, openMode, sessionForLogin } from "../web-auth";
import type { ActionResult } from "./types";

type LoginReply = { mode: "open" | "password"; user: string | null; version: string };

const MAX_AGE = 60 * 60 * 24 * 30;

// Once a login is set, changing it takes the current password — the API checks
// it. Setting the FIRST password (Brain keeps none yet) is allowed only in open
// mode (a loopback-only dashboard opened from this machine, or WEB_OPEN_MODE=true),
// so an anonymous visitor can never claim a fresh public deployment; otherwise
// the owner sets it via WEB_BASIC_AUTH or the API.
// Never when the dashboard runs on demo data or WEB_BASIC_AUTH.
async function blocked(): Promise<ActionResult | null> {
  if (!apiConfigured) return { ok: false, message: "Connect the Brain API first — there is nothing to protect on demo data." };
  if (process.env.BRAIN_WEB_ACTIONS?.trim() === "0") return { ok: false, message: "Changes from the dashboard are switched off (BRAIN_WEB_ACTIONS=0)." };
  const state = await loginState({ fresh: true });
  if (state.kind === "env") return { ok: false, message: "Sign-in is set by WEB_BASIC_AUTH in the deployment environment — change it there." };
  if (state.kind === "misconfigured") return { ok: false, message: state.reason };
  if (state.kind !== "brain") return { ok: false, message: "Brain is unreachable right now, so the login can’t be changed." };
  if (state.mode === "open" && !openMode((await headers()).get("host")))
    return { ok: false, message: "No login is set and this dashboard is reachable beyond this machine — set the first password via WEB_BASIC_AUTH or the Brain API, or open the dashboard on 127.0.0.1." };
  return null;
}

async function keepThisBrowserSignedIn(reply: LoginReply) {
  forgetLoginState();
  const jar = await cookies();
  if (reply.mode === "password" && reply.user) {
    const proto = (await headers()).get("x-forwarded-proto");
    jar.set(SESSION_COOKIE, sessionForLogin(reply.user, reply.version), {
      httpOnly: true,
      secure: proto ? proto.split(",")[0].trim() === "https" : process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: "/",
      maxAge: MAX_AGE,
    });
  }
  // Password removed: keep the old cookie. Open sign-in ignores it, and another
  // instance that hasn't heard of the change yet still lets this browser in.
  revalidatePath("/", "layout");
}

export async function setWebLogin(input: { user: string; password: string; currentPassword?: string }): Promise<ActionResult<LoginReply>> {
  const stop = await blocked();
  if (stop) return stop as ActionResult<LoginReply>;
  const res = await brainSend<LoginReply>("PUT", "/api/web-login", {
    user: input.user,
    password: input.password,
    ...(input.currentPassword ? { current_password: input.currentPassword } : {}),
  });
  if (!res.ok || !res.data) {
    const message = res.status === 404 ? "This Brain server is too old for dashboard sign-in — update it first." : (res.error ?? "Brain didn’t confirm the change.");
    return { ok: false, message };
  }
  await keepThisBrowserSignedIn(res.data);
  return { ok: true, message: `Saved. Sign in as “${res.data.user}” from now on; other browsers were signed out.`, data: res.data };
}

export async function disableWebLogin(input: { currentPassword: string }): Promise<ActionResult<LoginReply>> {
  const stop = await blocked();
  if (stop) return stop as ActionResult<LoginReply>;
  // Without open mode, no login means a locked dashboard — including for this browser.
  if (!openMode((await headers()).get("host")))
    return { ok: false, message: "Removing the password would lock the dashboard for everyone (it is reachable beyond this machine). Change the password instead." };
  const res = await brainSend<LoginReply>("POST", "/api/web-login/disable", { current_password: input.currentPassword });
  if (!res.ok || !res.data) return { ok: false, message: res.error ?? "Brain didn’t confirm the change." };
  await keepThisBrowserSignedIn(res.data);
  return { ok: true, message: "Password removed. Anyone who can open this address can now use the dashboard.", data: res.data };
}
