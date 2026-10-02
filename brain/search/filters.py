"""Retrieval and indexing path filters with file-type weighting."""

import fnmatch
import re
from typing import Dict, Optional

from brain.search.surfaces import classify_surface
from brain.search.task_intent import derive_task_intent, is_root_config_path

KNOWLEDGE_STATUS_CURRENT = "current"
KNOWLEDGE_STATUS_HISTORICAL = "historical"
HISTORICAL_SUMMARY_PREFIX = "[KNOWLEDGE_STATUS=historical]"
HISTORICAL_AUTHORITY_NOTE = (
    "Historical or snapshot evidence only; do not treat as current state, backlog, "
    "or instruction."
)
HISTORICAL_RETRIEVAL_WEIGHT = 0.08
NON_CURRENT_PATH_PREFIXES = (
    ".agent-factory/tasks/",
    "docs/audits/",
    "docs/design/sources/",
)

_HISTORICAL_STATUS_RE = re.compile(
    r"(?im)^\s*status\s*:\s*[\"']?(historical|archived)[\"']?\s*$"
)
_HISTORICAL_HEADER_MARKERS = (
    "**historical / superseded plan.**",
    "**historical planning artifact.**",
    "**historical / superseded status:**",
    "**historical device walkthrough",
)
_HISTORICAL_QUERY_TOKENS = (
    "historical",
    "history of",
    "archived",
    "archive",
    "formerly",
    "previous decision",
    "previous architecture",
    "decision context",
    "as it was",
    "audit",
    "evidence",
    "task contract",
    "task id",
    "prototype source",
    "истор",
    "архив",
    "раньше",
    "как было",
    "предыдущего решения",
)


def _markdown_frontmatter(content: str) -> str:
    stripped = content.lstrip("\ufeff \t\r\n")
    if not stripped.startswith("---"):
        return ""
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def classify_knowledge_status(path: str, content: str) -> str:
    """Classify explicitly historical Markdown without guessing from its age.

    Time-bound audit/design/task snapshots are non-current by repository
    convention. Other Markdown needs explicit frontmatter or a strong
    owner-written header marker, so dated ADRs remain current unless their
    source says otherwise.
    """
    normalized_path = path.replace("\\", "/").lower()
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    if any(
        normalized_path.startswith(prefix)
        for prefix in NON_CURRENT_PATH_PREFIXES
    ):
        return KNOWLEDGE_STATUS_HISTORICAL
    if not normalized_path.endswith((".md", ".mdx")):
        return KNOWLEDGE_STATUS_CURRENT
    frontmatter = _markdown_frontmatter(content[:4000])
    if frontmatter and _HISTORICAL_STATUS_RE.search(frontmatter):
        return KNOWLEDGE_STATUS_HISTORICAL
    header = content[:4000]
    lowered = header.lower()
    if any(marker in lowered for marker in _HISTORICAL_HEADER_MARKERS):
        return KNOWLEDGE_STATUS_HISTORICAL
    return KNOWLEDGE_STATUS_CURRENT


def annotate_knowledge_summary(summary: str, status: str) -> str:
    """Persist authority metadata without a database migration."""
    clean = (summary or "").strip()
    if status != KNOWLEDGE_STATUS_HISTORICAL:
        return clean
    if clean.startswith(HISTORICAL_SUMMARY_PREFIX):
        return clean
    return f"{HISTORICAL_SUMMARY_PREFIX} {HISTORICAL_AUTHORITY_NOTE} {clean}".strip()


def knowledge_status_from_summary(summary: Optional[str]) -> str:
    if (summary or "").lstrip().startswith(HISTORICAL_SUMMARY_PREFIX):
        return KNOWLEDGE_STATUS_HISTORICAL
    return KNOWLEDGE_STATUS_CURRENT


def query_requests_historical_context(query: str) -> bool:
    lowered = (query or "").lower()
    return any(token in lowered for token in _HISTORICAL_QUERY_TOKENS)


def knowledge_authority_weight(summary: Optional[str], query: str) -> float:
    """Demote historical evidence unless the request explicitly asks for it."""
    if knowledge_status_from_summary(summary) != KNOWLEDGE_STATUS_HISTORICAL:
        return 1.0
    if query_requests_historical_context(query):
        return 1.0
    return HISTORICAL_RETRIEVAL_WEIGHT

