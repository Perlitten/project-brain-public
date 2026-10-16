// Who the request being rendered is signed in as, in team state. Server-only:
// the sealed member key stays in its httpOnly cookie; pages get a name and
// whether to offer team administration, which Brain still checks per call.
import "server-only";
import { cookies } from "next/headers";
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
