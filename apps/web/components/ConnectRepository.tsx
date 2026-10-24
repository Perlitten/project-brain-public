"use client";
// "Connect repository": paste a git URL (GitHub, GitLab, Bitbucket, Azure or any
// git server), check it, pick the branch and let Brain clone and index it. The
// older "server folder" registration lives on as the second tab.
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Dialog, LockNote, useAction } from "./act";
import { AddProjectForm } from "./AddProject";
import { useCapabilities } from "./Capabilities";
import { useRestrictedMember } from "./MemberScope";
import { Chip } from "./ui";
import { connectRepository, probeRemote } from "@/lib/actions/git";
import { projectRegistrationContext } from "@/lib/actions/projects";
import { parseGitRemote, probeSummary, PROVIDER_LABEL, TOKEN_HINT, type GitProbe } from "@/lib/git-remote";

export function ConnectRepository() {
  const capabilities = useCapabilities();
  const restricted = useRestrictedMember();
  const [open, setOpen] = useState(false);
  if (capabilities.state === "demo" || !capabilities.supported.includes("repository_registration") || restricted) return null;
  const byUrl = capabilities.supported.includes("git_connect");
  return <>
    <button className="btn btn--primary" type="button" onClick={() => setOpen(true)}>Connect repository</button>
    <Dialog open={open} onClose={() => setOpen(false)} title="Connect repository" wide>{open && <ConnectForm byUrl={byUrl} />}</Dialog>
  </>;
}

type Mode = "url" | "folder";

function ConnectForm({ byUrl }: { byUrl: boolean }) {
  const [mode, setMode] = useState<Mode>(byUrl ? "url" : "folder");
  return <div className="connect">
    <div className="segmented connect__modes" role="group" aria-label="How to connect">
      <button type="button" aria-pressed={mode === "url"} onClick={() => setMode("url")} disabled={!byUrl} title={byUrl ? undefined : "This server does not support connecting by URL yet. Update the API."}>Git URL</button>
      <button type="button" aria-pressed={mode === "folder"} onClick={() => setMode("folder")}>Server folder</button>
    </div>
    {mode === "url" ? <UrlForm /> : <AddProjectForm />}
  </div>;
}

