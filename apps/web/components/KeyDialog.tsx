"use client";
// A newly minted key, shown exactly once: the key itself plus ready-to-paste
// agent configs. Agents connect to Brain's own MCP endpoint (<api>/mcp) by URL;
// the stdio bridge (python -m apps.mcp_server.remote_server) stays as the
// fallback for clients without HTTP transport. Used by Access.

import { useMemo, type ReactNode } from "react";
import { CodeBlock, Dialog } from "@/components/act";
import type { NewKey } from "@/lib/actions/admin";
import { mcpUrl } from "@/lib/mcp-address";

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
  hint?: ReactNode;
}) {
  const configs = useMemo(() => {
    if (!k) return null;
    const url = mcpUrl(apiUrl);
    const auth = `Bearer ${k.apiKey}`;
    const cli = `claude mcp add --transport http brain ${url} --header "Authorization: ${auth}"`;
    const json = JSON.stringify({ mcpServers: { brain: { type: "http", url, headers: { Authorization: auth } } } }, null, 2);
    // The stdio bridge reads its repository from env; over HTTP the server's default applies.
    const env: Record<string, string> = { BRAIN_API_URL: apiUrl, BRAIN_API_KEY: k.apiKey };
    if (repoPath) env.BRAIN_REPO = repoPath;
    const stdioJson = JSON.stringify({ mcpServers: { brain: { command: "python", args: ["-m", "apps.mcp_server.remote_server"], env } } }, null, 2);
    const stdioCli = `claude mcp add brain ${Object.entries(env).map(([a, b]) => `-e ${a}=${b}`).join(" ")} -- python -m apps.mcp_server.remote_server`;
    return { url, cli, json, stdioJson, stdioCli };
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
                Nothing to install: the agent talks to <code>{configs.url}</code> directly. Ask it to use any Brain tool — Setup turns the agent step green when
                Brain sees the call.
              </>
            )}
            {repoPath && (
              <>
                {" "}
                Tools work on <code>{repoPath}</code> unless the agent passes another <code>repo_path</code>.
              </>
            )}
          </p>
          <details className="disclosure">
            <summary>Client can’t connect by URL?</summary>
            <p>Run Brain’s tools as a local process instead. This needs a Project Brain checkout on the agent’s machine, started from its root.</p>
            <CodeBlock label="Claude Code — local process" code={configs.stdioCli} secret />
            <CodeBlock label="Any MCP client — local process" code={configs.stdioJson} secret />
          </details>
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
