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

Project Brain is a **single-user, self-hosted** service. The supported setup is:

- one operator runs the stack on their own machine or server, with their own
  repositories and provider credentials;
- the API is protected by an API key (`PROJECT_BRAIN_API_KEY`) or scoped `pbk_`
  credentials, and the development compose file binds services to `127.0.0.1`;
- anything exposed beyond localhost sits behind a reverse proxy with TLS that
  the operator configures.

In scope: authentication and scope bypasses, secret leakage, injection through
indexed repository content or API input, sandbox escapes in change-lab
validation, and unsafe defaults that ship in this repository.

Out of scope: multi-tenant isolation between untrusted users on one instance,
attacks that need an already compromised host or operator account, and
problems in third-party providers or dependencies (report those upstream, and
tell us if Project Brain uses them unsafely).