# Directories excluded from indexing and retrieval
EXCLUDED_DIR_NAMES = {
    "node_modules", ".git", ".gradle", "build", "dist",
    ".next", "out", "coverage", ".idea", ".vscode",
    ".venv", "venv", "env", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".ipynb_checkpoints",
    ".hypothesis", "logs", "log",
    "reports", "context_packs", "graphify-out", "artifacts", ".artifacts",
}

# Root-level or secret files never indexed or retrieved.
# v6 P1: `test_server.py` removed — a root test harness is a valid retrieval target
# (hold_v5_36 asks to retire it), not a secret. Exclusions are for secrets/generated noise.
# Entries MUST be lowercase: is_secret_or_env_file() lowercases the name before
# the membership check, so a mixed-case entry here would never match.
EXCLUDED_FILE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    "thumbs.db", ".ds_store", ".coverage", ".thumbnail",
    "claude.md", "go.ps1", "debug_test_game.txt",
    "keystore.properties", "local.properties", "google-services.json",
}

EXCLUDED_FILE_GLOBS = (
    ".env*",
    "*credentials*",
    "*secret*",
    "*keystore*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.jceks",
    "*.p8",
    "*.crt",
    "*.cer",
    "*.csr",
    "*.mobileprovision",
    "*.jar",
    "*.war",
    "*.aar",
    "*.apk",
    "*.aab",
    "*.dex",
    "*.class",
    "*.dll",
    "*.exe",
    "*.bin",
    "*.webp",
    "*.bmp",
    "*.tiff",
    "*.tif",
    "*.avif",
    "*.heic",
    "*.so",
    "*.dylib",
    "*.nupkg",
    "*.msix",
    "*.appx",
    "*.ipa",
)

# Relative path prefixes excluded from retrieval (generated artifacts)
EXCLUDED_PATH_PREFIXES = (
    "reports/",
    "context_packs/",
    "artifacts/",
    ".artifacts/",
    ".venv/",
    "graphify-out/",
)

FILE_TYPE_WEIGHTS = {
    "source_code": 1.0,
    "test": 0.85,
    "config": 0.75,
    "script": 0.75,
    "api_spec": 0.8,
    "migration": 0.7,
    "ci_config": 0.65,
    "documentation": 0.55,
    "design_token": 0.5,
    "template": 0.45,
    "asset": 0.1,
}

# Extension surfaces that pollute server/engine retrieval unless task is UI-focused
EXTENSION_NOISE_PATHS = (
    "extension/src/content/injected.js",
    "extension/src/content/content.ts",
    "extension/src/background.ts",
    "extension/webpack.config.js",
    "extension/src/content/card-database.ts",
)

# Script surfaces that dominate vector search on generic "graph"/"report" queries
SCRIPT_NOISE_GLOBS = (
    "scripts/sync_",
    "scripts/_probe_",
    "scripts/fuzz_report.py",
    "scripts/cfr_pool_report.py",
    "scripts/backfill_iclintz",
    "scripts/deploy_",
    "scripts/check-and-run",
)

# Deploy helper noise unless task is deploy-focused
DEPLOY_NOISE_GLOBS = (
    "deploy/systemd/",
    "deploy/nginx/",
)

# Per-pipeline multipliers for RRF fusion (hints/lexical > cache/graph)
RRF_PIPELINE_WEIGHTS: Dict[str, float] = {
    "lexical": 1.25,
    "hints": 1.6,
    "vector": 1.0,
    "graph": 0.75,
    "cache": 0.55,
}

# v6 surface-mismatch neighbours kept neutral (no fusion penalty).
_SURFACE_NEIGHBOURS: Dict[str, frozenset] = {
    "server": frozenset({"database", "engine", "config"}),
    "engine": frozenset({"server", "tests"}),
    "database": frozenset({"server", "tests"}),
    "extension": frozenset({"server"}),
    "scripts": frozenset({"server", "engine", "config"}),
    "config": frozenset({"server", "docs"}),
    "docs": frozenset({"server", "config"}),
}


def surface_fusion_weight(surface: str, expected_surface: str) -> float:
    """v6: bias the recall pool toward the routed surface without dropping breadth.

    Boost-only by design: the routed surface (and its neighbours, lightly) is
    lifted, but no surface is penalised at fusion — required files on off-route
    surfaces must keep their rank. Surface *demotion* happens later in the
    deterministic reranker where task intent is richer (``surface_mismatch_penalty``).
    Unrouted queries (``expected_surface == ""``) are a no-op (weight 1.0).
    """
    if not expected_surface or surface in {"other", "tests"}:
        return 1.0
    if surface == expected_surface:
        return 1.18
    if surface in _SURFACE_NEIGHBOURS.get(expected_surface, frozenset()):
        return 1.05
    return 1.0


