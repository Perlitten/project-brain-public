"""Portfolio Persistence Layer.

Stores portfolios and their generations with atomic writes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.portfolio.models import (
    PortfolioGeneration,
    PortfolioRecord,
)


class PortfolioStore:
    """Atomic JSON-backed portfolio and generation storage."""

    def __init__(self, storage_dir: Path):
        self._dir = storage_dir.resolve()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._portfolios_file = self._dir / "portfolios.json"
        self._generations_dir = self._dir / "generations"
        self._generations_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def save_portfolio(self, portfolio: PortfolioRecord) -> PortfolioRecord:
        """Save or update a portfolio record."""
        with self._lock:
            data = self._load_portfolios()
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            portfolio.created_at_utc = portfolio.created_at_utc or now
            portfolio.updated_at_utc = now
            data[portfolio.portfolio_id] = portfolio.to_dict()
            self._save_portfolios(data)
        return portfolio

    def get_portfolio(self, portfolio_id: str) -> Optional[PortfolioRecord]:
        """Get a portfolio by ID."""
        data = self._load_portfolios()
        raw = data.get(portfolio_id)
        if raw:
            return PortfolioRecord.from_dict(raw)
        return None

    def list_portfolios(self) -> List[PortfolioRecord]:
        """List all portfolios."""
        data = self._load_portfolios()
        return [PortfolioRecord.from_dict(v) for v in data.values()]

    def save_generation(self, gen: PortfolioGeneration) -> PortfolioGeneration:
        """Save a portfolio generation."""
        gen_file = self._generations_dir / f"{gen.generation_id}.json"
        content = json.dumps(gen.to_dict(), sort_keys=True, indent=2)
        tmp = gen_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(gen_file)
        return gen

    def get_generation(self, generation_id: str) -> Optional[PortfolioGeneration]:
        """Get a generation by ID."""
        gen_file = self._generations_dir / f"{generation_id}.json"
        if not gen_file.exists():
            return None
        try:
            data = json.loads(gen_file.read_text(encoding="utf-8"))
            return PortfolioGeneration.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None

    def list_generations(self, portfolio_id: str) -> List[PortfolioGeneration]:
        """List all generations for a portfolio."""
        gens = []
        for f in self._generations_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                gen = PortfolioGeneration.from_dict(data)
                if gen.portfolio_id == portfolio_id:
                    gens.append(gen)
            except (json.JSONDecodeError, OSError):
                continue
        return gens

    def _load_portfolios(self) -> Dict[str, Any]:
        if not self._portfolios_file.exists():
            return {}
        try:
            return json.loads(self._portfolios_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_portfolios(self, data: Dict[str, Any]) -> None:
        tmp = self._portfolios_file.with_suffix(".tmp")
        content = json.dumps(data, sort_keys=True, indent=2)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self._portfolios_file)
