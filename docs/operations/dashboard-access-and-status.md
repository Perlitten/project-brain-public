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


## Content history and finding actions

Current memory contains agreed, proposed and verified decisions. Replaced decisions have their own view. The API advertises `memory_current`; an older API cannot silently substitute an unfiltered or empty decision list for that view.

Finding dismissal requires operator/team-administrator authority, `core:write`, a reason and the observed evidence timestamp. A changed finding returns 409. A retry preserves the first reason and verified caller. The evidence shown at dismissal is saved separately from later scan updates. Dismissed findings are not revived automatically. Findings remain global in the current data model: repository-scoped listing still fails closed. Duplicate grouping covers the loaded page and uses evidence identity and recorded recurrence counts.

Open code reads bounded chunks from the index using the repository and file explicitly named in the finding evidence. It never reads an arbitrary server file or guesses a repository from a module name. Project source permissions are checked separately from operator access to global findings. Historical module-only findings do not gain file identity retroactively.

Request telemetry remains metadata-only by default. A team administrator may explicitly enable **Settings → Activity history → Record query text** (`REQUEST_TELEMETRY_CAPTURE_QUERY`) for future HTTP and MCP requests. This stores at most 500 characters; queries may contain source or secrets. The shared database stores this preference; API and already-running MCP processes check it before each query record. Disabling stops future capture across processes and does not erase previously stored text. The environment value seeds a new installation only once and cannot undo a later disable on restart. A missing/unreadable preference fails closed to metadata-only recording. Historical queries cannot be recovered. Trace details use the same repository/admin authorization as request history.

These contracts require the updated API and its startup migrations. The dashboard does not deploy the server, change Vercel bindings, or enable query capture automatically. Existing clients keep the previous metadata-only behavior unless the operator opts in.


Repository registration in team mode requires a live team administrator and the target project administrator checks; existing administrator credentials keep their scopes and remain compatible. Solo registration continues to require the explicit `workspace:write` credential scope. The dialog reports this permission missing before submission, without changing stored keys.

Upgrades preserve existing PostgreSQL and Lite/SQLite histories. Lite startup adds the six optional content-history columns without recreating tables; repeated startup is safe.
