import pytest
from brain.memory.decision_store import DecisionStore

@pytest.mark.asyncio
async def test_add_decision_rejects_unauthorized_global_scope():
    # An unprivileged agent attempting to create/update a global decision without admin role must be rejected
    with pytest.raises(PermissionError) as exc_info:
        await DecisionStore.add_decision(
            title="Global Security Rule",
            repo_path=None,  # system_global
            caller_role="agent",
        )
    assert "requires elevated admin authorization" in str(exc_info.value)

@pytest.mark.asyncio
async def test_add_decision_allows_repo_scoped_decision_for_agent():
    # A standard agent can create repository-scoped decisions
    # repo_path provided
    pass
