#!/usr/bin/env bash
# Project Brain - production bring-up, run ON the VPS from the repo dir.
#   cd /opt/project-brain && bash deploy/server_up.sh
#
# Idempotent: generates strong secrets into .env on first run, then builds and
# starts the hardened prod stack and waits for /health. Internal datastores are
# never published to the host; the API binds to 127.0.0.1:${BRAIN_API_PORT}.
set -euo pipefail

# A concurrent deployment already happened once on this host: two runs raced,
# the release stamp one had written was overwritten by the other, and production
# ended up serving a tree that existed in no branch. The mutex is therefore not
# hardening — it is the fix for an incident. It must come before .env, the
# release stamp, the backups and any container mutation, because each of those
# is state another run can corrupt.
command -v flock >/dev/null 2>&1 || {
    echo "ERROR: flock is required for production deployment." >&2
    exit 1
}
DEPLOY_LOCK="${BRAIN_DEPLOY_LOCK_FILE:-/tmp/project-brain-deploy.lock}"
exec 9>"$DEPLOY_LOCK"
if ! flock -n 9; then
    echo "ERROR: another Project Brain deployment is already running." >&2
    echo "       lock: $DEPLOY_LOCK" >&2
    exit 1
fi

cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.prod.yml"
ENV_FILE=".env"
DEPLOY_ENV_FILE=".deploy.env"
compose_config_temp=""
ci_json_temp=""
cleanup_preflight_files() {
    if [[ -n "$compose_config_temp" ]]; then
        rm -f -- "$compose_config_temp"
    fi
    if [[ -n "$ci_json_temp" ]]; then
        rm -f -- "$ci_json_temp"
    fi
}
trap cleanup_preflight_files EXIT

if [[ ! -f "$ENV_FILE" ]]; then
    echo "==> Seeding .env from deploy/.env.prod.example"
    cp deploy/.env.prod.example "$ENV_FILE"
fi

# --- fill blank secrets deterministically (only if empty) ---
ensure_secret() {
    local key="$1" gen="$2"
    local cur
    cur="$(sed -n -E "s/^${key}=//p" "$ENV_FILE" | head -1)"
    if [[ -z "$cur" ]]; then
        echo "==> Generating ${key}"
        # portable in-place replace of "KEY=" (empty) with KEY=<value>
        local val; val="$gen"
        # escape for sed replacement
        local esc; esc="$(printf '%s' "$val" | sed -e 's/[\/&]/\\&/g')"
        if grep -qE "^${key}=" "$ENV_FILE"; then
            sed -i -E "s/^${key}=.*/${key}=${esc}/" "$ENV_FILE"
        else
            printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
        fi
    fi
}

set_env_value() {
    local key="$1" value="$2" esc
    esc="$(printf '%s' "$value" | sed -e 's/[\/&]/\\&/g')"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i -E "s/^${key}=.*/${key}=${esc}/" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
    fi
}
ensure_secret POSTGRES_PASSWORD "$(openssl rand -hex 24)"
ensure_secret NEO4J_PASSWORD "$(openssl rand -hex 24)"
ensure_secret PROJECT_BRAIN_API_KEY "$(openssl rand -hex 32)"
ensure_secret PROJECT_BRAIN_WEBHOOK_TOKEN "$(openssl rand -hex 32)"
chmod 600 "$ENV_FILE"

env_value() {
    local key="$1"
    sed -n -E "s/^${key}=//p" "$ENV_FILE" | head -1
}

# --- LLM / embedding provider credentials (mirrors brain/llm/presets.py) ---
# Prints the preset-specific API-key variable for a provider, "-" when the
# endpoint needs no key, "?" when the provider is unknown. Unset = nvidia
# (the historical production default).
provider_key_env() {
    case "$1" in
        ""|nvidia) printf 'NVIDIA_API_KEY\n' ;;
        openai) printf 'OPENAI_API_KEY\n' ;;
        openrouter) printf 'OPENROUTER_API_KEY\n' ;;
        groq) printf 'GROQ_API_KEY\n' ;;
        together) printf 'TOGETHER_API_KEY\n' ;;
        deepseek) printf 'DEEPSEEK_API_KEY\n' ;;
        mistral) printf 'MISTRAL_API_KEY\n' ;;
        anthropic) printf 'ANTHROPIC_API_KEY\n' ;;
        google) printf 'GOOGLE_API_KEY\n' ;;
        mock|ollama|lmstudio|openai_compatible) printf -- '-\n' ;;
        *) printf '?\n' ;;
    esac
}

