"use server";

import { cookies, headers } from "next/headers";
import { revalidatePath } from "next/cache";
import { createAgentKey, type NewKey } from "./admin";
import { MEMBER_COOKIE, MEMBER_MAX_AGE, sealMember, forgetLoginState } from "../web-auth";
import { memberKeyScopes } from "../team-scopes";
import { mutate } from "./gate";
import type { ActionResult } from "./types";

/** Preparation creates a key, never membership: the owner's current sign-in stays usable. */
export async function prepareFirstAdmin(name: string): Promise<ActionResult<NewKey>> {
  return createAgentKey(name, memberKeyScopes(true), { kind: "human", ttlDays: 90 });
}

/** The API verifies possession of the saved human key before retiring shared sign-in. */
export async function activateFirstAdmin(input: { teamName: string; credential: string; saved: boolean }): Promise<ActionResult> {
  if (!input.saved) return { ok: false, message: "Save your administrator key before switching sign-in." };
  const teamName = input.teamName.trim();
  if (!teamName || teamName.length > 128 || !input.credential || input.credential.length > 512) return { ok: false, message: "Provide a team name and the saved administrator credential." };
  const result = await mutate("/team/projects/bootstrap", {
    body: { team_name: teamName, credential: input.credential }, success: "Team enabled. Your saved key is your sign-in.",
  });
  if (!result.ok) return { ok: false, message: result.message };
  forgetLoginState();
  const proto = (await headers()).get("x-forwarded-proto");
  (await cookies()).set(MEMBER_COOKIE, sealMember(input.credential), {
    httpOnly: true, secure: proto ? proto.split(",")[0].trim() === "https" : process.env.NODE_ENV === "production",
    sameSite: "lax", path: "/", maxAge: MEMBER_MAX_AGE,
  });
  revalidatePath("/", "layout");
  return { ok: true, message: result.message };
}
