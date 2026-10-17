// A member who is not a team admin reads one project at a time: Brain totals
// the every-project views (dashboard, findings) only for team admins.
import "server-only";
import { redirect } from "next/navigation";
import { getRepositories } from "./data";
import { restrictedMember } from "./team-session";

/** Without a project in the URL, send a member to the same page for their first readable project. */
export async function scopeMemberToProject(path: string, params: Record<string, string | string[] | undefined>): Promise<void> {
  if (typeof params.repo === "string" && params.repo) return;
  if (!(await restrictedMember())) return;
  const first = (await getRepositories())[0];
  if (!first) return;
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (typeof value === "string" && key !== "repo") query.set(key, value);
  query.set("repo", first.slug);
  redirect(`${path}?${query}`);
}