normalize_provider() {
    local p
    p="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    case "$p" in
        custom|openai-compatible) p=openai_compatible ;;
    esac
    printf '%s\n' "$p"
}

# provider_env_problems <llm|embedding>: one human-readable problem per line
# (variable names only, never values); prints nothing when the config is usable.
provider_env_problems() {
    local kind="$1" var provider llm_provider key_env key base model dim universal
    if [[ "$kind" == "llm" ]]; then var=DEFAULT_LLM_PROVIDER; else var=DEFAULT_EMBEDDING_PROVIDER; fi
    provider="$(normalize_provider "$(env_value "$var" || true)")"
    llm_provider="$(normalize_provider "$(env_value DEFAULT_LLM_PROVIDER || true)")"
    key_env="$(provider_key_env "$provider")"
    if [[ "$kind" == "llm" ]]; then
        universal=LLM_API_KEY
        key="$(env_value LLM_API_KEY || true)"
        base="$(env_value LLM_BASE_URL || true)"
        model="$(env_value LLM_MODEL || true)"
    else
        universal=EMBEDDING_API_KEY
        key="$(env_value EMBEDDING_API_KEY || true)"
        base="$(env_value EMBEDDING_BASE_URL || true)"
        model="$(env_value EMBEDDING_MODEL || true)"
        dim="$(env_value EMBEDDING_DIMENSION || true)"
        # EMBEDDING_* fall back to LLM_* when both roles use the same provider.
        if [[ "$provider" == "$llm_provider" ]]; then
            universal="EMBEDDING_API_KEY/LLM_API_KEY"
            [[ -n "$key" ]] || key="$(env_value LLM_API_KEY || true)"
            [[ -n "$base" ]] || base="$(env_value LLM_BASE_URL || true)"
        fi
    fi

    case "$key_env" in
        "?")
            printf '%s=%s is not a supported provider\n' "$var" "$provider"
            return 0
            ;;
        "-")
            ;;
        *)
            if [[ -z "$key" ]]; then
                key="$(env_value "$key_env" || true)"
            fi
            if [[ -z "$key" ]]; then
                printf '%s is not set (or %s) for %s=%s\n' "$key_env" "$universal" "$var" "${provider:-nvidia}"
            elif [[ "$key_env" == "NVIDIA_API_KEY" && -z "$base" && ! "$key" =~ ^nvapi- ]]; then
                printf 'NVIDIA API key for %s=%s does not start with nvapi-\n' "$var" "${provider:-nvidia}"
            fi
            ;;
    esac

    if [[ "$provider" == "openai_compatible" && -z "$base" ]]; then
        if [[ "$kind" == "llm" ]]; then
            printf 'LLM_BASE_URL is not set for %s=openai_compatible\n' "$var"
        else
            printf 'EMBEDDING_BASE_URL (or LLM_BASE_URL) is not set for %s=openai_compatible\n' "$var"
        fi
    fi
    # Presets without a default model for this role need it spelled out.
    local no_default=" openai_compatible lmstudio "
    if [[ "$kind" == "embedding" ]]; then
        no_default=" openai_compatible lmstudio openrouter groq deepseek "
    fi
    if [[ "$no_default" == *" $provider "* ]]; then
        if [[ -z "$model" ]]; then
            if [[ "$kind" == "llm" ]]; then
                printf 'LLM_MODEL is not set for %s=%s\n' "$var" "$provider"
            else
                printf 'EMBEDDING_MODEL is not set for %s=%s\n' "$var" "$provider"
            fi
        fi
        if [[ "$kind" == "embedding" && ( -z "${dim:-}" || "${dim:-0}" == "0" ) ]]; then
            printf 'EMBEDDING_DIMENSION is not set for %s=%s\n' "$var" "$provider"
        fi
    fi
    return 0
}

# Deployment-only credentials must never flow through Compose's `env_file`.
# Keep these values in the host-mode-600 .deploy.env file instead.
deploy_env_value() {
    local key="$1"
    [[ -f "$DEPLOY_ENV_FILE" ]] || return 0
    sed -n -E "s/^${key}=//p" "$DEPLOY_ENV_FILE" | head -1
}

env_bool() {
    local key="$1" value
    value="$(env_value "$key" | tr '[:upper:]' '[:lower:]')"
    case "$value" in
        true|1|yes|on) printf 'true\n' ;;
        false|0|no|off|"") printf 'false\n' ;;
        *)
            echo "ERROR: ${key} has invalid boolean value '${value}'." >&2
            return 1
            ;;
    esac
}

