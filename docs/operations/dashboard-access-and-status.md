# Dashboard access and status recovery

These changes are on `main` after v0.9.2; existing release archives are unchanged.

## Enable a team without losing the owner sign-in

On **Settings → Team**, prepare a human administrator key, copy or download it to
private storage, and confirm it is saved before activating the team. Preparation
creates an identity and credential, but no membership: shared sign-in still works.
Activation proves possession of that live human key, creates the first admin,
and signs this browser in with an encrypted, HTTP-only member cookie. A lost
activation response is recoverable with the saved key at the normal sign-in page.
The prepared key expires in 90 days; rotate it before expiry.

Bootstrap does not upgrade the dashboard integration. The calling credential
must already hold all scopes it mints and `team:admin`, `principals:write`, and
`setup:write` to activate the team. A restricted key cannot grant itself these
permissions. Keyless development cannot activate a team. Existing teams,
including teams with all members revoked, cannot be claimed through bootstrap.

If the dashboard lacks these scopes, use the trusted server CLI:

```sh
brain team init --name "My team"
brain team invite create --admin
```

Accept the printed invitation at `/invite` yourself and save the resulting key.
An existing server-initialized team keeps its name; use that name in the UI.
For recovery, a server operator can mint a replacement for an existing human:

```sh
brain team member add --principal alice --admin --mint-key
```

Keep the printed key private. This requires server access; the dashboard does not
recover a human account by changing its integration credential. Do not dissolve a
team merely to bypass a lost key. Bootstrap also honors the restore access fence.

Access identifies the credential serving the dashboard. Revoke/Disable are
disabled for that key/identity, and the API rejects self-revocation in solo and
team state. Mint a replacement, switch the client or deployment to it, verify
access, and only then revoke the old key using the replacement. Ordinary newly
created keys default to 90 days; non-expiring keys remain an explicit option.
Team scopes in the picker do not create human membership or project roles.

## Interpret observations before changing services

| Observation | Meaning and next action |
| --- | --- |
| Request timeout | That request exceeded its deadline. Retry; inspect request/API logs if persistent. It does not prove that Brain is stopped. |
| HTTP 401 | Sign in again or have an operator check the configured credential. |
| HTTP 403 | Check the credential's scopes and live project role. Do not restart Docker to fix permission denial. |
| Connection failure | Retry, then check the configured API address, connectivity and server logs. |
| Source unavailable | The API cannot read the registered repository folder. Check checkout existence, mounts in API and worker containers, path and permissions; then reindex. |
| Unused agent tool | No successful usage evidence in the displayed window; health remains unverified. Responses with p95 above 2 seconds are labelled slow. |
| Missing reference measurements | The historical reference artifact is absent. Scheduled golden-task smoke benchmarks produce separate reports and do not populate that artifact. |

The dashboard requests repository overviews with `include_health=false`: its
separate health poll checks service availability. API callers that omit this
option retain the original full overview, including service probes. An omitted
probe is `null`, never a fabricated healthy result. Failed jobs collection is
unavailable, distinct from a successful empty job history.

Saving Settings updates the API immediately. Workers need a restart to use those
changes. Environment/deployment overrides can return after restart; update the
deployment configuration when the Settings page identifies an override. Displayed
server times use the shared UTC formatter; values without timezone information are
not assigned one silently.

The UI changes do not diagnose a running installation's missing mount or API
latency, change deployment settings, or rotate live credentials automatically.
