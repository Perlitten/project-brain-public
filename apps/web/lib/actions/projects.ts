"use server";
import { brainRead } from "../api";
import { getCapabilities } from "../capabilities";
import { mutate } from "./gate";
import { startReindex } from "./jobs";
import type { ActionResult } from "./types";

type RegistrationContext = { team: boolean; canIndex: boolean; projects: { id: string; name: string; canIndex: boolean }[] };

export async function projectRegistrationContext(): Promise<ActionResult<RegistrationContext>> {
  const capabilities = await getCapabilities();
  if (capabilities.state === "demo") return { ok: false, message: "Connect this dashboard to a Brain server before adding a repository." };
  if (!capabilities.supported.includes("repository_registration")) return { ok: false, message: "This server does not advertise repository registration. Update the API first." };
  const me = await brainRead<{ team_state?: string; scopes?: string[]; is_team_admin?: boolean; projects?: { id?: string; effective_permissions?: string[] }[] }>("/team/me", { fresh: true });
  if (!me.ok || !["solo", "team"].includes(me.data.team_state ?? "")) return { ok: false, message: "Brain did not confirm solo/team state. Retry before adding a repository." };
  if (me.data.team_state === "solo" && (!Array.isArray(me.data.scopes) || !me.data.scopes.some(scope => scope === "*" || scope === "workspace:write"))) return { ok: false, message: "This solo credential lacks workspace:write. Ask the server operator for a replacement credential with repository registration permission." };
  if (me.data.team_state === "team" && me.data.is_team_admin !== true) return { ok: false, message: "A team administrator must add repositories." };
  if (me.data.team_state === "solo") return { ok: true, message: "", data: { team: false, canIndex: true, projects: [] } };
  const projects = await brainRead<{ id: string; name: string; disabled_at?: string | null }[]>("/team/projects/", { fresh: true });
  if (!projects.ok || !Array.isArray(projects.data)) return { ok: false, message: "Project choices unavailable. A team administrator must select an active project." };
  const runnable = new Set((me.data.projects ?? [])
    .filter(project => Array.isArray(project.effective_permissions) && project.effective_permissions.includes("run.request"))
    .map(project => project.id));
  return { ok: true, message: "", data: {
    team: true,
    canIndex: false,
    projects: projects.data.filter(p => !p.disabled_at).map(p => ({ id: p.id, name: p.name, canIndex: runnable.has(p.id) })),
  } };
}

export async function addProject(input: { name: string; path: string; projectId?: string; indexNow: boolean }): Promise<ActionResult<{ registered: true; indexed: boolean; path: string; canIndex: boolean }>> {
  const name = input.name.trim(), path = input.path.trim();
  if (!name || name.length > 120) return { ok: false, message: "Use a project name of 1–120 characters." };
  if (!path || path.length > 1024 || /[\x00-\x1f]/.test(path) || !/^(?:\/|[A-Za-z]:[\\/]|\\\\)/.test(path) || path.split(/[\\/]/).includes("..")) return { ok: false, message: "Use an absolute repository folder path without parent traversal or control characters." };
  const context = await projectRegistrationContext();
  if (!context.ok || !context.data) return { ok: false, message: context.message };
  const selectedProject = context.data.team ? context.data.projects.find(p => p.id === input.projectId) : undefined;
  if (context.data.team && !selectedProject) return { ok: false, message: "Choose an active team project for this repository." };
  const canIndex = context.data.team ? selectedProject?.canIndex === true : context.data.canIndex;
  if (input.indexNow && !canIndex) return { ok: false, message: "Your role and this project’s policy do not grant run.request. Add the repository without indexing, then ask a Contributor or Reviewer with run permission to start its first index." };
  const result = await mutate("/workspace/repositories", {
    body: { display_name: name, path, project_id: context.data.team ? input.projectId : undefined, create_index_record: true },
    success: "Repository registered. Existing projects and the default agent repository are unchanged.",
    revalidate: ["/projects", "/"],
  });
  if (!result.ok) return { ok: false, message: result.message };
  const index = input.indexNow ? await startReindex({ repoPath: path }) : null;
  return { ok: true, message: input.indexNow ? index?.ok ? "Repository registered; first index queued." : `Repository registered, but indexing did not start: ${index?.message}` : canIndex ? "Repository registered. Start its first index when ready." : "Repository registered. A Contributor or Reviewer with run permission can start its first index.",
    data: { registered: true, indexed: index?.ok === true, path, canIndex }, jobId: index?.jobId };
}