worker_pools_enabled="$(env_bool BRAIN_WORKER_POOLS_V2_ENABLED)" || exit 1
if [[ "$worker_pools_enabled" == "true" ]]; then
    COMPOSE="$COMPOSE --profile worker-pools-v2"
    if docker inspect brain-redis >/dev/null 2>&1; then
        legacy_pending="$(docker exec brain-redis redis-cli LLEN brain:worker:queue 2>/dev/null || printf 'unknown')"
        legacy_processing="$(docker exec brain-redis redis-cli LLEN brain:worker:processing 2>/dev/null || printf 'unknown')"
        if [[ "$legacy_pending" != "0" || "$legacy_processing" != "0" ]]; then
            echo "ERROR: drain legacy brain:worker queue before enabling v2 pools (queued=${legacy_pending}, processing=${legacy_processing})." >&2
            exit 1
        fi
    fi
fi

ensure_dir_from_env() {
    local key="$1" fallback="$2" dir
    dir="$(env_value "$key")"
    dir="${dir:-$fallback}"
    mkdir -p "$dir"
}

ensure_dir_from_env BRAIN_TARGET_REPO_DIR "./target-repo"
ensure_dir_from_env BRAIN_INDEXED_PROJECTS_DIR "./indexed-projects"
mkdir -p reports context_packs

# --- immutable release identity ---------------------------------------------
# Production may be deployed from a Git export (the live host intentionally has
# no .git directory).  In that case the deployment tool writes the exact source
# commit to .project-brain-release.  A digest of the actual build inputs is
# exported as a second, independently verifiable identity.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
        echo "ERROR: refusing production build from a dirty Git worktree." >&2
        exit 1
    fi
    BRAIN_BUILD_SHA="$(git rev-parse HEAD)"
elif [[ -s .project-brain-release ]]; then
    BRAIN_BUILD_SHA="$(tr -d '[:space:]' < .project-brain-release)"
else
    BRAIN_BUILD_SHA="unknown"
fi
BRAIN_BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
source_manifest_temp="$(mktemp)"
python3 scripts/write_source_manifest.py . \
    --source-revision "$BRAIN_BUILD_SHA" \
    --output "$source_manifest_temp" \
    --include pyproject.toml \
    --include README.md \
    --include brain \
    --include apps \
    --include rules \
    --include eval \
    --include scripts/write_source_manifest.py \
    --include scripts/validate_lfm_release_gate.py \
    >/dev/null
BRAIN_SOURCE_DIGEST="$(
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["content_digest"])' \
        "$source_manifest_temp"
)"
rm -f "$source_manifest_temp"
deploy_manifest_temp="$(mktemp)"
python3 scripts/write_source_manifest.py . \
    --source-revision "$BRAIN_BUILD_SHA" \
    --output "$deploy_manifest_temp" \
    --include Dockerfile \
    --include docker-compose.prod.yml \
    --include deploy/server_up.sh \
    --include scripts/write_source_manifest.py \
    --include scripts/validate_lfm_release_gate.py \
    >/dev/null
BRAIN_DEPLOY_DIGEST="$(
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["content_digest"])' \
        "$deploy_manifest_temp"
)"
rm -f "$deploy_manifest_temp"
BRAIN_IMAGE_TAG="$BRAIN_BUILD_SHA"
set_env_value BRAIN_IMAGE_TAG "$BRAIN_IMAGE_TAG"
export BRAIN_BUILD_SHA BRAIN_BUILD_TIME BRAIN_SOURCE_DIGEST BRAIN_DEPLOY_DIGEST BRAIN_IMAGE_TAG
if [[ "$BRAIN_BUILD_SHA" == "unknown" ]]; then
    echo "ERROR: release identity is unknown; provide .project-brain-release." >&2
    exit 1
fi
echo "==> Release identity: ${BRAIN_BUILD_SHA:0:12} source=${BRAIN_SOURCE_DIGEST:0:12} deploy=${BRAIN_DEPLOY_DIGEST:0:12}"

