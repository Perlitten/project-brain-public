"use client";
// A newly minted key, shown exactly once: the key itself plus ready-to-paste
// agent configs. Used by Get started (agent step) and Access.

import { useMemo, type ReactNode } from "react";
import { CodeBlock, Dialog } from "@/components/act";
import type { NewKey } from "@/lib/actions/admin";

export function KeyDialog({
  k,
  apiUrl,
  repoPath,
  onClose,
  hint,
}: {
  k: NewKey | null;
  apiUrl: string;
  repoPath?: string;
  onClose: () => void;
  /** Replaces the closing hint under the configs. */
  hint?: ReactNode;
}) {
  const configs = useMemo(() => {
    if (!k) return null;
    const env: Record<string, string> = { BRAIN_API_URL: apiUrl, BRAIN_API_KEY: k.apiKey };
    if (repoPath) env.BRAIN_REPO = repoPath;
    const json = JSON.stringify({ mcpServers: { brain: { command: "python", args: ["-m", "apps.mcp_server.remote_server"], env } } }, null, 2);
    const cli = `claude mcp add brain ${Object.entries(env)
      .map(([a, b]) => `-e ${a}=${b}`)
      .join(" ")} -- python -m apps.mcp_server.remote_server`;
    return { json, cli };
  }, [k, apiUrl, repoPath]);
  return (
    <Dialog open={Boolean(k)} onClose={onClose} title={`Key for “${k?.name ?? "agent"}”`} wide>
      {k && configs && (
        <div className="form form--wide">
          <div className="callout tone-warn">
            <b>Copy it now — Brain shows this key only once.</b>
            <span>Lost it? Revoke it in Access and create a new one.</span>
          </div>
          <CodeBlock label="API key" code={k.apiKey} secret />
          <CodeBlock label="Claude Code — one command" code={configs.cli} secret />
          <CodeBlock label="Any MCP client — .mcp.json / mcp.json" code={configs.json} secret />
          <p className="field__hint">
            {hint ?? (
              <>
                Run it from a Project Brain checkout (the agent machine needs one for <code>apps.mcp_server</code>), then ask the agent to use any Brain tool —
                this step turns green when Brain sees it.
              </>
            )}
          </p>
          <div className="dialog__actions">
            <button type="button" className="btn btn--primary" onClick={onClose}>
              I’ve copied it
            </button>
          </div>
        </div>
      )}
    </Dialog>
  );
}
