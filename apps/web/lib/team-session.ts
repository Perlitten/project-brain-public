// Who the request being rendered is signed in as, in team state. Server-only:
// the sealed member key stays in its httpOnly cookie; pages get a name and
// whether to offer team administration, which Brain still checks per call.
import "server-only";
import { cookies } from "next/headers";
import { signInMode } from "./sign-in-view";
import { MEMBER_COOKIE, memberFor, type WebMember } from "./web-auth";

export async function currentMember(): Promise<WebMember | null> {
  let sealed: string | undefined;
  try {
    sealed = (await cookies()).get(MEMBER_COOKIE)?.value;
  } catch {
    return null;
  }
  return sealed ? memberFor(sealed) : null;
}

/** The signed-in team member when they are not a team admin; Brain refuses them the admin-only pages. */
export async function restrictedMember(): Promise<WebMember | null> {
  const [mode, member] = await Promise.all([signInMode(), currentMember()]);
  return mode === "team" && member && !member.admin ? member : null;
}