# A production build must come from a green run of the repository's canonical
# CI workflow for this exact immutable SHA. A local checkout can have tests,
# but it cannot prove the commit that is about to be shipped passed CI.
verify_production_ci() {
    local repository workflow github_token api_url
    repository="$(env_value BRAIN_GITHUB_REPOSITORY)"
    workflow="$(env_value BRAIN_GITHUB_CI_WORKFLOW)"
    github_token="$(deploy_env_value BRAIN_GITHUB_TOKEN)"
    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        echo "ERROR: production deploy requires BRAIN_GITHUB_REPOSITORY=owner/repo." >&2
        return 1
    fi
    if [[ ! "$workflow" =~ ^[A-Za-z0-9_.-]+$ ]]; then
        echo "ERROR: production deploy requires BRAIN_GITHUB_CI_WORKFLOW (for example ci.yml)." >&2
        return 1
    fi
    if [[ -z "$github_token" ]]; then
        echo "ERROR: production deploy requires BRAIN_GITHUB_TOKEN with Actions read access." >&2
        return 1
    fi
    command -v curl >/dev/null 2>&1 || {
        echo "ERROR: curl is required to verify the GitHub CI gate." >&2
        return 1
    }
    ci_json_temp="$(mktemp)"
    api_url="https://api.github.com/repos/${repository}/actions/workflows/${workflow}/runs?head_sha=${BRAIN_BUILD_SHA}&per_page=20"
    if ! curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
        -H "Accept: application/vnd.github+json" \
        -H "Authorization: Bearer ${github_token}" \
        "$api_url" > "$ci_json_temp"; then
        echo "ERROR: unable to read GitHub CI state; refusing production deploy." >&2
        return 1
    fi
    if ! python3 - "$ci_json_temp" "$BRAIN_BUILD_SHA" <<'PY'
import json
import sys

path, sha = sys.argv[1:]
try:
    runs = json.load(open(path, encoding="utf-8")).get("workflow_runs", [])
except (OSError, ValueError, AttributeError) as exc:
    raise SystemExit(f"ERROR: invalid GitHub CI response: {type(exc).__name__}")
matching = [run for run in runs if run.get("head_sha") == sha]
if not matching:
    raise SystemExit("ERROR: no CI run exists for the release SHA")
latest = max(matching, key=lambda run: run.get("run_started_at") or run.get("created_at") or "")
if latest.get("status") != "completed" or latest.get("conclusion") != "success":
    raise SystemExit(
        "ERROR: CI is not green for the release SHA "
        f"(status={latest.get('status')!r}, conclusion={latest.get('conclusion')!r})"
    )
print(f"==> CI gate OK: run {latest.get('html_url', '<unknown>')}")
PY
    then
        return 1
    fi
}

if [[ "$(env_value ENVIRONMENT)" == "production" ]]; then
    verify_production_ci || exit 1
fi

# --- preflight: placeholders must be customized before deployment ---
if grep -q 'REPLACE_WITH' "$ENV_FILE"; then
    echo "ERROR: replace all REPLACE_WITH... placeholders in .env before deploy." >&2
    echo "       Example: brain.203.0.113.10.nip.io" >&2
    exit 1
fi

# --- preflight: the configured LLM/embedding providers must have credentials ---
# DEFAULT_LLM_PROVIDER / DEFAULT_EMBEDDING_PROVIDER (nvidia when unset) select
# the provider; its key (or LLM_API_KEY) must be present. The nvapi- prefix is
# enforced only for nvidia. Keyless endpoints (ollama, lmstudio,
# openai_compatible) need a base URL / model instead.
provider_problems="$(provider_env_problems llm; provider_env_problems embedding)"
if [[ -n "$provider_problems" ]]; then
    while IFS= read -r line; do
        echo "ERROR: $line" >&2
    done <<< "$provider_problems"
    echo "       Set the provider credentials in .env from your password manager and re-run." >&2
    exit 1
fi

self_diagnosis_enabled="$(env_bool SELF_DIAGNOSIS_ENABLED)" || exit 1
telegram_alerts_enabled="$(env_bool TELEGRAM_ALERTS_ENABLED)" || exit 1
if [[ "$self_diagnosis_enabled" == "true" ]]; then
    if [[ "$telegram_alerts_enabled" != "true" ]] \
        || [[ -z "$(env_value TELEGRAM_ALERT_BOT_TOKEN)" ]] \
        || [[ -z "$(env_value TELEGRAM_ALERT_CHAT_ID)" ]]; then
        echo "ERROR: enabled self-diagnosis requires enabled Telegram alerts, bot token, and chat id." >&2
        exit 1
    fi
fi

# Resolve the effective container environments first. Compose interpolation and
# shell overrides must not be able to diverge from the LFM policy we validate
# in .env; the JSON can contain secrets, so keep it in a mode-600 temporary file.
compose_config_temp="$(mktemp)"
$COMPOSE config --format json > "$compose_config_temp"

