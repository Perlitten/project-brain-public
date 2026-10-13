from brain.graph.schema import (
    NodeType,
    RelationshipType,
    NODE_TYPES,
    RELATIONSHIP_TYPES,
    VALID_NODE_TYPES,
    VALID_RELATIONSHIP_TYPES,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.graph.graph_client import GraphClient as GraphClient


def __getattr__(name):
    if name == "GraphClient":
        from brain.graph.graph_client import GraphClient
        return GraphClient
    raise AttributeError(name)

__all__ = [
    "NodeType",
    "RelationshipType",
    "NODE_TYPES",
    "RELATIONSHIP_TYPES",
    "VALID_NODE_TYPES",
    "VALID_RELATIONSHIP_TYPES",
    "GraphClient",
]