function UrlForm() {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [branch, setBranch] = useState("");
  const [name, setName] = useState("");
  const [project, setProject] = useState("");
  const [indexNow, setIndexNow] = useState(true);
  const [probe, setProbe] = useState<GitProbe | null>(null);
  const [probeMessage, setProbeMessage] = useState("");
  const [context, setContext] = useState<Awaited<ReturnType<typeof projectRegistrationContext>> | null>(null);
  const [result, setResult] = useState<Awaited<ReturnType<typeof connectRepository>> | null>(null);
  const check = useAction(probeRemote, { title: "Check repository", quiet: true, refresh: false });
  const connect = useAction(connectRepository, { title: "Connect repository" });
  const guess = useMemo(() => parseGitRemote(url), [url]);

  useEffect(() => {
    let cancelled = false;
    projectRegistrationContext().then(r => { if (!cancelled) setContext(r); }).catch(() => { if (!cancelled) setContext({ ok: false, message: "Project choices unavailable. Close and retry." }); });
    return () => { cancelled = true; };
  }, []);
  // A new address invalidates what the last check found.
  useEffect(() => { setProbe(null); setProbeMessage(""); setBranch(""); }, [url]);

  const canIndex = context?.data?.team
    ? context.data.projects.find(candidate => candidate.id === project)?.canIndex === true
    : context?.data?.canIndex === true;
  useEffect(() => { if (!canIndex) setIndexNow(false); }, [canIndex]);

  const rootMissing = probe ? probe.managed_root_configured === false : false;
  const rootReadOnly = probe ? probe.managed_root_configured === true && probe.managed_root_writable === false : false;
  const showToken = guess.kind !== "ssh";
  const branches = probe?.reachable ? probe.branches ?? [] : [];

  const runCheck = async () => {
    const res = await check.run({ url, token });
    setProbe(res.data ?? null);
    setProbeMessage(res.message);
    if (res.ok && res.data?.default_branch) setBranch(res.data.default_branch);
  };

  if (result?.ok && result.data) {
    const d = result.data;
    return <div role="status" className="connect__done">
      <p><b>{d.name}</b> is being cloned from <code>{d.display_url}</code> ({d.branch}){indexNow ? " and will be indexed right after" : ""}.</p>
      <p className="text-dim">It appears in the Projects list as soon as the clone finishes; the Origin column then shows whether it is up to date. Clone location on the server: <code className="mono">{d.path}</code>.</p>
      <p><Link className="btn btn--neutral" href={`/indexing`}>Watch the job</Link></p>
    </div>;
  }

  return <form className="form form--wide" onSubmit={async e => {
    e.preventDefault();
    setResult(await connect.run({ url, branch: branch || undefined, token: token || undefined, name: name || undefined, projectId: project || undefined, indexNow }));
  }}>
    <p>Brain clones the repository onto its server, indexes it and keeps following the branch: the scheduled origin check fetches new commits and reindexes what changed. Private repositories need a read-only token (https) or an SSH key on the server.</p>

    <label className="field">
      <span className="field__label">Repository URL</span>
      <input className="input mono" required autoFocus data-autofocus spellCheck={false} autoComplete="off" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://github.com/owner/repo.git" aria-invalid={Boolean(url) && !guess.valid} aria-describedby="connect-url-hint" />
      <span id="connect-url-hint" className={`field__hint${url && !guess.valid ? " field__hint--bad" : ""}`}>
        {!url ? "GitHub, GitLab, Bitbucket, Azure DevOps or any git server over https:// or ssh." : !guess.valid ? guess.problem : <>
          <Chip tone="info" dot={false}>{PROVIDER_LABEL[guess.provider]}</Chip> {guess.name ? <>repository <b>{guess.name}</b></> : "repository"}{guess.kind === "ssh" ? " over SSH" : ""}
          {guess.embeddedCredentials && <> · <span className="text-bad">Remove the credentials from the URL and paste the token below instead.</span></>}
        </>}
      </span>
    </label>

    {showToken && <label className="field">
      <span className="field__label">Access token <em>only for private repositories</em></span>
      <input className="input mono" type="password" autoComplete="off" spellCheck={false} maxLength={512} value={token} onChange={e => setToken(e.target.value)} placeholder="Leave empty for a public repository" aria-describedby="connect-token-hint" />
      <span id="connect-token-hint" className="field__hint">{TOKEN_HINT[guess.provider]} Brain uses it to clone and fetch; it is never shown again or written to the job log.</span>
    </label>}
    {!showToken && url && guess.valid && <p className="field__hint">SSH addresses use the SSH key of the Brain server’s worker. Without one, use the https address with a token.</p>}

    <div className="connect__check">
      <button type="button" className="btn btn--neutral" disabled={!guess.valid || check.pending || check.mode === "locked"} onClick={runCheck}>{check.pending ? "Checking…" : probe ? "Check again" : "Check"}</button>
      {probeMessage && <span role="status" className={probe?.reachable ? "text-dim" : "text-bad"}>{probe ? probeSummary(probe) : probeMessage}</span>}
    </div>
    {rootMissing && <div className="callout tone-warn"><b>Clones directory not set</b><span>Connecting by URL needs a folder on the server where Brain keeps its clones. Set “Clones directory” in <Link className="link" href="/settings#git">Settings → Git &amp; origin sync</Link> first.</span></div>}
    {rootReadOnly && <div className="callout tone-warn"><b>Clones directory is not writable</b><span>The API cannot write to the configured clones directory. Fix its permissions or choose another folder in <Link className="link" href="/settings#git">Settings → Git &amp; origin sync</Link>.</span></div>}

    <div className="form__row">
      <label className="field">
        <span className="field__label">Branch <em>{branches.length ? "" : "default branch when left empty"}</em></span>
        {branches.length ? (
          <select className="select" value={branch} onChange={e => setBranch(e.target.value)}>
            {branches.map(b => <option key={b.name} value={b.name}>{b.name}{b.name === probe?.default_branch ? " (default)" : ""}</option>)}
          </select>
        ) : (
          <input className="input mono" maxLength={200} value={branch} onChange={e => setBranch(e.target.value)} placeholder={probe?.default_branch ?? "main"} spellCheck={false} />
        )}
      </label>
      <label className="field">
        <span className="field__label">Project name <em>optional</em></span>
        <input className="input" maxLength={120} value={name} onChange={e => setName(e.target.value)} placeholder={guess.name ?? "Taken from the URL"} />
      </label>
    </div>

    {context?.data?.team && <label className="field">
      <span className="field__label">Team project</span>
      <select className="select" required value={project} onChange={e => setProject(e.target.value)}>
        <option value="">Choose a project</option>
        {context.data.projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
    </label>}

    <label className="check"><input type="checkbox" checked={indexNow} disabled={!canIndex} onChange={e => setIndexNow(e.target.checked)} />Index right after cloning (uses the configured embedding and summary providers)</label>
    {context?.data?.team && project && !canIndex && <p className="text-dim">Cloning runs as a background job and requires run.request on this project, even without initial indexing.</p>}
    {!context ? <p role="status">Checking server capabilities and project access…</p> : !context.ok && <p role="alert">{context.message}</p>}
    {result && !result.ok && <p role="alert">{result.message}</p>}

    <LockNote />
    <div className="dialog__actions">
      <button type="submit" className="btn btn--primary" disabled={!guess.valid || connect.pending || connect.mode === "locked" || !context?.ok || !canIndex || rootMissing || rootReadOnly}>{connect.pending ? "Connecting…" : "Connect"}</button>
    </div>
  </form>;
}