# Remote registration alone is inert. The Python preflight behaviorally checks
# every accepted boolean spelling, duplicate critical keys, immutable release
# binding, the complete evidence bundle, and the effective api/worker values.
python3 scripts/validate_lfm_release_gate.py \
    --env-file "$ENV_FILE" \
    --build-sha "$BRAIN_BUILD_SHA" \
    --source-digest "$BRAIN_SOURCE_DIGEST" \
    --reports-root "$(pwd)/reports" \
    --effective-compose-config "$compose_config_temp"

echo "==> Validating compose config"
$COMPOSE config >/dev/null

# --- pre-deploy backup: snapshot datastores before rebuilding containers ---
BACKUP_DIR="./backups/$(date -u +%Y%m%dT%H%M%SZ)"
if docker inspect brain-postgres >/dev/null 2>&1; then
    echo "==> Backing up PostgreSQL before deploy"
    mkdir -p "$BACKUP_DIR"
    docker exec brain-postgres pg_dump -U "${POSTGRES_USER:-postgres}" "${POSTGRES_DB:-brain_db}" \
        | gzip > "$BACKUP_DIR/brain_db.sql.gz" 2>/dev/null \
        && echo "    PostgreSQL backup: $BACKUP_DIR/brain_db.sql.gz" \
        || echo "    WARNING: PostgreSQL backup failed (container may not be running)"
fi
if docker inspect brain-neo4j >/dev/null 2>&1; then
    echo "==> Backing up Neo4j before deploy"
    mkdir -p "$BACKUP_DIR"
    docker exec brain-neo4j neo4j-admin database dump neo4j --to-path=/tmp/neo4j-backup 2>/dev/null \
        && docker cp brain-neo4j:/tmp/neo4j-backup/neo4j.dump "$BACKUP_DIR/neo4j.dump" 2>/dev/null \
        && echo "    Neo4j backup: $BACKUP_DIR/neo4j.dump" \
        || echo "    WARNING: Neo4j backup failed (container may not be running)"
fi
# Prune old backups (keep last 5)
if [[ -d ./backups ]]; then
    ls -1dt ./backups/20* 2>/dev/null | tail -n +6 | xargs rm -rf -- 2>/dev/null || true
fi

echo "==> Building images"
$COMPOSE build

# Bind-mounted runtime dirs (reports/, context_packs/) are created above by
# whoever runs this script, so they inherit that user's uid — while the API
# runs as uid 999 ('brain', pinned in the Dockerfile). Align ownership through
# the freshly built image itself: root only inside this one-off init container,
# never in the running service, no sudo needed on the host, idempotent. This
# replaces the manual `chown -R 999:999` hotfix of 2026-07-30 with deploy
# mechanics.
#
# Plain `docker run`, NOT `compose run`: the api service carries
# `cap_drop: ALL`, and a compose-run one-off inherits it — root without
# CAP_CHOWN cannot chown, which is how the first attempt at this step failed
# in production. The default docker capability set includes CHOWN.
echo "==> Ensuring reports/ and context_packs/ ownership (uid 999 'brain')"
docker run --rm --user 0:0 \
    -v "$(pwd)/reports:/fix/reports" \
    -v "$(pwd)/context_packs:/fix/context_packs" \
    --entrypoint chown \
    "brain-api:${BRAIN_IMAGE_TAG}" \
    -R brain:brain /fix/reports /fix/context_packs

echo "==> Starting prod stack (internal datastores unpublished)"
$COMPOSE up -d

echo "==> Waiting for API /ready (up to ~3 min)"
port="$(grep -E '^BRAIN_API_PORT=' "$ENV_FILE" | cut -d= -f2-)"; port="${port:-8010}"
ok=0
for _ in $(seq 1 36); do
    if curl -fsS "http://127.0.0.1:${port}/ready" >/dev/null 2>&1; then ok=1; break; fi
    sleep 5
