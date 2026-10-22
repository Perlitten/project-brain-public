"use server";
import { brainRead } from "../api";
import { getCapabilities } from "../capabilities";
import { mutate } from "./gate";
import { startReindex } from "./jobs";
import type { ActionResult } from "./types";

export async function projectRegistrationContext(): Promise<ActionResult<{ team: boolean; projects: { id: string; name: string }[] }>> {
  if (!(await getCapabilities()).supported.includes("repository_registration")) return { ok: false, message: "This server does not advertise repository registration. Update the API first." };
  const me = await brainRead<{ team_state?: string }>("/team/me", { fresh: true });
  if (!me.ok || !["solo", "team"].includes(me.data.team_state ?? "")) return { ok: false, message: "Brain did not confirm solo/team state. Retry before adding a repository." };
  if (me.data.team_state === "solo") return { ok: true, message: "", data: { team: false, projects: [] } };
  const projects = await brainRead<{ id: string; name: string; disabled_at?: string | null }[]>("/team/projects/", { fresh: true });
  if (!projects.ok || !Array.isArray(projects.data)) return { ok: false, message: "Project choices unavailable. A team administrator must select an active project." };
  return { ok: true, message: "", data: { team: true, projects: projects.data.filter(p => !p.disabled_at).map(p => ({ id: p.id, name: p.name })) } };
}

export async function addProject(input: { name: string; path: string; projectId?: string; indexNow: boolean }): Promise<ActionResult<{ registered: true; indexed: boolean; path: string }>> {
  const name = input.name.trim(), path = input.path.trim();
  if (!name || name.length > 120) return { ok: false, message: "Use a project name of 1–120 characters." };
  if (!path || path.length > 1024 || /[\x00-\x1f]/.test(path) || !/^(?:\/|[A-Za-z]:[\\/]|\\\\)/.test(path) || path.split(/[\\/]/).includes("..")) return { ok: false, message: "Use an absolute repository folder path without parent traversal or control characters." };
  const context = await projectRegistrationContext();
  if (!context.ok || !context.data) return { ok: false, message: context.message };
  if (context.data.team && !context.data.projects.some(p => p.id === input.projectId)) return { ok: false, message: "Choose an active team project for this repository." };
  const result = await mutate("/workspace/repositories", {
    body: { display_name: name, path, project_id: context.data.team ? input.projectId : undefined, create_index_record: true },
    success: "Repository registered. Existing projects and the default agent repository are unchanged.",
    revalidate: ["/projects", "/"],
  });
  if (!result.ok) return { ok: false, message: result.message };
  const index = input.indexNow ? await startReindex({ repoPath: path }) : null;
  return { ok: true, message: input.indexNow ? index?.ok ? "Repository registered; first index queued." : `Repository registered, but indexing did not start: ${index?.message}` : "Repository registered. Start its first index when ready.",
    data: { registered: true, indexed: index?.ok === true, path }, jobId: index?.jobId };
}
