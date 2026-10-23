# Step 0 — deployed backend identified

Observed 23 October 2026 at 07:44 Europe/Bucharest, using HTTPS with certificate verification, a unique cache-busting query and no-cache headers. No credentials, container commands or server changes were used.

- Endpoint: https://brain.167.86.91.77.nip.io/api/version
- HTTP 200, version `0.9.2`.
- Reported build SHA: `b2e9be03b9b090352395a7abc8bce5d757025dff`.
- Reported build time: `2026-10-18T15:16:21Z`.
- Current GitHub main at observation: `d3726712e1c44f10427d4740a372dfec442c961a`.
- [Captured response and headers](backend-step0.json).

[The reported build's overview](https://github.com/Perlitten/project-brain-public/blob/b2e9be03b9b090352395a7abc8bce5d757025dff/apps/api/routers/web.py#L1043) has no `include_health` argument and unconditionally awaits `check_health()` before loading repository measurements. [Merged #183](https://github.com/Perlitten/project-brain-public/pull/183) adds the conditional probe. The public Contabo API therefore reports a build predating that fix. Its version label alone is insufficient to establish deployment.

An older FastAPI route ignores the frontend's unknown `include_health=false` query parameter, so deploying just the frontend does not remove its repeated backend probes. The API must be updated before evaluating the fix against the twelve live project rows. The conditional aggregated endpoint (Dashboard ≤5 API calls) remains unimplemented; the condition that #183 is already deployed has not been met on this API.

The Vercel dashboard still returns HTTP 401 Basic authentication. Its configured `BRAIN_API_URL` cannot be independently read with the available credentials, so this identifies the public Contabo API build, not the Vercel environment binding or its twelve private project rows.

## Owner-operated rollout

This host uses archive-based source deployment. Follow [the existing source-update procedure](https://github.com/Perlitten/project-brain-public/blob/d3726712e1c44f10427d4740a372dfec442c961a/deploy/RUNBOOK.md#start-or-update), preserving instance configuration, runtime directories, mounts and Docker volumes; replacing only a release stamp or restarting an old image is insufficient. The source tree must contain the reviewed target commit. For the complete A/B/C feature set, that means main after those PRs land.

After source replacement and a green exact-revision main deployment gate, the existing rollout entry point is:

```sh
release_root="$(docker inspect brain-api --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
test -n "$release_root" && cd "$release_root" && bash deploy/server_up.sh
```

Do not run this against an old source tree. Do not bypass the gate: current main pipeline #29 still has failed SARIF uploads (`403 Rate Limit Exceeded`); native deployment workflows also remain pending. PR merge checks and the stricter production gate are separate. No server, Vercel environment, network settings, tags or releases were changed during this investigation.

After rollout, `/api/version` must report the chosen new SHA; then verify the real Dashboard and evaluate whether aggregation is still needed. The public version check can be repeated without dashboard credentials.
