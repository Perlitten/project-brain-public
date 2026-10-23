"use client";
import { useEffect, useState } from "react";
import { Dialog, LockNote, RunButton, useAction } from "./act";
import { useCapabilities } from "./Capabilities";
import { useRestrictedMember } from "./MemberScope";
import { addProject, projectRegistrationContext } from "@/lib/actions/projects";
import { startReindex } from "@/lib/actions/jobs";

export function AddProject() {
  const capabilities = useCapabilities();
  const restricted = useRestrictedMember();
  const [open, setOpen] = useState(false);
  if (capabilities.state === "demo" || !capabilities.supported.includes("repository_registration") || restricted) return null;
  return <><button className="btn btn--primary" type="button" onClick={() => setOpen(true)}>Add project</button><Dialog open={open} onClose={() => setOpen(false)} title="Add project">{open && <AddProjectForm />}</Dialog></>;
}
export function AddProjectForm() {
  const [name, setName] = useState(""); const [path, setPath] = useState(""); const [project, setProject] = useState(""); const [indexNow, setIndexNow] = useState(false);
  const [context, setContext] = useState<Awaited<ReturnType<typeof projectRegistrationContext>> | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof addProject>> | null>(null);
  const { run, pending, mode } = useAction(addProject, { title: "Add project" });
  useEffect(() => { let cancelled = false; projectRegistrationContext().then(r => { if (!cancelled) setContext(r); }).catch(() => { if (!cancelled) setContext({ ok: false, message: "Project choices unavailable. Close and retry." }); }); return () => { cancelled = true; }; }, []);
  const canIndex = context?.data?.team
    ? context.data.projects.find(candidate => candidate.id === project)?.canIndex === true
    : context?.data?.canIndex === true;
  useEffect(() => { if (!canIndex) setIndexNow(false); }, [canIndex]);
  if (result?.data?.registered) return <div role="status"><p>{result.message}</p>{!result.data.indexed && result.data.canIndex && <RunButton action={() => startReindex({ repoPath: result.data!.path })} label="Index now" title="Start the first index" />}<p>The folder must remain readable inside both API and worker containers.</p></div>;
  return <form className="form form--wide" onSubmit={async e => { e.preventDefault(); setResult(await run({ name, path, projectId: project || undefined, indexNow })); }}>
    <p>Register another folder on the Brain server. This does not replace the default repository for agents. The API checks the folder, Git layout, duplicate registration and your permissions.</p>
    <label className="field">Project name<input className="input" required maxLength={120} value={name} onChange={e => setName(e.target.value)} /></label>
    <label className="field">Repository folder on the Brain server<input className="input mono" required value={path} onChange={e => setPath(e.target.value)} placeholder="/repos/my-project" /></label>
    {context?.data?.team && <label className="field">Team project<select className="select" required value={project} onChange={e => setProject(e.target.value)}><option value="">Choose a project</option>{context.data.projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>}
    <label className="check"><input type="checkbox" checked={indexNow} disabled={!canIndex} onChange={e => setIndexNow(e.target.checked)} />Index now (uses the configured embedding and summary providers)</label>
    {context?.data?.team && project && !canIndex && <p className="text-dim">Your role or this project’s policy does not grant run.request. A Contributor or Reviewer with run permission can index it after registration.</p>}
    {!context ? <p role="status">Checking server capabilities and project access…</p> : !context.ok && <p role="alert">{context.message}</p>}
    {result && !result.ok && <p role="alert">{result.message}</p>}
    <LockNote /><button type="submit" className="btn btn--primary" disabled={pending || mode !== "on" || !context?.ok}>{pending ? "Adding…" : "Add project"}</button>
  </form>;
}
