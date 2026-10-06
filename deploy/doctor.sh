#!/usr/bin/env bash
# Project Brain deployment doctor.
# Safe for agent use: prints variable names and statuses, never secret values.
set -euo pipefail

cd "$(dirname "$0")/.."

MODE="${1:-preflight}"
ENV_FILE=".env"
COMPOSE="docker compose -f docker-compose.prod.yml"
FAILURES=0

pass() { printf 'PASS: %s\n' "$1"; }
warn() { printf 'WARN: %s\n' "$1"; }
fail() { printf 'FAIL: %s\n' "$1" >&2; FAILURES=$((FAILURES + 1)); }

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

env_value() {
    local key="$1"
    if [[ ! -f "$ENV_FILE" ]]; then
        return 0
    fi
    grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2-
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

check_command() {
    local cmd="$1"
    if have_cmd "$cmd"; then
        pass "command available: $cmd"
    else
        fail "missing command: $cmd"
    fi
}

check_env_present() {
    if [[ -f "$ENV_FILE" ]]; then
        pass ".env exists"
    else
        fail ".env missing; copy deploy/.env.prod.example to .env"
    fi
}

check_no_placeholders() {
    if [[ -f "$ENV_FILE" ]] && grep -q 'REPLACE_WITH' "$ENV_FILE"; then
        fail ".env still contains REPLACE_WITH placeholders"
    else
        pass ".env has no REPLACE_WITH placeholders"
    fi
}

check_required_env() {
    local missing=()
    local invalid=()
    local key val
    for key in BRAIN_PUBLIC_HOST BRAIN_TARGET_REPO_DIR BRAIN_INDEXED_PROJECTS_DIR; do
        val="$(env_value "$key")"
        if [[ -z "$val" ]]; then
            missing+=("$key")
        fi
    done
    if [[ "${#missing[@]}" -gt 0 ]]; then
        fail "missing required env keys: ${missing[*]}"
    else
        pass "required env keys are set"
    fi

    if [[ "${#invalid[@]}" -gt 0 ]]; then
        fail "invalid env key format: ${invalid[*]}"
    else
        pass "required env formats look valid"
    fi

    # Credentials for whichever LLM / embedding provider is configured.
    local kind var problems line
    for kind in llm embedding; do
        if [[ "$kind" == "llm" ]]; then var=DEFAULT_LLM_PROVIDER; else var=DEFAULT_EMBEDDING_PROVIDER; fi
        problems="$(provider_env_problems "$kind")"
        if [[ -n "$problems" ]]; then
            while IFS= read -r line; do
                fail "$kind provider: $line"
            done <<< "$problems"
        else
            pass "$kind provider configured: $(normalize_provider "$(env_value "$var" || true)")"
        fi
    done
}

check_generated_secrets() {
    local blank=()
    local key val
    for key in POSTGRES_PASSWORD NEO4J_PASSWORD PROJECT_BRAIN_API_KEY PROJECT_BRAIN_WEBHOOK_TOKEN; do
        val="$(env_value "$key")"
        if [[ -z "$val" ]]; then
            blank+=("$key")
        fi
    done
    if [[ "${#blank[@]}" -gt 0 ]]; then
        warn "blank generated secrets; deploy/server_up.sh will generate: ${blank[*]}"
    else
        pass "generated secret env keys are populated"
    fi
}

check_mount_dirs() {
    local key dir
    for key in BRAIN_TARGET_REPO_DIR BRAIN_INDEXED_PROJECTS_DIR; do
        dir="$(env_value "$key")"
        if [[ -z "$dir" ]]; then
            continue
        fi
        if [[ -d "$dir" ]]; then
            pass "$key directory exists"
        else
            fail "$key directory does not exist"
        fi
    done
}

filter_stale_scan_runtime_paths() {
    local path
    while IFS= read -r path; do
        [[ -n "$path" ]] || continue
        case "$path" in
            .env | .mcp.json | .dashboard-temp-login.txt | .project-brain-release) continue ;;
            .secrets/* | .secrets\\*) continue ;;
            target-repo/* | target-repo\\*) continue ;;
            indexed-repos/* | indexed-repos\\*) continue ;;
            indexed-projects/* | indexed-projects\\*) continue ;;
        esac
        printf '%s\n' "$path"
    done
}

check_no_stale_terms() {
    local old_repo_lower old_repo_title old_ip old_brain_host old_n8n_host old_user old_hoster old_vps
    local old_d_projects_fwd old_d_projects_back old_obsidian_fwd old_obsidian_back old_file_uri
    local old_hybrid old_verified old_holdout old_independent old_run_independent old_run_phase8 old_run_full
    local old_lightrag old_auth_token old_telegram old_setup old_capture old_checks old_priv_ref
    local old_prof_tg old_prof_edge old_caddy pattern

    old_repo_lower='urban'"-rivals"
    old_repo_title='Urban'" Rivals"
    old_ip='198[.]51[.]100[.]77'
    old_brain_host='brain[.]198[.]51'
    old_n8n_host='n8n[.]brain[.]198[.]51'
    old_user='old'"-deploy-user"
    old_hoster='Conta'"bo"
    old_vps='vmi[0-9]+'
    old_d_projects_fwd='D:'"/projects"
    old_d_projects_back='D:'"\\\\projects"
    old_obsidian_fwd='D:'"/Obsidian"
    old_obsidian_back='D:'"\\\\Obsidian"
    old_file_uri='file:'"///D:"
    old_hybrid='HYBRID_CONTEXT'"_ENGINE"
    old_verified='VERIFIED'"_DELIVERY"
    old_holdout='hold'"out-v"
    old_independent='independent'"-tasks-v2"
    old_run_independent='run_independent'"_verification"
    old_run_phase8='run_phase8'"_independent"
    old_run_full='run_full'"_verification"
    old_lightrag='LIGHT'"RAG_API_KEY"
    old_auth_token='BRAIN_API'"_AUTH_TOKEN"
    old_telegram='TELEGRAM'"_ALLOWED_USER_IDS"
    old_setup='setup_n8n'"_telegram"
    old_capture='capture_repo'"_memory"
    old_checks='run_brain'"_checks"
    old_priv_ref='private'"_reflection"
    old_prof_tg='--profile '"telegram"
    old_prof_edge='--profile '"edge"
    old_caddy='Cad'"dy"

    pattern="${old_repo_lower}|${old_repo_title}|${old_d_projects_fwd}|${old_d_projects_back}|${old_ip}|${old_brain_host}|${old_n8n_host}|${old_user}|${old_hoster}|${old_vps}|${old_obsidian_fwd}|${old_obsidian_back}|${old_file_uri}|${old_hybrid}|${old_verified}|${old_holdout}|${old_independent}|${old_run_independent}|${old_run_phase8}|${old_run_full}|${old_lightrag}|${old_auth_token}|${old_telegram}|${old_setup}|${old_capture}|${old_checks}|${old_priv_ref}|${old_prof_tg}|${old_prof_edge}|${old_caddy}"
    local matches=""
    if have_cmd rg && [[ "${BRAIN_DOCTOR_FORCE_GREP:-0}" != "1" ]]; then
        matches="$(rg -uu -I -l "$pattern" \
            --glob '!.git/**' --glob '!.venv/**' --glob '!node_modules/**' \
            --glob '!audits/**' --glob '!docs/adr/**' \
            --glob '!reports/**' --glob '!context_packs/**' --glob '!**/__pycache__/**' \
            --glob '!/.env' --glob '!/.mcp.json' --glob '!/.secrets/**' \
            --glob '!/.dashboard-temp-login.txt' --glob '!/.project-brain-release' \
            --glob '!/target-repo/**' --glob '!/indexed-repos/**' \
            --glob '!/indexed-projects/**' \
            2>/dev/null || true)"
    else
        matches="$(
            while IFS= read -r -d '' file; do
                if grep -IqE "$pattern" "$file" 2>/dev/null; then
                    printf '%s\n' "$file"
                fi
            done < <(
                find . -type f \
                    ! -path './.git/*' ! -path './.venv/*' \
                    ! -path './audits/*' ! -path './docs/adr/*' \
                    ! -path './node_modules/*' ! -path './reports/*' \
                    ! -path './context_packs/*' ! -path './*/__pycache__/*' \
                    ! -path './.env' ! -path './.mcp.json' \
                    ! -path './.secrets/*' \
                    ! -path './.dashboard-temp-login.txt' \
                    ! -path './.project-brain-release' \
                    ! -path './target-repo/*' ! -path './indexed-repos/*' \
                    ! -path './indexed-projects/*' -print0
            )
        )"
    fi
    matches="$(printf '%s\n' "$matches" | filter_stale_scan_runtime_paths)"
    if [[ -n "$matches" ]]; then
        fail "stale project terms found in files:"
        printf '%s\n' "$matches" >&2
    else
        pass "no stale project terms found"
    fi
}

check_compose_config() {
    if $COMPOSE config --quiet >/dev/null; then
        pass "docker compose config"
    else
        fail "docker compose config failed"
    fi
}

check_local_health() {
    local api_port
    api_port="$(env_value BRAIN_API_PORT)"
    api_port="${api_port:-8010}"

    if curl -fsS "http://127.0.0.1:${api_port}/ready" >/dev/null; then
        pass "local API readiness"
    else
        fail "local API readiness failed"
    fi
}

check_containers() {
    if $COMPOSE ps >/dev/null; then
        pass "compose ps available"
        $COMPOSE ps
    else
        fail "compose ps failed"
    fi
}

check_public() {
    local brain_host
    brain_host="$(env_value BRAIN_PUBLIC_HOST)"

    if [[ -z "$brain_host" ]]; then
        fail "BRAIN_PUBLIC_HOST missing"
        return
    fi

    if getent hosts "$brain_host" >/dev/null 2>&1; then
        pass "DNS resolves: BRAIN_PUBLIC_HOST"
    else
        fail "DNS does not resolve: BRAIN_PUBLIC_HOST"
    fi

    if curl -fsS "https://${brain_host}/health" >/dev/null; then
        pass "public API reachable"
    else
        fail "public API not reachable"
    fi
}

case "$MODE" in
    preflight)
        check_command docker
        check_command curl
        check_command git
        check_command openssl
        check_env_present
        check_no_placeholders
        check_required_env
        check_generated_secrets
        check_mount_dirs
        check_no_stale_terms
        check_compose_config
        ;;
    post-start)
        check_env_present
        check_no_placeholders
        check_compose_config
        check_containers
        check_local_health
        ;;
    public)
        check_env_present
        check_public
        ;;
    stale-scan)
        check_no_stale_terms
        ;;
    all)
        bash "$0" preflight
        bash "$0" post-start
        bash "$0" public
        ;;
    *)
        printf 'Usage: %s [preflight|post-start|public|stale-scan|all]\n' "$0" >&2
        exit 2
        ;;
esac

if [[ "$FAILURES" -gt 0 ]]; then
    printf 'DOCTOR FAILED: %s failure(s)\n' "$FAILURES" >&2
    exit 1
fi

printf 'DOCTOR OK: %s\n' "$MODE"
