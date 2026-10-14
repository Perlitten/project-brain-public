"""Single, explicit configuration source for backup and restore tooling."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

from brain.config.settings import Settings


@dataclass(frozen=True)
class RecoveryConfig:
    postgres_url: URL
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    @property
    def pg_dump_dsn(self) -> str:
        return self.postgres_url.set(drivername="postgresql").render_as_string(hide_password=False)


def load_recovery_config(config_path: str | None = None) -> RecoveryConfig:
    selected = config_path or os.environ.get("BRAIN_ENV_FILE")
    if not selected:
        raise ValueError("Specify --config /path/to/Brain/.env (or BRAIN_ENV_FILE); refusing implicit database defaults")
    path = Path(selected).expanduser().resolve()
    if not path.is_file():
        raise ValueError("Recovery configuration file is missing; refusing implicit database defaults")
    file_values = {key: value for key, value in dotenv_values(path).items() if value is not None}
    keys = {"DATABASE_URL", "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD",
            "POSTGRES_DB", "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD", "BRAIN_STORAGE_MODE"}
    if any(key in os.environ and os.environ[key] != file_values.get(key) for key in keys):
        raise ValueError("Ambient database settings differ from the recovery configuration; refusing ambiguous target")
    if "DATABASE_URL" not in file_values and not {
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"
    }.issubset(file_values):
        raise ValueError("Recovery configuration must explicitly set DATABASE_URL or all POSTGRES_* connection fields")
    if not {"NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"}.issubset(file_values):
        raise ValueError("Recovery configuration must explicitly set all NEO4J_* connection fields")
    settings_values: dict[str, Any] = {
        **file_values,
        "DATABASE_URL": file_values.get("DATABASE_URL", ""),
        "BRAIN_STORAGE_MODE": file_values.get("BRAIN_STORAGE_MODE", "full"),
    }
    try:
        configured = Settings(_env_file=None, **settings_values)  # type: ignore[call-arg]
    except Exception:
        raise ValueError("Invalid recovery configuration; check settings without printing credentials") from None
    if configured.BRAIN_STORAGE_MODE != "full":
        raise ValueError("PostgreSQL/Neo4j recovery requires full storage mode")
    try:
        url = make_url(configured.DATABASE_URL or "")
    except Exception:
        raise ValueError("Invalid PostgreSQL DATABASE_URL in recovery configuration") from None
    if url.get_backend_name() != "postgresql" or not url.database:
        raise ValueError("Recovery requires a PostgreSQL DATABASE_URL with an explicit database name")
    if any(key.lower() not in {"sslmode", "connect_timeout", "application_name"} for key in url.query):
        raise ValueError("Unsupported PostgreSQL URL option; recovery cannot confirm its true target")
    return RecoveryConfig(url, configured.NEO4J_URI, configured.NEO4J_USER, configured.NEO4J_PASSWORD)
