"""Datastore fields: Postgres/Redis/Neo4j and related stores."""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from pydantic import field_validator


class SettingsInfra:
    # PostgreSQL Configuration
    POSTGRES_HOST: str = "localhost"

    POSTGRES_PORT: int = 5433

    POSTGRES_USER: str = "postgres"

    POSTGRES_PASSWORD: str = "postgres_password"

    POSTGRES_DB: str = "brain_db"

    POSTGRES_POOL_SIZE: int = 10

    POSTGRES_MAX_OVERFLOW: int = 10

    POSTGRES_POOL_TIMEOUT_SECONDS: float = 15.0

    # Database connection URL will be assembled if not provided in environment
    DATABASE_URL: Optional[str] = None

    # Redis Configuration
    REDIS_HOST: str = "localhost"

    REDIS_PORT: int = 6379

    REDIS_PASSWORD: Optional[str] = None

    REDIS_URL: Optional[str] = None

    # Neo4j Configuration
    NEO4J_URI: str = "bolt://localhost:7687"

    NEO4J_USER: str = "neo4j"

    NEO4J_PASSWORD: str = "neo4j_password"

    # Grafify graph JSON export (override via env GRAFIFY_OUTPUT_PATH)
    GRAFIFY_OUTPUT_PATH: Optional[str] = None

    # Obsidian vault directory for rules/decisions import
    OBSIDIAN_VAULT_PATH: Optional[str] = None

    @field_validator("POSTGRES_POOL_SIZE")
    @classmethod
    def validate_postgres_pool_size(cls, value: int) -> int:
        if not 1 <= value <= 50:
            raise ValueError("POSTGRES_POOL_SIZE must be between 1 and 50")
        return value

    @field_validator("POSTGRES_MAX_OVERFLOW")
    @classmethod
    def validate_postgres_max_overflow(cls, value: int) -> int:
        if not 0 <= value <= 50:
            raise ValueError("POSTGRES_MAX_OVERFLOW must be between 0 and 50")
        return value

    @field_validator("POSTGRES_POOL_TIMEOUT_SECONDS")
    @classmethod
    def validate_postgres_pool_timeout(cls, value: float) -> float:
        if not 0 < value <= 300:
            raise ValueError("POSTGRES_POOL_TIMEOUT_SECONDS must be greater than 0 and at most 300")
        return value

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: Optional[str], info) -> str:
        if info.data.get("BRAIN_STORAGE_MODE") == "lite":
            directory = Path(info.data.get("BRAIN_LITE_DIR", ".brain")).expanduser().resolve()
            return "sqlite+aiosqlite:///" + (directory / "brain.sqlite3").as_posix()
        if isinstance(v, str) and v:
            return v
        data = info.data
        user = data.get("POSTGRES_USER", "postgres")
        password = data.get("POSTGRES_PASSWORD", "postgres_password")
        host = data.get("POSTGRES_HOST", "localhost")
        port = data.get("POSTGRES_PORT", 5433)
        db = data.get("POSTGRES_DB", "brain_db")
        return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def assemble_redis_connection(cls, v: Optional[str], info) -> str:
        if isinstance(v, str) and v:
            return v
        data = info.data
        host = data.get("REDIS_HOST", "localhost")
        port = data.get("REDIS_PORT", 6379)
        password = data.get("REDIS_PASSWORD")
        if password:
            return f"redis://:{password}@{host}:{port}/0"
        return f"redis://{host}:{port}/0"
