"use server";
// Connect a repository from its git URL, and sync one that is already
// connected. The token typed in the form goes to the API once, in the request
// body over TLS; the API never stores or returns it (see routers/git_connect.py).
import { parseGitRemote, probeSummary, type GitProbe } from "../git-remote";
import { mutate } from "./gate";
import { projectRegistrationContext } from "./projects";
import type { ActionResult } from "./types";

export interface GitConnectData {
  job_id: string;
  status: string;
  path: string;
  name: string;
  provider: string;
  display_url: string;
  branch: string;
  project_id?: string;
}

const CONTROL = /[\x00-\x1f\x7f]/;

function cleanToken(token?: string): { token?: string; problem?: string } {
  const t = token?.trim();
  if (!t) return {};
  if (t.length > 512 || CONTROL.test(t)) return { problem: "That token is not usable: it is too long or contains control characters." };
  return { token: t };
}

export async function probeRemote(input: { url: string; token?: string }): Promise<ActionResult<GitProbe>> {
  const guess = parseGitRemote(input.url);
  if (!guess.valid) return { ok: false, message: guess.problem ?? "Invalid git URL." };
  const token = cleanToken(input.token);
  if (token.problem) return { ok: false, message: token.problem };
  const res = await mutate<GitProbe>("/git/probe", {
    body: { url: input.url.trim(), token: token.token },
    success: probeSummary,
    timeoutMs: 60_000,
  });
  if (!res.ok || !res.data) return res;
  // The API answers 200 for an unreachable remote too; the form needs the details either way.
  return res.data.reachable ? res : { ok: false, message: probeSummary(res.data), data: res.data };
}

export async function connectRepository(input: {
  url: string;
  branch?: string;
  token?: string;
  name?: string;
  projectId?: string;
  indexNow: boolean;
}): Promise<ActionResult<GitConnectData>> {
  const guess = parseGitRemote(input.url);
  if (!guess.valid) return { ok: false, message: guess.problem ?? "Invalid git URL." };
  const token = cleanToken(input.token);
  if (token.problem) return { ok: false, message: token.problem };
  const branch = input.branch?.trim() || undefined;
  if (branch && (branch.length > 200 || branch.startsWith("-") || CONTROL.test(branch) || /\s/.test(branch))) return { ok: false, message: "That branch name is not valid." };
  const name = input.name?.trim() || undefined;
  if (name && name.length > 120) return { ok: false, message: "Use a project name of up to 120 characters." };

  const context = await projectRegistrationContext();
  if (!context.ok || !context.data) return { ok: false, message: context.message };
  const selectedProject = context.data.team ? context.data.projects.find(p => p.id === input.projectId) : undefined;
  if (context.data.team && !selectedProject) return { ok: false, message: "Choose an active team project for this repository." };
  const canIndex = context.data.team ? selectedProject?.canIndex === true : context.data.canIndex;
  if (input.indexNow && !canIndex) return { ok: false, message: "Your role and this project’s policy do not grant run.request. Connect the repository without indexing, then ask a Contributor or Reviewer with run permission to start its first index." };

  return mutate<GitConnectData>("/git/connect", {
    body: {
      url: input.url.trim(),
      branch,
      token: token.token,
      display_name: name,
      project_id: context.data.team ? input.projectId : undefined,
      index_now: input.indexNow,
    },
    success: d => `Cloning ${d.display_url} (${d.branch})${input.indexNow ? ", then indexing it" : ""}.`,
    revalidate: ["/projects", "/"],
    timeoutMs: 90_000,
  });
}

/** Fetch a connected repository's origin now and reindex it if origin moved. */
export async function syncRepository(repoPath: string): Promise<ActionResult> {
  const path = repoPath.trim();
  if (!path || path.length > 1000 || CONTROL.test(path)) return { ok: false, message: "This repository has no usable path to sync." };
  return mutate("/jobs/origin-poll", {
    body: { repo_path: path },
    success: "Checking origin for new commits; the index follows if it moved.",
    revalidate: ["/projects"],
  });
}