def surface_mismatch_penalty(path: str, expected_surface: str, wanted: bool) -> float:
    """v6 deterministic-rerank penalty for clear off-surface noise.

    Returns a negative bonus only when the path's surface is neither the routed
    surface nor a neighbour, and the task intent does not explicitly want it.
    """
    if not expected_surface or wanted:
        return 0.0
    surface = classify_surface(path)
    if surface in {"other", "tests", expected_surface}:
        return 0.0
    if surface in _SURFACE_NEIGHBOURS.get(expected_surface, frozenset()):
        return 0.0
    return -0.3


def should_ignore_dir(name: str) -> bool:
    """Check if a directory should be skipped during repository scans."""
    return name in EXCLUDED_DIR_NAMES or name.endswith(".lock")


def is_secret_or_env_file(name: str) -> bool:
    """Return True for secret/env filenames that must never be indexed."""
    lower = name.lower()
    if lower in EXCLUDED_FILE_NAMES or lower.startswith(".env"):
        return True
    return any(fnmatch.fnmatch(lower, pattern) for pattern in EXCLUDED_FILE_GLOBS)


def should_exclude_from_indexing(path: str) -> bool:
    """Return True when a relative path must not be indexed."""
    if should_exclude_from_retrieval(path):
        return True
    normalized = path.replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    return is_secret_or_env_file(basename)


def should_exclude_from_retrieval(path: str) -> bool:
    """Return True when a file path should never appear in search results."""
    normalized = path.replace("\\", "/").lower()
    if any(normalized.startswith(prefix) for prefix in EXCLUDED_PATH_PREFIXES):
        return True
    parts = normalized.split("/")
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    basename = parts[-1] if parts else normalized
    return is_secret_or_env_file(basename)


def get_file_type_weight(file_type: Optional[str]) -> float:
    """Return retrieval weight for a classified file type (code > tests > docs)."""
    if not file_type:
        return 0.7
    return FILE_TYPE_WEIGHTS.get(file_type, 0.6)


def is_template_path(path: str) -> bool:
    """Return True for Jinja/HTML template files under a templates/ directory."""
    normalized = path.replace("\\", "/").lower()
    return "/templates/" in normalized and normalized.endswith((".html", ".jinja", ".jinja2"))


def is_test_path(path: str) -> bool:
    """Return True when a path looks like a test or spec file."""
    normalized = path.replace("\\", "/").lower()
    parts = normalized.split("/")
    name = parts[-1] if parts else normalized
    return any("test" in part or "spec" in part for part in parts) or name.startswith("test_")


def is_readme_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.endswith("/readme.md") or normalized == "readme.md"


def _src_stem_for_pairing(path: str) -> str:
    norm = path.replace("\\", "/").lower()
    name = norm.rsplit("/", 1)[-1]
    base = name.rsplit(".", 1)[0]
    return base.replace("test_", "").replace("_test", "")


def paired_test_path(src_path: str) -> Optional[str]:
    """Convention: tests/test_<stem>.<ext> for a source file."""
    if is_test_path(src_path):
        return None
    stem = _src_stem_for_pairing(src_path)
    if not stem:
        return None
    ext = src_path.rsplit(".", 1)[-1] if "." in src_path else "py"
    return f"tests/test_{stem}.{ext}"


def paired_src_path(test_path: str) -> Optional[str]:
    if not is_test_path(test_path):
        return None
    stem = _src_stem_for_pairing(test_path)
    ext = test_path.rsplit(".", 1)[-1] if "." in test_path else "py"
    norm = test_path.replace("\\", "/").lower()
    candidates = [
        f"src/engine/{stem}.{ext}",
        f"src/db/{stem}.{ext}",
        f"src/server/{stem}.{ext}",
        f"src/{stem}.{ext}",
    ]
    if stem.endswith("_db"):
        short = stem[: -len("_db")]
        candidates.insert(0, f"src/db/{short}.{ext}")
        candidates.insert(1, f"src/db/{stem}.{ext}")
    if "/db/" in norm or "migrate" in stem:
        candidates.insert(0, f"src/db/migrate.{ext}")
    # Best-guess: the highest-priority candidate (the _db / migrate inserts above
    # put the right one first). Callers validate it against the actually-retrieved
    # paths before applying any pair-boost, so a wrong guess is simply dropped.
    return candidates[0] if candidates else None


