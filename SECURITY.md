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
