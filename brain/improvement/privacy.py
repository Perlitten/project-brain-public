"""Privacy, Data Classification, and Retention Governance for Project Brain v0.7.0."""

from brain.improvement.models import PrivacyClassification, TrajectoryPrivacy


class PrivacyGuard:
    """Enforces zero-leakage privacy rules and training-eligibility policy."""

    @staticmethod
    def enforce_default_policy(privacy_data: TrajectoryPrivacy) -> TrajectoryPrivacy:
        """Forces training_eligible=False by default for production telemetry."""
        privacy_data.training_eligible = False
        return privacy_data

    @staticmethod
    def is_training_eligible(privacy: TrajectoryPrivacy) -> bool:
        """Explicit governance check for model fine-tuning eligibility."""
        if privacy.classification == PrivacyClassification.RESTRICTED_VAULT:
            return False
        return privacy.training_eligible  # Must be explicitly opted in
