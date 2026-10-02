from enum import Enum
from typing import Dict, Set

class NodeType(str, Enum):
    REPOSITORY = "Repository"
    MODULE = "Module"
    FILE = "File"
    SYMBOL = "Symbol"
    FEATURE = "Feature"
    SCREEN = "Screen"
    API_ENDPOINT = "ApiEndpoint"
    DTO = "Dto"
    DATABASE_ENTITY = "DatabaseEntity"
    DESIGN_TOKEN = "DesignToken"
    DESIGN_COMPONENT = "DesignComponent"
    ANALYTICS_EVENT = "AnalyticsEvent"
    TEST = "Test"
    DECISION = "Decision"
    RULE = "Rule"
    TASK = "Task"
    CONTEXT_PACK = "ContextPack"
    EXTERNAL_SERVICE = "ExternalService"
    CONFIG_KEY = "ConfigKey"
    ENVIRONMENT_VARIABLE = "EnvironmentVariable"

class RelationshipType(str, Enum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    IMPLEMENTS = "IMPLEMENTS"
    EXTENDS = "EXTENDS"
    USES = "USES"
    DEFINES = "DEFINES"
    BELONGS_TO = "BELONGS_TO"
    RELATED_TO = "RELATED_TO"
    AFFECTS = "AFFECTS"
    TESTED_BY = "TESTED_BY"
    DOCUMENTED_BY = "DOCUMENTED_BY"
    CONSUMES = "CONSUMES"
    PRODUCES = "PRODUCES"
    CONFIGURES = "CONFIGURES"
    DEPENDS_ON = "DEPENDS_ON"
    VIOLATES = "VIOLATES"
    SUPERSEDES = "SUPERSEDES"
    MENTIONS = "MENTIONS"
    GENERATED_FOR = "GENERATED_FOR"
    # v6 P1 structural edges
    ROUTE = "ROUTE"
    TESTS = "TESTS"
    SCRIPT_DOMAIN = "SCRIPT_DOMAIN"

# Dictionary representation as requested
NODE_TYPES: Dict[str, str] = {node.name: node.value for node in NodeType}
RELATIONSHIP_TYPES: Dict[str, str] = {rel.name: rel.value for rel in RelationshipType}

# Sets for validation
VALID_NODE_TYPES: Set[str] = {node.value for node in NodeType}
VALID_RELATIONSHIP_TYPES: Set[str] = {rel.value for rel in RelationshipType}
