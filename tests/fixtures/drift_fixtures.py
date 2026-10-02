"""Helper to set up controlled fixture repositories for architectural drift testing (Phase 7)."""

from __future__ import annotations

from pathlib import Path


def setup_clean_fixture(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    api_dir = target_dir / "apps" / "api" / "routers"
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / "clean_router.py").write_text(
        """from brain.database.repository_utils import get_repository_by_path

def get_data():
    return get_repository_by_path('.')
""",
        encoding="utf-8",
    )


def setup_violating_fixture(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    api_dir = target_dir / "apps" / "api" / "routers"
    api_dir.mkdir(parents=True, exist_ok=True)
    (api_dir / "users.py").write_text(
        """import os
from sqlalchemy.orm.session import Session

def handle_users():
    pass
""",
        encoding="utf-8",
    )

    worker_dir = target_dir / "brain" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worker.py").write_text(
        """from apps.api.static import render_template

def process_job():
    pass
""",
        encoding="utf-8",
    )

    # Malformed python file
    (target_dir / "brain" / "malformed.py").write_text(
        """def invalid_syntax(:
    broken code here
""",
        encoding="utf-8",
    )


def setup_moved_fixture(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    api_dir = target_dir / "apps" / "api" / "routers"
    api_dir.mkdir(parents=True, exist_ok=True)
    # Same violation shifted down 15 lines
    lines = ["# Comment line\n"] * 15 + [
        "import os\n",
        "from sqlalchemy.orm.session import Session\n",
        "\n",
        "def handle_users():\n",
        "    pass\n",
    ]
    (api_dir / "users.py").write_text("".join(lines), encoding="utf-8")

    worker_dir = target_dir / "brain" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worker.py").write_text(
        """from apps.api.static import render_template

def process_job():
    pass
""",
        encoding="utf-8",
    )


def setup_resolved_fixture(target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    api_dir = target_dir / "apps" / "api" / "routers"
    api_dir.mkdir(parents=True, exist_ok=True)
    # Violation removed in users.py
    (api_dir / "users.py").write_text(
        """import os

def handle_users():
    pass
""",
        encoding="utf-8",
    )

    worker_dir = target_dir / "brain" / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    (worker_dir / "worker.py").write_text(
        """from apps.api.static import render_template

def process_job():
    pass
""",
        encoding="utf-8",
    )