def expand_protected_pairs(paths: list[str], available: set[str]) -> list[str]:
    """Insert paired src↔test neighbors for pack top-10 inclusion policy."""
    ordered: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if path not in seen and path in available:
            ordered.append(path)
            seen.add(path)
        if not is_test_path(path):
            paired = paired_test_path(path)
            if paired and paired in available and paired not in seen:
                ordered.append(paired)
                seen.add(paired)
        else:
            paired = paired_src_path(path)
            if paired and paired in available and paired not in seen:
                ordered.insert(max(len(ordered) - 1, 0), paired)
                seen.add(paired)
    return ordered


def wants_doc_readme_intent(task_description: str, task_type: str) -> bool:
    lower = task_description.lower()
    return task_type in {"docs", "architecture_rule"} or any(
        token in lower
        for token in ("readme", "document", "documentation", "golden", "guidance", "onboarding", "workflow")
    )


def compute_test_slot_cap(
    task_type: str,
    task_description: str,
    budget: str,
    protected_test_count: int,
    matched_src_in_pack: int,
) -> int:
    """General test slot policy — co-rank raises cap when src files are present."""
    base = 4 if budget == "deep" else (3 if task_type == "bugfix" else 2)
    if matched_src_in_pack > 0:
        base = max(base, matched_src_in_pack + 2)
    return max(base, protected_test_count)


def apply_pack_slot_policy(
    entries: list,
    file_limit: int,
    protected_paths: set,
    task_type: str,
    task_description: str,
    budget: str,
) -> tuple[list, dict]:
    """Apply test/template caps without silently dropping protected pipeline top-10 files."""
    matched_src = sum(
        1 for p in protected_paths
        if not is_test_path(p) and paired_test_path(p) in protected_paths
    )
    protected_tests = sum(1 for p in protected_paths if is_test_path(p))
    max_test_slots = compute_test_slot_cap(
        task_type, task_description, budget, protected_tests, matched_src
    )
    max_template_slots = 3 if budget == "deep" else (2 if task_type == "design_change" else 1)

    test_slots = 0
    template_slots = 0
    capped: list = []
    exclusions: dict = {}

    for entry in entries:
        path = entry[1]
        if path in protected_paths:
            capped.append(entry)
            if is_test_path(path):
                test_slots += 1
            if is_template_path(path):
                template_slots += 1
            continue
        if is_test_path(path):
            if test_slots >= max_test_slots:
                exclusions[path] = "test_slot_cap"
                continue
            test_slots += 1
        if is_template_path(path):
            if template_slots >= max_template_slots:
                exclusions[path] = "template_slot_cap"
                continue
            template_slots += 1
        capped.append(entry)

    return capped, exclusions


def is_migration_path(path: str) -> bool:
    """Return True for migration helper modules (not task-relevant by default)."""
    normalized = path.replace("\\", "/").lower()
    return (
        normalized.endswith("migrations.py")
        or "/migrations/" in normalized
        or normalized.endswith("/migrate.py")
        or "/alembic/" in normalized
    )


def is_extension_noise_path(path: str) -> bool:
    """Return True for extension glue files that dominate vector search."""
    normalized = path.replace("\\", "/").lower()
    return any(normalized.endswith(suffix) or normalized == suffix for suffix in EXTENSION_NOISE_PATHS)


def is_deploy_noise_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return any(token in normalized for token in DEPLOY_NOISE_GLOBS)


def is_extension_tab_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return "/options/tabs/" in normalized and normalized.endswith(".tsx")


def is_extension_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return normalized.startswith("extension/")


def is_script_noise_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    if not normalized.startswith("scripts/"):
        return False
    return any(token in normalized for token in SCRIPT_NOISE_GLOBS) or normalized.endswith(".html")


def _task_wants_extension_surface(task_type: str, task_description: str) -> bool:
    lower_task = task_description.lower()
    if task_type in {"design_change", "cross_surface"}:
        return True
    return any(
        token in lower_task
        for token in (
            "extension", "frontend", "tab", "ui", "theme", "badge", "chrome",
            "popup", "dark mode", "webpack", "docker", "nginx", "compose",
        )
    )


def _max_extension_slots(task_type: str, task_description: str, k: int = 10) -> int:
    if _task_wants_extension_surface(task_type, task_description):
        return min(4, k)
    return 1


