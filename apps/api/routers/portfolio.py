"""REST API Router for Portfolio Intelligence — Phase B7."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from apps.api.auth import require_api_key, require_scope
from brain.portfolio.graph_builder import PortfolioCoupling, PortfolioGraphBuilder
from brain.portfolio.models import parse_portfolio_config
from brain.portfolio.store import PortfolioStore
from brain.workspace.registry_store import RegistryStore

router = APIRouter(
    prefix="/portfolio",
    tags=["portfolio"],
    dependencies=[Depends(require_api_key), Depends(require_scope("portfolio:read"))],
)


def _get_store() -> PortfolioStore:
    return PortfolioStore(Path(".brain/portfolio"))


class PortfolioCreateRequest(BaseModel):
    config: dict


@router.get("", dependencies=[Depends(require_scope("portfolio:read"))])
async def list_portfolios():
    store = _get_store()
    portfolios = store.list_portfolios()
    return {"status": "success", "total": len(portfolios), "portfolios": [p.to_dict() for p in portfolios]}


@router.post("", dependencies=[Depends(require_scope("portfolio:write"))])
async def create_portfolio(req: PortfolioCreateRequest):
    try:
        portfolio = parse_portfolio_config(req.config)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store = _get_store()
    saved = store.save_portfolio(portfolio)
    return {"status": "success", "portfolio": saved.to_dict()}


@router.get("/{portfolio_id}", dependencies=[Depends(require_scope("portfolio:read"))])
async def get_portfolio(portfolio_id: str):
    store = _get_store()
    p = store.get_portfolio(portfolio_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Portfolio '{portfolio_id}' not found")
    return {"status": "success", "portfolio": p.to_dict()}


@router.post("/{portfolio_id}/build", dependencies=[Depends(require_scope("portfolio:write"))])
async def build_portfolio_graph(portfolio_id: str):
    store = _get_store()
    portfolio = store.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail=f"Portfolio '{portfolio_id}' not found")

    # A generation records the revision it was built from, so the revisions have
    # to come from the registry. Writing a literal "HEAD" would make a record
    # that can never be resolved back to a commit look like a current one.
    registry = RegistryStore(Path(".brain/workspace"))
    revisions: dict[str, str] = {}
    generations: dict[str, str] = {}
    missing: list[str] = []
    for repo in portfolio.repositories:
        record = registry.get(repo.repository_id)
        if record is None:
            missing.append(repo.repository_id)
            continue
        revisions[repo.repository_id] = record.current_revision
        generations[repo.repository_id] = record.graph_generation_status
    if missing:
        raise HTTPException(
            status_code=409,
            detail="Portfolio repositories are not registered: " + ", ".join(missing),
        )

    builder = PortfolioGraphBuilder(portfolio)
    gen = builder.build_generation(revisions, generations)

    if gen.quality_report.get("passed", False):
        gen = builder.activate(gen)
        portfolio.active_generation_id = gen.generation_id
        store.save_portfolio(portfolio)

    store.save_generation(gen)
    return {"status": "success", "generation": gen.to_dict()}


@router.get("/{portfolio_id}/dependencies", dependencies=[Depends(require_scope("portfolio:read"))])
async def get_dependencies(portfolio_id: str):
    store = _get_store()
    portfolio = store.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if not portfolio.active_generation_id:
        raise HTTPException(status_code=404, detail="No active generation")
    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    return {"status": "success", "dependencies": [r.to_dict() for r in gen.relationships]}


@router.get("/{portfolio_id}/cycles", dependencies=[Depends(require_scope("portfolio:read"))])
async def get_cycles(portfolio_id: str):
    store = _get_store()
    portfolio = store.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if not portfolio.active_generation_id:
        raise HTTPException(status_code=404, detail="No active generation")
    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    coupling = PortfolioCoupling.calculate(gen, portfolio)
    return {"status": "success", "circular_dependencies": coupling["circular_dependencies"]}


@router.get("/{portfolio_id}/coupling", dependencies=[Depends(require_scope("portfolio:read"))])
async def get_coupling(portfolio_id: str):
    store = _get_store()
    portfolio = store.get_portfolio(portfolio_id)
    if not portfolio:
        raise HTTPException(status_code=404, detail="Portfolio not found")
    if not portfolio.active_generation_id:
        raise HTTPException(status_code=404, detail="No active generation")
    gen = store.get_generation(portfolio.active_generation_id)
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    coupling = PortfolioCoupling.calculate(gen, portfolio)
    return {"status": "success", "coupling": coupling}
