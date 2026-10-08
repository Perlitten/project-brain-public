"use client";

import { useEffect, useRef, useState } from "react";
import { Icon } from "../Icon";
import "./agent-configurations.css";

type ClientConfig = { name: string; where: string; config: string; cli: string };

function CodeSnippet({ value, label }: { value: string; label: string }) {
  const [status, setStatus] = useState<"idle" | "copied" | "error">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  async function copy() {
    if (timer.current) clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(value);
      setStatus("copied");
      timer.current = setTimeout(() => setStatus("idle"), 2000);
    } catch {
      setStatus("error");
    }
  }

  return <div className="agent-config__snippet">
    <div className="agent-config__toolbar">
      <span>{label}</span>
      <button type="button" className="btn btn--neutral" onClick={copy} aria-label={`Copy ${label.toLowerCase()}`}>
        <Icon name={status === "copied" ? "check" : "copy"} size={14} />
        {status === "copied" ? "Copied" : "Copy"}
      </button>
    </div>
    <pre tabIndex={0} aria-label={label}><code>{value}</code></pre>
    <span className={status === "error" ? "agent-config__error" : "sr-only"} role="status">
      {status === "error" ? "Couldn’t copy. Select the code and copy it manually." : status === "copied" ? `${label} copied.` : ""}
    </span>
  </div>;
}

export function AgentConfigurations({ clients }: { clients: ClientConfig[] }) {
  const [selected, setSelected] = useState(0);
  const client = clients[selected] ?? clients[0];
  if (!client) return null;
  let formatted = client.config;
  try { formatted = JSON.stringify(JSON.parse(client.config), null, 2); } catch { /* Preserve non-JSON configuration. */ }

  return <div className="agent-config">
    <div className="agent-config__clients" role="group" aria-label="Choose your agent">
      {clients.map((entry, index) => <button type="button" key={entry.name} aria-pressed={entry === client} onClick={() => setSelected(index)}>
        {entry.name === "Any MCP client (stdio)" ? "Other MCP client" : entry.name}
      </button>)}
    </div>
    <div key={client.name} className="agent-config__content">
      <p className="agent-config__destination">Add to <code>{client.where}</code></p>
      <CodeSnippet value={formatted} label="JSON configuration" />
      {client.cli && <details className="agent-config__terminal">
        <summary>{client.name === "Claude Code" ? "Use the terminal instead" : "Launch command"}</summary>
        <CodeSnippet value={client.cli} label="Terminal command" />
      </details>}
      <p className="agent-config__note">Run this client on a machine with Brain installed. Check that the Python and repository paths match that machine, then restart your client.</p>
    </div>
  </div>;
}
