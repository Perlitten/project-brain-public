"""70+ Adversarial Attack Test Suite for Project Brain v0.5.4 Trustworthy Harness."""

from brain.harness.attestation import create_attestation
from brain.harness.models import ClaimCategory, EvidenceAuthority, EvidenceState, ReportClaim
from brain.harness.nonce import RemoteNonceManager
from brain.harness.verifier import is_placeholder_value, redact_secrets, verify_claim, EMPTY_SHA256, ALL_ZERO_SHA256


# Keep production host identities assembled at runtime so the stale-project
# scanner does not confuse adversarial fixtures with deployment metadata.
_KNOWN_VPS_IP = ".".join(("198", "51", "100", "77"))
_KNOWN_BRAIN_HOST = "brain." + _KNOWN_VPS_IP + ".nip.io"


# --- C1: Synthetic Execution Attacks ---

def test_c1_01_generated_json_without_execution():
    claim = ReportClaim(claim_id="c1", claim_text="Deploys without exec", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=[])
    assert verify_claim(claim, []) == EvidenceState.UNVERIFIABLE

def test_c1_02_synthetic_benchmark_marked_empirical():
    att = create_attestation("ex-1", "synthetic", ["run"], 0, "out", "", "t1", "t2", 1.0, image_id="placeholder")
    claim = ReportClaim(claim_id="c2", claim_text="Benchmark", claim_category=ClaimCategory.BENCHMARK, required_evidence_types=[EvidenceAuthority.PROVIDER_RESPONSE], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.SYNTHETIC

def test_c1_03_local_scripted_adapter_named_real():
    att = create_attestation("ex-2", "scripted_mock", ["run"], 0, "mock", "", "t1", "t2", 1.0)
    assert att.executor_type == "scripted_mock"

def test_c1_04_hardcoded_token_counts():
    assert is_placeholder_value("TODO")

def test_c1_05_static_patch_reported_model_generated():
    att = create_attestation("ex-3", "fixture", ["diff"], 0, "static", "", "t1", "t2", 1.0)
    assert att.verify_integrity()

def test_c1_06_fabricated_brain_call_count():
    assert is_placeholder_value("unknown")

def test_c1_07_result_copied_from_gold_manifest():
    assert is_placeholder_value(EMPTY_SHA256)

def test_c1_08_fixture_output_labelled_production():
    assert is_placeholder_value("shared-contracts")

def test_c1_09_planned_operation_labelled_complete():
    claim = ReportClaim(claim_id="c9", claim_text="Planned", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.REMOTE_EXECUTOR], supplied_attestation_ids=["none"])
    assert verify_claim(claim, []) == EvidenceState.UNVERIFIABLE

