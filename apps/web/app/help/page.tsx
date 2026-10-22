import Link from "next/link";
import type { ReactNode } from "react";
import { PageHead, Panel } from "@/components/ui";
import { ServerPulse } from "@/components/ServerPulse";
import { glossary, type GlossaryKey } from "@/lib/glossary";
import { withProject } from "@/lib/nav";

export const metadata = { title: "Help" };

const REPO = "https://github.com/Perlitten/project-brain-public";

// Display names where the key alone reads badly; the definitions themselves
// live in lib/glossary.ts so the hover tips and this page never disagree.
const TERM_NAMES: Partial<Record<GlossaryKey, string>> = {
  mcp: "MCP",
  chunk: "Piece (chunk)",
  pack: "Context pack",
  golden: "Golden set",
  p95: "p95",
};
const termName = (k: GlossaryKey) => TERM_NAMES[k] ?? k.charAt(0).toUpperCase() + k.slice(1);

// Guides move between releases, so link the paths of this source tree on main
// rather than guessing them for whatever tag the API reports.
const doc = (path: string) => `${REPO}/blob/main/${path}`;

function faq(repo?: string): { q: string; a: ReactNode }[] {
  const link = (path: string) => withProject(path, repo);
  return [
    {
      q: "My agent isn’t using Brain",
      a: (
        <>
          <p>
            Connecting Brain makes its tools available; it doesn’t make an agent call them. Check that the agent is connected in{" "}
            <Link className="link" href={link("/setup#agent")}>Setup</Link>, then ask it in its instructions to call <code>prepare_task_context</code> before
            planning a change and <code>review_diff</code> before finishing.
          </p>
          <p>
            MCP calls appear under <Link className="link" href={link("/activity")}>Activity</Link>, and prepared task context appears under{" "}
            <Link className="link" href={link("/packs")}>Context packs</Link>. Agent tasks only lists work explicitly started through the harness. See the{" "}
            <a className="link" href={doc("docs/MCP.md#suggested-agent-workflow")}>suggested agent workflow</a>.
          </p>
        </>
      ),
    },
    {
      q: "Brain says it is behind the code",
      a: (
        <>
          <p>
            Brain answers from the last commit it read. Press <kbd>Ctrl</kbd> <kbd>K</kbd> and choose “Re-index the repository” (on a team this needs a Contributor or Reviewer role on the project), or{" "}
            use the index step in <Link className="link" href="/setup#indexed">Setup</Link> (team admins). Later runs only re-read what changed.
          </p>
          <p>
            Brain reads committed source only. If a re-index refuses the repository, commit your source edits first; ignored build output such as{" "}
            <code>node_modules</code> doesn’t count. Past runs are under <Link className="link" href={link("/indexing")}>Index runs</Link>.
          </p>
        </>
      ),
    },
    {
      q: "A job is queued and never finishes",
      a: (
        <>
          <p>
            Jobs run in a background worker, not in the API. For a source checkout, <code>./scripts/start.sh</code> starts the API, worker and dashboard.
            For a Docker server, use <code>./deploy/server_up.sh</code> and follow the{" "}
            <a className="link" href={doc("deploy/RUNBOOK.md#start-or-update")}>deployment runbook</a> rather than starting a second local stack.
          </p>
          <p><Link className="link" href={link("/activity")}>Activity</Link> shows each job and where it stopped.</p>
        </>
      ),
    },
    {
      q: "The dashboard shows example numbers",
      a: (
        <p>
          The dashboard isn’t connected to a Brain server, so it shows demo data. Start Brain with <code>./scripts/start.sh</code>; it gives the
          dashboard the server address and key. A web app started on its own stays a demo.
        </p>
      ),
    },
    {
      q: "Where does my code go?",
      a: (
        <>
          <p>
            The code index and memory stay in the databases you run. Text still leaves the machine if you use cloud models: an embedding provider
            receives the pieces it turns into embeddings, and a language-model provider receives prompts that can include source, decisions and rules.
            Connected agents receive tool results under their own service’s policy.
          </p>
          <p>
            For local-only operation, point both model roles and their endpoints at local models in{" "}
            <Link className="link" href={link("/settings")}>Settings</Link>, disable external notifications, webhooks and remote integrations, and use a
            local agent connection. Details are in the <a className="link" href={doc("docs/FAQ.md#where-does-my-data-go")}>FAQ</a>.
          </p>
        </>
      ),
    },
    {
      q: "Can I trust what Brain hands my agent?",
      a: (
        <p>
          Treat it as a starting point. A context pack selects files, rules and decisions for one task; it can miss evidence or lag behind the latest
          edits, so the agent should still read current source and run tests. <Link className="link" href={link("/quality")}>Quality</Link> shows how well
          search finds the right code on a fixed test set, which isn’t a measure of coding results.
        </p>
      ),
    },
    {
      q: "How do I update Brain?",
      a: (
        <p>
          Back up <code>.env</code> and your data first. On a local checkout, fetch tags and check out the new release tag, run{" "}
          <code>./scripts/bootstrap.sh</code>, refresh web packages with <code>cd apps/web &amp;&amp; npm ci</code>, and restart with{" "}
          <code>./scripts/start.sh</code>. On a server, follow the{" "}
          <a className="link" href={doc("deploy/RUNBOOK.md#start-or-update")}>runbook</a> and the{" "}
          <a className="link" href={doc("docs/runbooks/backup-restore.md")}>backup guide</a>.
        </p>
      ),
    },
  ];
}