def enforce_precision_top_k(
    paths: list,
    task_type: str,
    task_description: str,
    k: int = 10,
    priority_paths: list | None = None,
) -> list:
    """Reorder paths so the first *k* slots favor core product surfaces over extension/script noise."""
    if not paths:
        return []

    priority = [p for p in (priority_paths or []) if p in paths]
    remaining = [p for p in paths if p not in set(priority)]

    intent = derive_task_intent(task_description, task_type=task_type)
    lower_task = task_description.lower()
    wants_ui = intent.wants_extension
    wants_deploy = intent.wants_deploy
    wants_scripts = intent.wants_scripts
    wants_ml = intent.wants_ml
    wants_tests = task_type in {"bugfix", "api_contract", "database", "architecture_rule"} or any(
        token in lower_task for token in ("test", "fix", "bug", "validation", "adr", "boundary")
    )

    def _tier(path: str) -> int:
        normalized = path.replace("\\", "/").lower()
        if path in priority:
            return -10
        if is_root_config_path(path) and intent.wants_root_config:
            return 1
        if is_extension_noise_path(path) or "webpack" in normalized:
            return 90
        if is_extension_tab_path(path) and intent.wants_server_router and not wants_ui:
            return 75
        if is_extension_path(path) and not wants_ui:
            return 80
        if is_script_noise_path(path) and not wants_scripts:
            return 70
        if is_deploy_noise_path(path) and not wants_deploy:
            return 68
        if normalized in {"readme.md", "claude.md"} or normalized.endswith("/readme.md"):
            if wants_doc_readme_intent(task_description, task_type):
                return 8
            return 65
        if normalized.startswith("src/server/routers/") and intent.wants_server_router:
            return 0
        if normalized.startswith("src/server/") or normalized.startswith("src/engine/"):
            return 0
        if normalized.startswith("src/db/"):
            return 0 if task_type in {"database", "architecture_rule"} else 2
        if normalized.startswith("src/ml/") and wants_ml:
            return 3
        if normalized.startswith("tests/"):
            if "test_audit_fixes" in normalized and any(t in lower_task for t in ("pillz", "audit", "recommendation")):
                return 1
            if "test_e2e_stability" in normalized and ("wire" in lower_task or "e2e" in lower_task):
                return 1
            if "test_engine_components" in normalized and "adr" in lower_task:
                return 1
            return 2 if wants_tests else 25
        if normalized.startswith("deploy/") or normalized.startswith("docker/"):
            return 4 if wants_deploy else 40
        if normalized.startswith("scripts/"):
            if intent.is_deletion and "backfill" in normalized:
                return 5
            return 8 if wants_scripts else 50
        if normalized.startswith("extension/"):
            return 15 if wants_ui else 85
        if normalized == "run_backend.py" or normalized.startswith("src/ml/"):
            return 12 if wants_ml else 20
        return 30

    indexed = list(enumerate(remaining))
    indexed.sort(key=lambda item: (_tier(item[1]), item[0]))

    max_ext = _max_extension_slots(task_type, task_description, k)
    ext_in_top = 0
    top: list[str] = []
    rest: list[str] = []
    for _, path in indexed:
        if path in top or path in rest:
            continue
        if len(top) < k:
            if is_extension_path(path):
                if ext_in_top >= max_ext:
                    rest.append(path)
                    continue
                ext_in_top += 1
            top.append(path)
        else:
            rest.append(path)

    for path in remaining:
        if path not in top and path not in rest:
            rest.append(path)
    ordered = priority + top + rest
    seen: set[str] = set()
    deduped: list[str] = []
    for path in ordered:
        if path in seen:
            continue
        seen.add(path)
        deduped.append(path)
    return deduped


