"""Feature registry endpoints backed by the Neo4j graph: /features."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from apps.api.auth import require_api_key, require_principal, require_scope
from apps.api.schemas import FeatureCreate
from apps.api.team_access import repository_authority, team_administrator_access
from brain.auth.principals import ResolvedPrincipal
from brain.config.paths import get_repo_root, resolve_repo_path
from brain.database.repository_utils import require_repository_by_path
from brain.graph.graph_client import GraphClient
from brain.graph.schema import NodeType
from brain.team.authorization import Permission

router = APIRouter()


# The listing has no repository filter (every Feature node in the graph): a team
# administrator once the Brain has members.
@router.get("/features", dependencies=[Depends(require_api_key), Depends(require_scope("core:read")),
                                       Depends(team_administrator_access())])
async def list_features():
    from brain.database.session import neo4j_driver

    query = "MATCH (f:Feature) RETURN f"
    features = []
    try:
        async with neo4j_driver.session() as session:
            result = await session.run(query)
            async for record in result:
                node = record["f"]
                features.append({"name": node.get("name"), "properties": dict(node)})
    except Exception as exc:
        logger.error(f"Failed to list features from Neo4j: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to list features") from exc
    return features


@router.post("/features", dependencies=[Depends(require_api_key), Depends(require_scope("core:write"))])
async def create_feature(body: FeatureCreate, principal: ResolvedPrincipal = Depends(require_principal)):
    try:
        repo_path = resolve_repo_path(body.repo_path) if body.repo_path else get_repo_root()
        # Writing knowledge into one repository's graph: a contributor on its project.
        await repository_authority(principal, Permission.PROPOSE, repo_path=str(repo_path))
        repository = await require_repository_by_path(repo_path)
        client = GraphClient(repository_id=repository.id)
        await client.create_node(NodeType.FEATURE.value, body.name, body.properties)
        return {"status": "success", "message": f"Feature '{body.name}' created in Neo4j."}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"Failed to create feature: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create feature") from exc