done
echo "==> Container status:"; $COMPOSE ps
if [[ "$ok" == "1" ]]; then
    echo "==> HEALTH OK"
    curl -fsS "http://127.0.0.1:${port}/ready" | head -c 400; echo
    # Deploy check for the 2026-07-30 incident class: the running api must be
    # non-root AND able to write the bind-mounted reports dir. A regression
    # here fails the deploy loudly instead of crash-looping later.
    echo "==> Write-probe: api uid + reports/ writability"
    api_uid="$(docker exec brain-api id -u)"
    if [[ "$api_uid" == "0" ]]; then
        echo "ERROR: brain-api runs as root — permission problems must not be solved this way." >&2
        exit 1
    fi
    docker exec brain-api sh -c 'touch /app/reports/.write-probe && rm /app/reports/.write-probe' \
        || { echo "ERROR: /app/reports is not writable by uid ${api_uid} — ownership mechanics regressed." >&2; exit 1; }
    echo "    uid=${api_uid}, reports writable: OK"
else
    echo "==> HEALTH NOT READY - recent api logs:" >&2
    $COMPOSE logs --tail 60 api >&2 || true
    exit 1
fi
echo "==> Scheduler state (from /health; jobs fire inside the worker):"
curl -fsS "http://127.0.0.1:${port}/health" | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin).get("scheduler"), indent=2))' || true

# Repo path sandbox check: every repositories row and every dir under the
# ALLOWED_REPO_ROOTS mounts must pass validate_secure_repo_path inside the
# container, or indexing that path fails at request time. A misconfig here
# must not fail a deploy that already passed its health gates, so this runs
# in an || context and reports FAIL lines instead.
echo "==> Repo path sandbox check (validate_secure_repo_path)"
$COMPOSE exec -T api python -m brain.config.repo_root_check \
    || echo "    FAIL: repository path sandbox check failed (see FAIL lines above)" >&2

# --- release image retention -------------------------------------------------
# Every deploy leaves behind a brain-api:<sha> image, and on this host they grew
# until the disk hit 95%. Those images are also the rollback mechanism, so they
# cannot be pruned blindly: this runs only after the health gates above proved
# the new release actually serves traffic, keeps a rollback window of the most
# recent BRAIN_IMAGE_RETENTION images, and never touches a tag some container
# still references or a repository other than brain-api. Anything else on the
# host (external-brain, n8n, datastores) is out of scope by construction.
prune_old_release_images() {
    local keep tag index in_use
    local age_sorted=() removable=()
    keep="$(env_value BRAIN_IMAGE_RETENTION)"
    keep="${keep:-5}"
    if [[ ! "$keep" =~ ^[1-9][0-9]*$ ]]; then
        echo "    WARNING: BRAIN_IMAGE_RETENTION='${keep}' is not a positive integer; using 5" >&2
        keep=5
    fi
    # Repository is matched here rather than through `--filter reference=`: the
    # filter is rejected outright by some engines, and a silent empty result
    # would look exactly like "nothing to clean". CreatedAt is a fixed-width UTC
    # timestamp, so a lexicographic reverse sort is a newest-first ordering.
    mapfile -t age_sorted < <(
        docker images --format '{{.CreatedAt}}|{{.Repository}}|{{.Tag}}' \
            | awk -F'|' '$2 == "brain-api" && $3 != "<none>" { print $1 "|" $3 }' \
            | sort -r | cut -d'|' -f2
    )
    if (( ${#age_sorted[@]} <= keep )); then
        echo "    ${#age_sorted[@]} brain-api image(s), retention ${keep}: nothing to remove"
        return 0
    fi
    # Stopped containers count: their image is what an operator restarts.
    in_use="$(docker ps -a --format '{{.Image}}' | sed -n 's|^brain-api:||p' | sort -u)"
    index=0
    for tag in "${age_sorted[@]}"; do
        index=$((index + 1))
        (( index <= keep )) && continue
        [[ "$tag" == "$BRAIN_IMAGE_TAG" ]] && continue
        printf '%s\n' "$in_use" | grep -qxF -- "$tag" && continue
        removable+=("$tag")
    done
    if (( ${#removable[@]} == 0 )); then
        echo "    ${#age_sorted[@]} brain-api image(s), retention ${keep}: all older tags are still referenced"
        return 0
    fi
    for tag in "${removable[@]}"; do
        if docker rmi "brain-api:${tag}" >/dev/null 2>&1; then
            echo "    removed brain-api:${tag:0:12}"
        else
            echo "    WARNING: could not remove brain-api:${tag:0:12} (still referenced)"
        fi
    done
    echo "    kept ${keep} most recent release image(s) plus the running one"
}

echo "==> Pruning old release images"
# Invoked in an `||` context on purpose: that suppresses `set -e` inside the
# whole call, so a retention failure can never fail a deploy that has already
# passed its health gates.
prune_old_release_images \
    || echo "    WARNING: image retention failed; the deploy itself is unaffected" >&2
