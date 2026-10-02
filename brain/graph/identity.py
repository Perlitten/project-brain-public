"""Graphify v2 Qualified Identity Generators."""

from pathlib import Path


def normalize_repo_path(file_path: str | Path) -> str:
    p = Path(file_path).as_posix()
    return p.lstrip("./")


def build_repo_qualified_id(repo_id: str) -> str:
    return f"repo:{repo_id}"


def build_generation_qualified_id(repo_id: str, generation_id: str) -> str:
    return f"gen:{repo_id}:{generation_id}"


def build_file_qualified_id(repo_id: str, generation_id: str, file_path: str | Path) -> str:
    norm = normalize_repo_path(file_path)
    return f"file:{repo_id}:{generation_id}:{norm}"


def build_module_qualified_id(repo_id: str, generation_id: str, module_name: str) -> str:
    return f"module:{repo_id}:{generation_id}:{module_name}"


def build_symbol_qualified_id(
    repo_id: str,
    generation_id: str,
    language: str,
    file_path: str | Path,
    symbol_kind: str,
    qualified_name: str,
) -> str:
    norm = normalize_repo_path(file_path)
    return f"sym:{repo_id}:{generation_id}:{language}:{norm}:{symbol_kind}:{qualified_name}"


def build_subsystem_qualified_id(repo_id: str, subsystem_name: str, version: int = 1) -> str:
    return f"subsystem:{repo_id}:v{version}:{subsystem_name}"


def build_config_qualified_id(repo_id: str, generation_id: str, config_name: str) -> str:
    return f"config:{repo_id}:{generation_id}:{config_name}"


def build_api_endpoint_qualified_id(repo_id: str, generation_id: str, http_method: str, path: str) -> str:
    return f"api:{repo_id}:{generation_id}:{http_method.upper()}:{path}"


def build_worker_task_qualified_id(repo_id: str, generation_id: str, task_name: str) -> str:
    return f"worker:{repo_id}:{generation_id}:{task_name}"
