"""13-Domain Failure Taxonomy for Project Brain v0.7.0."""

from enum import Enum


class FailureDomain(str, Enum):
    TASK_UNDERSTANDING = "task_understanding"
    ROUTING = "routing"
    FRESHNESS_AND_SCOPE = "freshness_and_scope"
    INVESTIGATION = "investigation"
    EVIDENCE = "evidence"
    PLANNING = "planning"
    CONTEXT_MANAGEMENT = "context_management"
    TOOL_INTERFACE = "tool_interface"
    IMPLEMENTATION = "implementation"
    VERIFICATION = "verification"
    REPAIR = "repair"
    SECURITY_AND_SAFETY = "security_and_safety"
    INFRASTRUCTURE = "infrastructure"
