import Link from "next/link";
import type { ReactElement } from "react";
import { Chip, PageHead, Panel } from "./ui";
import { restrictedMember } from "@/lib/team-session";

/** In team state a member who is not a team admin gets this page in place of an admin-only one, rather than errors from every refused read. */
export async function adminOnlyPage(title: string): Promise<ReactElement | null> {
  const member = await restrictedMember();
  if (!member) return null;
  return (
    <>
      <PageHead title={title} />
      <Panel id="admins-only" title="Team admins only" spine="info">
        <div className="signin__state">
          <Chip tone="info">Team member</Chip>
          <b>Signed in as {member.name}</b>
          <p>This page covers the whole Brain server, so only team admins can open it. Your projects, their memory and context packs are in Projects and Memory.</p>
          <Link className="btn btn--neutral" href="/projects">Open your projects</Link>
        </div>
      </Panel>
    </>
  );
}
