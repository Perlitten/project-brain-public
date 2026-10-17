# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.9.x   | Yes       |
| < 0.9   | No        |

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub Security Advisories:
<https://github.com/Perlitten/project-brain-public/security/advisories/new>

Include the affected version or commit, the steps to reproduce, and the impact
you expect. We aim to acknowledge reports within **7 days** and will keep you
updated while a fix is prepared. Please give us a reasonable chance to release
a fix before you disclose the issue publicly.

## Threat model

### Data transmission

Self-hosted storage is not a guarantee of local-only processing. External
embedding providers receive selected source chunks; external LLM providers
receive prompts that may include source, task descriptions and project memory.
MCP clients receive context and apply their own model/provider policies.
Optional remote services and notifications transmit their configured payloads.
For local inference, configure both provider roles and endpoint overrides to
local models, and disable external integrations. Review provider retention
policies before indexing confidential repositories.

Lesson validation is reported evidence, not proof of server-side execution.
Eligible L3 guidance can auto-promote; high-risk cases and distilled L4
procedures require approval by default. See
[the tier-specific policy](docs/MEMORY_LESSON_PROTOCOL.md).

### Deployment boundary

Project Brain is a **self-hosted** service run by one trusted operator. The
supported setup is:

- one operator runs the stack on their own machine or server, with their own
  repositories and provider credentials, for themselves or, with the
  experimental team mode on `main`, for a small team of trusted people;
- the API is protected by an API key (`PROJECT_BRAIN_API_KEY`) or scoped `pbk_`
  credentials, and the development compose file binds services to `127.0.0.1`;
- anything exposed beyond localhost sits behind a reverse proxy with TLS that
  the operator configures.

In scope: authentication and scope bypasses, secret leakage, injection through
indexed repository content or API input, sandbox escapes in change-lab
validation, and unsafe defaults that ship in this repository.

Out of scope: multi-tenant isolation between untrusted users on one instance
(hostile tenants, separate organizations), attacks that need an already
compromised host or operator account, and problems in third-party providers or
dependencies (report those upstream, and tell us if Project Brain uses them
unsafely).

### Team mode (experimental preview)

Team mode is an experimental preview on `main`, not in v0.9.2 and not deployed
or piloted. Operator guide: [team mode walkthrough](docs/TEAM.md); stage and
known gaps: [team-mode status](docs/design/team-status.md).

**Project isolation is in scope.** On `main`, a team member who can read,
write, queue work for or learn about a repository in a project where they hold
no role (through the API, MCP, the dashboard or a background job) is a
vulnerability; so is a non-administrator reaching team administration, and a
revoked membership that still opens project data. Report these privately as
above. The team members themselves are trusted colleagues the operator chose:
denial of service between members, and anything a team administrator can do by
design, are out of scope. Gaps already listed on the status page are known.

**An invitation token is a bearer secret.** Whoever first presents an unexpired
`pbi_` token becomes the member it was issued for; no email or identity
provider is checked. Brain stores only its SHA-256 hash and shows it once.
Deliver it privately, keep the expiry short, and revoke it
(`brain team invite revoke <id>`) if it may have leaked. The same applies to
every `pbk_` member key.

**Revoking membership does not revoke keys.** `brain team member revoke`,
`DELETE /team/projects/members/{principal_id}` and **Revoke** on the dashboard
Team page end the membership and every project role: the member is refused
for project data and signed out of the dashboard, but their credentials stay
valid. Those credentials work again if the person is re-added, and, because a
Brain with no live member is a team of one, every remaining key acts as
administrator if the last member is revoked. To cut someone off, also revoke
their credentials or disable their principal (dashboard **Settings → Access**,
`POST /admin/credentials/{id}/revoke`, `POST /admin/principals/{id}/disable`).
For a member who joined by invitation, `brain team invite revoke <id>
--revoke-member` revokes the membership, every project role and every
credential in one step.