export default async function Help({ searchParams }: { searchParams: Promise<{ repo?: string }> }) {
  const { repo } = await searchParams;
  const docs: { label: string; path: string; note: string }[] = [
    { label: "Getting started", path: "docs/GETTING_STARTED.md", note: "Install, index a repository and run a first task" },
    { label: "Connect agents (MCP)", path: "docs/MCP.md", note: "Client configs and every tool an agent can call" },
    { label: "Memory", path: "docs/MEMORY.md", note: "Decisions, rules, lessons and how they are recalled" },
    { label: "Dashboard", path: "docs/DASHBOARD.md", note: "What each screen shows" },
    { label: "FAQ", path: "docs/FAQ.md", note: "Privacy, costs, offline use and limits" },
    { label: "Deployment", path: "docs/operations/DEPLOYMENT.md", note: "Running Brain on a remote server with TLS and keys" },
  ];

  return (
    <>
      <PageHead
        title="Help"
        lede={
          <>
            What Brain is, answers to common problems, and the words this dashboard uses. Each entry links to the screen or guide that goes deeper.
          </>
        }
      />

      <Panel id="about" title="About Brain">
        <p className="help__about">
          Project Brain reads the committed source of your repositories and gives your coding agents context through MCP: the relevant code, the
          decisions and rules you recorded, and what a change may affect. It runs on your own server, for one operator or a small team whose members
          each see only the projects they have a role on. It is early software; run it for people you trust.
        </p>
        <dl className="kv">
          <ServerPulse />
          <dt>Source</dt>
          <dd>
            <a className="link" href={REPO}>GitHub</a> · <a className="link" href={`${REPO}/releases`}>Releases</a> ·{" "}
            <a className="link" href={`${REPO}/issues`}>Report a problem</a> · MIT License
          </dd>
        </dl>
      </Panel>

      <Panel id="faq" title="Common questions" desc="Problems people run into first, and where to fix them." flush>
        {faq(repo).map((item) => (
          <details className="more help__item" key={item.q}>
            <summary>{item.q}</summary>
            <div className="help__answer">{item.a}</div>
          </details>
        ))}
      </Panel>

      <Panel id="glossary" title="Glossary" desc="The same explanations appear when you hover a dotted-underlined word.">
        <dl className="kv help__terms">
          {(Object.keys(glossary) as GlossaryKey[]).map((k) => (
            <div className="help__term" key={k}>
              <dt>{termName(k)}</dt>
              <dd>{glossary[k]}</dd>
            </div>
          ))}
        </dl>
      </Panel>

      <Panel id="docs" title="Guides" desc="The latest documentation on GitHub.">
        <ul className="help__docs">
          {docs.map((d) => (
            <li key={d.path}>
              <a className="link" href={doc(d.path)}>{d.label}</a>
              <span className="muted"> — {d.note}</span>
            </li>
          ))}
        </ul>
      </Panel>
    </>
  );
}