def get_contextual_retrieval_weight(
    path: str,
    file_type: Optional[str],
    task_type: str,
    task_description: str,
) -> float:
    """Adjust file-type weight using task context to demote noisy surfaces."""
    weight = get_file_type_weight("template" if is_template_path(path) else file_type)
    lower_task = task_description.lower()
    normalized = path.replace("\\", "/").lower()

    if is_template_path(path):
        if task_type == "design_change" or any(
            token in lower_task for token in ("branding", "template", "html", "styling", "dashboard")
        ):
            if "base" in lower_task and "base" not in normalized:
                return weight * 0.15
            if "base.html" in normalized:
                return max(weight, 1.2)
            return max(weight, 1.0)
        return weight * 0.3

    if is_test_path(path):
        if "health" in lower_task:
            if "test_health" in path.lower() or "health" in path.lower():
                return weight
            return weight * 0.3
        if task_type == "bugfix" or any(token in lower_task for token in ("test", "fix", "bug")):
            return weight
        return weight * 0.4

    if is_migration_path(path) and "migration" not in lower_task:
        return weight * 0.35

    if is_extension_noise_path(path):
        if task_type in {"design_change", "feature"} and any(
            token in lower_task for token in ("extension", "tab", "theme", "ui", "badge", "dark mode")
        ):
            return weight * 0.85
        return weight * 0.12

    if is_script_noise_path(path) and not any(
        token in lower_task for token in ("script", "sync", "backfill", "report", "selfplay", "self-play")
    ):
        return weight * 0.1

    if normalized.startswith("extension/") and not _task_wants_extension_surface(task_type, task_description):
        return weight * 0.2

    if "webpack" in normalized and not _task_wants_extension_surface(task_type, task_description):
        return weight * 0.08

    if normalized in {"readme.md", "claude.md"} or normalized.endswith("/readme.md"):
        if wants_doc_readme_intent(task_description, task_type):
            return max(weight, 1.15)
        return weight * 0.15

    # CSV is demoted everywhere (data files, rarely the answer); HTML only under scripts/.
    if normalized.endswith(".csv") or (normalized.endswith(".html") and "/scripts/" in normalized):
        return weight * 0.2

    if normalized.startswith("extension/test/"):
        return weight * 0.25

    if is_root_config_path(path) and derive_task_intent(task_description, task_type=task_type).wants_root_config:
        return max(weight, 1.35)

    if task_type == "deletion_rename" and normalized.startswith("scripts/"):
        return max(weight, 1.1)

    if is_extension_tab_path(path) and derive_task_intent(task_description, task_type=task_type).wants_server_router:
        if normalized.endswith("battletab.tsx") or "game.py" in lower_task:
            return weight
        return weight * 0.45

    if is_deploy_noise_path(path) and not derive_task_intent(task_description, task_type=task_type).wants_deploy:
        return weight * 0.12

    if normalized.startswith("src/ml/") and derive_task_intent(task_description, task_type=task_type).wants_ml:
        return max(weight, 1.2)

    if "recommendation" in lower_task or "pillz" in lower_task:
        if normalized.startswith("tests/") and any(k in normalized for k in ("audit", "recommend", "override", "pillz")):
            return max(weight, 1.0)

    if task_type == "database" and normalized.startswith("scripts/backfill"):
        return weight * 0.3

    if task_type == "database" and normalized.startswith("src/db/"):
        return max(weight, 1.15)

    if task_type in {"architecture_rule", "api_contract"} and normalized.startswith("src/server/"):
        return max(weight, 1.05)

    if task_type in {"architecture_rule", "bugfix", "analytics"} and normalized.startswith("src/engine/"):
        return max(weight, 1.05)

    if task_type in {"bugfix", "api_contract", "database"} and normalized.startswith("tests/"):
        return max(weight, 1.0)

    if normalized.startswith("scripts/sync_") and "sync" not in lower_task:
        return weight * 0.15

    if path.endswith("models.py") and not any(
        token in lower_task for token in ("dto", "schema", "model", "response", "entity", "status endpoint")
    ):
        return weight * 0.4

    if normalized.endswith("__init__.py"):
        return weight * 0.35

    if "/cli/" in normalized and "endpoint" in lower_task:
        return weight * 0.3

    if normalized.startswith("brain/search/") and "api" in lower_task:
        if not any(token in lower_task for token in ("retrieval", "search", "hybrid", "context pack")):
            return weight * 0.2

    if "/routers/" in normalized and not any(
        token in lower_task for token in ("router", "endpoint", "dashboard", "status")
    ):
        return weight * 0.4

    if "repository_utils" in normalized and "repository" not in lower_task:
        return weight * 0.35

    return weight


def trim_ranked_files(
    scored_files: list,
    file_limit: int,
    min_keep: int = 2,
    score_ratio: float = 0.52,
    aggressive: bool = True,
) -> list:
    """Drop low-confidence tail results to improve precision without hurting recall."""
    if not scored_files:
        return []
    if not aggressive or file_limit >= 15:
        return scored_files[:file_limit]

    top_score = scored_files[0][0]
    if top_score <= 0:
        return scored_files[:file_limit]

    cutoff = top_score * score_ratio
    trimmed = [item for item in scored_files if item[0] >= cutoff]
    if len(trimmed) < min_keep:
        trimmed = scored_files[:min_keep]

    gap_trimmed = [trimmed[0]]
    for entry in trimmed[1:]:
        if entry[0] < gap_trimmed[-1][0] * 0.62:
            break
        gap_trimmed.append(entry)

    return gap_trimmed[:file_limit]
