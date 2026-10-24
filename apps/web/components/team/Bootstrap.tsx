"use client";

import { useState } from "react";
import { CodeBlock, LockNote, useAction } from "@/components/act";
import { activateFirstAdmin, prepareFirstAdmin } from "@/lib/actions/bootstrap";
import type { NewKey } from "@/lib/actions/admin";

export function Bootstrap() {
  const [name, setName] = useState("");
  const [teamName, setTeamName] = useState("My team");
  const [key, setKey] = useState<NewKey | null>(null);
  const [saved, setSaved] = useState(false);
  const prepare = useAction(prepareFirstAdmin, { title: "Prepare administrator" });
  const activate = useAction(activateFirstAdmin, { title: "Enable team" });
  const download = () => {
    if (!key) return;
    const url = URL.createObjectURL(new Blob([key.apiKey + "\n"], { type: "text/plain" }));
    const a = document.createElement("a");
    a.href = url; a.download = "brain-admin-key.txt"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <div className="form form--wide">
    <p>Create a separate human administrator, save their key, then switch this browser to that identity. Shared sign-in stays active until the final step. Your current credential must already hold the scopes it issues; a restricted integration cannot promote itself.</p>
    {!key ? <form className="form" onSubmit={async (e) => {
      e.preventDefault();
      const result = await prepare.run(name);
      if (result.ok && result.data) setKey(result.data);
    }}>
      <label className="field"><span className="field__label">Your administrator name</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} autoComplete="off" /></label>
      <button className="btn btn--primary" disabled={prepare.pending || prepare.mode === "locked" || !name.trim()}>Prepare administrator key</button>
    </form> : <>
      <div className="callout tone-warn"><b>Save this key before continuing</b><span>Brain will not show it again. It signs you in as {key.name}, expires in 90 days and must be rotated before then. Store it privately. If this page closes now, shared sign-in still works; create a replacement under Access.</span></div>
      <CodeBlock label="Administrator sign-in key" code={key.apiKey} secret />
      <button type="button" className="btn btn--neutral" onClick={download}>Download administrator key</button>
      <label className="check"><input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} />I saved the key somewhere private and understand that shared sign-in will stop.</label>
      <label className="field"><span className="field__label">Team name</span><input className="input" value={teamName} onChange={(e) => setTeamName(e.target.value)} required maxLength={128} /><span className="field__hint">If an operator already initialized a team, use its existing name.</span></label>
      <button type="button" className="btn btn--primary" disabled={!saved || !teamName.trim() || activate.pending || activate.mode === "locked"} onClick={async () => {
        const result = await activate.run({ teamName, credential: key.apiKey, saved });
        if (result.ok) window.location.assign("/team");
      }}>Enable team and sign in as administrator</button>
    </>}
    <LockNote />
  </div>;
}