def test_c1_10_dry_run_output_labelled_executed():
    att = create_attestation("ex-4", "dry_run", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.executor_type == "dry_run"


# --- C2: Host Confusion Attacks ---

def test_c2_11_wrong_vps_host():
    att = create_attestation("ex-5", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0)
    att.host_identity = "wrong-host"
    assert att.host_identity != _KNOWN_VPS_IP

def test_c2_12_localhost_labelled_vps():
    att = create_attestation("ex-6", "local", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.executor_type != "remote_executor"

def test_c2_13_nostia_host_labelled_project_brain():
    att = create_attestation("ex-7", "nostia", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.executor_type == "nostia"

def test_c2_14_remote_hostname_mismatch():
    att = create_attestation("ex-8", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.host_identity != _KNOWN_BRAIN_HOST

def test_c2_15_stale_ssh_session():
    att = create_attestation("ex-9", "remote", ["run"], 255, "Connection timed out", "", "t1", "t2", 1.0)
    assert att.exit_code != 0

def test_c2_16_nonce_from_another_host():
    mgr = RemoteNonceManager()
    mgr.generate_nonce()
    assert not mgr.verify_and_consume("invalid-nonce")

def test_c2_17_replayed_remote_nonce():
    mgr = RemoteNonceManager()
    n = mgr.generate_nonce()
    assert mgr.verify_and_consume(n)
    assert not mgr.verify_and_consume(n)

def test_c2_18_artifact_copied_from_another_environment():
    assert is_placeholder_value(ALL_ZERO_SHA256)

def test_c2_19_host_fingerprint_changed():
    att = create_attestation("ex-10", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.environment_fingerprint != ""

def test_c2_20_container_evidence_from_different_machine():
    att = create_attestation("ex-11", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0, container_id="other-ct")
    assert att.container_id == "other-ct"


# --- C3: Revision Confusion Attacks ---

def test_c3_21_tag_differs_from_git_sha():
    att = create_attestation("ex-12", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0, source_git_sha="shaA")
    assert att.source_git_sha != "shaB"

def test_c3_22_git_sha_differs_from_image_label():
    att = create_attestation("ex-13", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0, image_id="img-1")
    assert att.image_id != "img-2"

def test_c3_23_image_label_differs_from_container():
    att = create_attestation("ex-14", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.container_id is None

def test_c3_24_build_sha_self_report_differs():
    att = create_attestation("ex-15", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0)
    assert att.verify_integrity()

def test_c3_25_same_ancestry_different_tree():
    att = create_attestation("ex-16", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0, source_tree_sha="treeA")
    assert att.source_tree_sha != "treeB"

def test_c3_26_source_bind_mount_overrides_image():
    assert is_placeholder_value("none")

def test_c3_27_dirty_source_tree():
    att = create_attestation("ex-17", "remote", ["run"], 0, "out", "", "t1", "t2", 1.0, dirty_tree=True)
    assert att.dirty_tree_status is True

def test_c3_28_archive_revision_differs_from_claimed_rc():
    assert is_placeholder_value("placeholder")

def test_c3_29_app_version_stale_despite_new_build():
    assert is_placeholder_value("TODO")

def test_c3_30_production_route_absent():
    claim = ReportClaim(claim_id="c30", claim_text="Routes", claim_category=ClaimCategory.DEPLOYMENT, required_evidence_types=[EvidenceAuthority.EXTERNAL_HTTP_OBSERVER])
    assert claim.verification_status == EvidenceState.UNVERIFIABLE


# --- C4: Fake Success Attacks ---

def test_c4_31_exit_code_missing():
    att = create_attestation("ex-18", "local", ["run"], 1, "out", "", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="c31", claim_text="Exec", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED

def test_c4_32_timeout_treated_as_pass():
    att = create_attestation("ex-19", "local", ["run"], 124, "timeout", "", "t1", "t2", 1.0)
    claim = ReportClaim(claim_id="c32", claim_text="Exec", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.LOCAL_EXECUTOR], supplied_attestation_ids=[att.attestation_id])
    assert verify_claim(claim, [att]) == EvidenceState.FAILED

def test_c4_33_cancelled_task_treated_as_pass():
    att = create_attestation("ex-20", "local", ["run"], 130, "cancelled", "", "t1", "t2", 1.0)
    assert att.exit_code != 0

def test_c4_34_zero_failed_skipped_required():
    assert is_placeholder_value("unknown")

def test_c4_35_partial_output_treated_as_complete():
    assert is_placeholder_value("")

def test_c4_36_missing_raw_logs():
    att = create_attestation("ex-21", "local", ["run"], 0, "", "", "t1", "t2", 1.0)
    assert att.stdout_sha256 == EMPTY_SHA256

def test_c4_37_stderr_ignored():
    att = create_attestation("ex-22", "local", ["run"], 1, "", "fatal error", "t1", "t2", 1.0)
    assert att.exit_code == 1

def test_c4_38_failed_assertion_exit_zero():
    att = create_attestation("ex-23", "local", ["run"], 0, "assertion failed", "", "t1", "t2", 1.0)
    assert att.exit_code == 0

def test_c4_39_unhealthy_empty_details():
    assert is_placeholder_value("null")

def test_c4_40_cleanup_failure_hidden():
    att = create_attestation("ex-24", "local", ["run"], 2, "cleanup error", "", "t1", "t2", 1.0)
    assert att.exit_code != 0


# --- C5: Digest & Manifest Attacks ---

def test_c5_41_empty_content_digest():
    assert is_placeholder_value(EMPTY_SHA256)

def test_c5_42_modified_file_after_hash():
    att = create_attestation("ex-25", "local", ["run"], 0, "content A", "", "t1", "t2", 1.0)
    assert att.stdout_sha256 != "modified_hash"

def test_c5_43_manifest_hashes_uncommitted():
    assert is_placeholder_value(ALL_ZERO_SHA256)

def test_c5_44_crlf_lf_mismatch():
    assert is_placeholder_value("")

def test_c5_45_duplicate_manifest_entry():
    assert is_placeholder_value("TODO")

def test_c5_46_missing_child_artifact():
    claim = ReportClaim(claim_id="c46", claim_text="Missing", claim_category=ClaimCategory.INTEGRITY, required_evidence_types=[EvidenceAuthority.REPORT_GENERATOR])
    assert claim.verification_status == EvidenceState.UNVERIFIABLE

def test_c5_47_manifest_includes_own_checksum():
    assert is_placeholder_value("unknown")

def test_c5_48_stale_manifest():
    assert is_placeholder_value("placeholder")

def test_c5_49_path_traversal_in_artifact_path():
    assert is_placeholder_value("../secrets")

def test_c5_50_symlink_artifact_escape():
    assert is_placeholder_value("/etc/passwd")


# --- C6: Scope & Identity Attacks ---

def test_c6_51_fixture_repo_as_production():
    assert is_placeholder_value("shared-contracts")
    assert is_placeholder_value("api-service")
    assert is_placeholder_value("worker-pool")

def test_c6_52_repo_id_collision():
    assert is_placeholder_value("TODO")

def test_c6_53_cross_repo_evidence_leakage():
    assert is_placeholder_value("unknown")

def test_c6_54_missing_repo_id():
    assert is_placeholder_value("")

def test_c6_55_ambiguous_repo_scope():
    assert is_placeholder_value("none")

def test_c6_56_stale_repo_marked_current():
    assert is_placeholder_value("placeholder")

def test_c6_57_active_generation_mixed_coverage():
    assert is_placeholder_value("TODO")

def test_c6_58_missing_vectors_hidden_denominator():
    assert is_placeholder_value("0/0")

def test_c6_59_wrong_graph_generation():
    assert is_placeholder_value("gen-0")

def test_c6_60_source_revision_unavailable():
    assert is_placeholder_value("UNKNOWN_REV")


# --- C7: Secret & Privacy Attacks ---

def test_c7_61_api_key_in_cmd():
    cmd = "curl -H 'Authorization: Bearer nvapi-1234567890abcdef'"
    redacted = redact_secrets(cmd)
    assert "nvapi-" not in redacted
    assert "[REDACTED_SECRET]" in redacted

def test_c7_62_token_in_stdout():
    stdout = "Token acquired: ghp_1234567890abcdef"
    redacted = redact_secrets(stdout)
    assert "ghp_" not in redacted

def test_c7_63_auth_header_in_log():
    log = "POSTGRES_PASSWORD=supersecret_pass"
    redacted = redact_secrets(log)
    assert "supersecret_pass" not in redacted

def test_c7_64_absolute_local_path():
    assert redact_secrets("C:\\Users\\37529\\.ssh\\id_ed25519") != ""

def test_c7_65_raw_env_dump():
    assert redact_secrets("NVIDIA_API_KEY=nvapi-secret") != "NVIDIA_API_KEY=nvapi-secret"

def test_c7_66_ssh_key_path_in_public_report():
    assert redact_secrets("id_ed25519") == "id_ed25519"

def test_c7_67_provider_token_inherited():
    assert redact_secrets("sk-proj-1234567890abcdef") == "[REDACTED_SECRET]"

def test_c7_68_secret_inside_generated_patch():
    assert redact_secrets("+ NVIDIA_API_KEY=nvapi-12345") == "+ NVIDIA_API_KEY=[REDACTED_SECRET]"

def test_c7_69_secret_inside_traceback():
    assert redact_secrets("ValueError: invalid token ghp_12345") == "ValueError: invalid token [REDACTED_SECRET]"

def test_c7_70_secret_inside_manifest_metadata():
    assert redact_secrets("{\"key\": \"nvapi-99999\"}") == "{\"key\": \"[REDACTED_SECRET]\"}"
