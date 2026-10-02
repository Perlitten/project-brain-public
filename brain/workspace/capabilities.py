"""Capabilities Manager for Repository Access Control.

Phase A4 — Default-deny capability policy per repository.
Untrusted repositories never gain execution capability automatically.
"""

from __future__ import annotations

from typing import List

from brain.workspace.models import (
    Capability,
    DEFAULT_CAPABILITIES,
    TrustLevel,
)


class CapabilityDeniedError(PermissionError):
    """Raised when a repository lacks a required capability."""
    pass


class CapabilitiesManager:
    """Evaluates and enforces per-repository capability policies."""

    @staticmethod
    def get_defaults(trust_level: TrustLevel) -> List[str]:
        """Return default capabilities for a trust level."""
        caps = DEFAULT_CAPABILITIES.get(trust_level, [])
        return [c.value for c in caps]

    @staticmethod
    def check(
        repository_capabilities: List[str],
        required: Capability,
    ) -> bool:
        """Check whether a repository has a required capability."""
        return required.value in repository_capabilities

    @staticmethod
    def require(
        repository_id: str,
        repository_capabilities: List[str],
        required: Capability,
    ) -> None:
        """Require a capability or raise CapabilityDeniedError."""
        if required.value not in repository_capabilities:
            raise CapabilityDeniedError(
                f"Repository '{repository_id}' lacks capability '{required.value}'"
            )

    @staticmethod
    def grant(
        current_capabilities: List[str],
        trust_level: TrustLevel,
        capability: Capability,
    ) -> List[str]:
        """Grant a capability if trust level permits it.

        Untrusted repositories cannot gain EXECUTE_VALIDATION,
        APPLY_CANDIDATE_PATCHES, or MUTATE_POLICY.
        """
        forbidden_for_untrusted = {
            Capability.EXECUTE_VALIDATION.value,
            Capability.APPLY_CANDIDATE_PATCHES.value,
            Capability.MUTATE_POLICY.value,
            Capability.MUTATE_WAIVERS.value,
            Capability.EXPORT_PATCH.value,
            Capability.CREATE_WORKTREES.value,
        }

        if trust_level == TrustLevel.UNTRUSTED_EXTERNAL and capability.value in forbidden_for_untrusted:
            raise CapabilityDeniedError(
                f"Cannot grant '{capability.value}' to untrusted repository"
            )

        if trust_level == TrustLevel.DISABLED:
            raise CapabilityDeniedError(
                "Cannot grant capabilities to a disabled repository"
            )

        caps = list(current_capabilities)
        if capability.value not in caps:
            caps.append(capability.value)
        return caps

    @staticmethod
    def revoke(
        current_capabilities: List[str],
        capability: Capability,
    ) -> List[str]:
        """Revoke a capability."""
        return [c for c in current_capabilities if c != capability.value]

    @staticmethod
    def validate_capabilities(
        capabilities: List[str],
        trust_level: TrustLevel,
    ) -> List[str]:
        """Return list of invalid capabilities for the trust level."""
        forbidden_for_untrusted = {
            Capability.EXECUTE_VALIDATION.value,
            Capability.APPLY_CANDIDATE_PATCHES.value,
            Capability.MUTATE_POLICY.value,
            Capability.MUTATE_WAIVERS.value,
            Capability.EXPORT_PATCH.value,
            Capability.CREATE_WORKTREES.value,
        }

        forbidden_for_read_only = {
            Capability.WRITE_BRAIN_METADATA.value,
            Capability.APPLY_CANDIDATE_PATCHES.value,
            Capability.MUTATE_POLICY.value,
            Capability.MUTATE_WAIVERS.value,
            Capability.EXPORT_PATCH.value,
            Capability.UPDATE_BASELINE.value,
        }

        issues = []
        all_known = {c.value for c in Capability}

        for cap in capabilities:
            if cap not in all_known:
                issues.append(f"Unknown capability: {cap}")
                continue

            if trust_level == TrustLevel.DISABLED:
                issues.append(f"Disabled repository cannot have capability: {cap}")
            elif trust_level == TrustLevel.UNTRUSTED_EXTERNAL and cap in forbidden_for_untrusted:
                issues.append(f"Untrusted repository cannot have capability: {cap}")
            elif trust_level == TrustLevel.TRUSTED_READ_ONLY and cap in forbidden_for_read_only:
                issues.append(f"Read-only repository cannot have capability: {cap}")

        return issues
