"""Feature registry endpoints backed by the Neo4j graph: /features."""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger

from apps.api.auth import require_api_key, require_scope
from apps.api.schemas import FeatureCreate
from brain.config.paths import get_repo_root, resolve_repo_path
from brain.database.repository_utils import require_repository_by_path
from brain.graph.graph_client import GraphClient
from brain.graph.schema import NodeType

router = APIRouter()


@router.get("/features", dependencies=[Depends(require_api_key), Depends(require_scope("core:read"))])
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
async def create_feature(body: FeatureCreate):
    try:
        repository = await require_repository_by_path(
            resolve_repo_path(body.repo_path) if body.repo_path else get_repo_root()
        )
        client = GraphClient(repository_id=repository.id)
        await client.create_node(NodeType.FEATURE.value, body.name, body.properties)
        return {"status": "success", "message": f"Feature '{body.name}' created in Neo4j."}
    except Exception as exc:
        logger.error(f"Failed to create feature: {type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create feature") from exc
