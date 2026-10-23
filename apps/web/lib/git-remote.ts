// What the "Connect repository" form can tell from a pasted URL before the
// server sees it: the hosting provider, the repository name and whether the
// address is one the API accepts (https://, ssh:// or git@host:path). The
// server re-validates; this only shapes the form and its hints.

export type GitProvider = "github" | "gitlab" | "bitbucket" | "azure" | "other";

export interface GitRemoteGuess {
  /** False when the server will certainly reject the address; `problem` says why. */
  valid: boolean;
  problem?: string;
  kind?: "https" | "ssh";
  host?: string;
  provider: GitProvider;
  /** Repository name without `.git`, e.g. `project-brain`. */
  name?: string;
  /** The URL with any `user:secret@` part removed, safe to show back. */
  display?: string;
  /** A token or password is embedded in the URL itself. */
  embeddedCredentials: boolean;
}

const PROVIDER_HOSTS: Record<string, GitProvider> = {
  "github.com": "github",
  "gitlab.com": "gitlab",
  "bitbucket.org": "bitbucket",
  "dev.azure.com": "azure",
  "ssh.dev.azure.com": "azure",
  "vs-ssh.visualstudio.com": "azure",
};
const SCHEME = /^(https?|ssh):\/\//i;
// scp-like: git@github.com:owner/repo.git (no scheme, a colon after the host, no port).
const SCP = /^(?:([A-Za-z0-9._-]+)@)?([A-Za-z0-9.-]+):(?!\/\/)([^:]+)$/;
const CONTROL = /[\x00-\x20\x7f]/;
const UNSUPPORTED = "Use an https://, ssh:// or git@host:path address.";

export const PROVIDER_LABEL: Record<GitProvider, string> = {
  github: "GitHub",
  gitlab: "GitLab",
  bitbucket: "Bitbucket",
  azure: "Azure DevOps",
  other: "Git server",
};

/** Which credential to paste for a private https remote. */
export const TOKEN_HINT: Record<GitProvider, string> = {
  github: "Fine-grained personal access token with read access to Contents (Settings → Developer settings).",
  gitlab: "Project or personal access token with the read_repository scope.",
  bitbucket: "Repository access token, or an app password with repository read permission.",
  azure: "Personal access token with Code (Read).",
  other: "Password or access token the server accepts over https.",
};

export function providerFor(host: string): GitProvider {
  const h = host.toLowerCase();
  for (const [known, provider] of Object.entries(PROVIDER_HOSTS)) if (h === known || h.endsWith("." + known)) return provider;
  if (h.includes("gitlab")) return "gitlab";
  if (h.includes("github")) return "github";
  if (h.includes("bitbucket")) return "bitbucket";
  return "other";
}

function nameFrom(path: string): string | undefined {
  const parts = path.replace(/\/+$/, "").split("/").filter(Boolean);
  const last = parts[parts.length - 1];
  return last ? last.replace(/\.git$/i, "") || undefined : undefined;
}

const invalid = (problem: string): GitRemoteGuess => ({ valid: false, problem, provider: "other", embeddedCredentials: false });

export function parseGitRemote(input: string): GitRemoteGuess {
  const raw = (input ?? "").trim();
  if (!raw) return invalid("Paste the repository’s clone URL.");
  if (raw.length > 1000) return invalid("The URL is too long.");
  if (raw.startsWith("-")) return invalid("The URL must not start with “-”.");
  if (CONTROL.test(raw)) return invalid("The URL must not contain spaces or control characters.");
  if (raw.includes("::")) return invalid(UNSUPPORTED);
  if (SCHEME.test(raw)) {
    let url: URL;
    try { url = new URL(raw); } catch { return invalid("That is not a complete URL."); }
    const host = url.hostname.toLowerCase();
    if (!host) return invalid("The URL has no host.");
    if (url.protocol === "ssh:" && url.password) return invalid("SSH addresses use the server’s SSH key; use HTTPS for a password or token.");
    const path = url.pathname;
    if (["", ".", ".."].includes(path.replace(/^\/+|\/+$/g, ""))) return invalid("The URL has no repository path.");
    const embedded = Boolean(url.username || url.password);
    const display = `${url.protocol}//${embedded ? "***@" : ""}${url.host}${path}`;
    return { valid: true, kind: url.protocol === "ssh:" ? "ssh" : "https", host, provider: providerFor(host), name: nameFrom(path), display, embeddedCredentials: embedded };
  }
  if (raw.includes("://")) return invalid(UNSUPPORTED);
  const scp = SCP.exec(raw);
  if (scp) {
    const host = scp[2].toLowerCase();
    const path = scp[3];
    if (["", ".", ".."].includes(path.replace(/^\/+|\/+$/g, "")) || (!host.includes(".") && host !== "localhost")) return invalid(UNSUPPORTED);
    return { valid: true, kind: "ssh", host, provider: providerFor(host), name: nameFrom(path), display: raw, embeddedCredentials: false };
  }
  return invalid(UNSUPPORTED);
}

/** The server's reply to POST /git/probe. */
export interface GitProbe {
  ok: boolean;
  display_url?: string;
  provider?: string;
  name?: string;
  reachable?: boolean;
  needs_auth?: boolean;
  default_branch?: string | null;
  head?: string | null;
  branches?: { name: string; head: string }[];
  error?: string | null;
  managed_root_configured?: boolean;
  managed_root_writable?: boolean;
}

/** One sentence for the form after a probe. */
export function probeSummary(p: GitProbe): string {
  if (!p.reachable) return p.needs_auth ? "The remote asked for credentials. Add a token and check again." : p.error || "The remote is not reachable from the Brain server.";
  const n = p.branches?.length ?? 0;
  const branches = n === 0 ? "no branches listed" : `${n} branch${n === 1 ? "" : "es"}`;
  return `Reachable: ${branches}${p.default_branch ? `, default ${p.default_branch}` : ""}.`;
}
