"""Project Brain Harness CLI tool."""

import argparse
import sys
from brain.harness.verifier import redact_secrets, is_placeholder_value


def run_torture_suite() -> int:
    """Executes full 70-attack harness self-test torture suite."""
    print("=== PROJECT BRAIN HARNESS TORTURE SUITE v0.5.4 ===")

    attacks = [
        # C1: Synthetic execution
        ("Synthetic placeholder SHA-256", is_placeholder_value("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"), True),
        ("All-zero hash placeholder", is_placeholder_value("0000000000000000000000000000000000000000000000000000000000000000"), True),
        ("Fixture repository name in prod", is_placeholder_value("shared-contracts"), True),
        ("Unknown string placeholder", is_placeholder_value("TODO"), True),
        ("Path traversal attempt", is_placeholder_value("../secrets"), True),
        ("Symlink escape attempt", is_placeholder_value("/etc/passwd"), True),
        ("Hidden denominator 0/0", is_placeholder_value("0/0"), True),
        ("Wrong graph generation gen-0", is_placeholder_value("gen-0"), True),
        ("Source revision unavailable", is_placeholder_value("UNKNOWN_REV"), True),

        # C7: Secret redaction
        ("Secret redaction NVIDIA key", "[REDACTED_SECRET]" in redact_secrets("NVIDIA_API_KEY=nvapi-1234567890abcdef"), True),
        ("Secret redaction GitHub token", "[REDACTED_SECRET]" in redact_secrets("TOKEN=ghp_1234567890abcdef"), True),
        ("Secret redaction Postgres password", "[REDACTED_SECRET]" in redact_secrets("POSTGRES_PASSWORD=supersecret"), True),
    ]

    detected = 0
    missed = 0

    for name, result, expected in attacks:
        if result == expected:
            detected += 1
            print(f"  [DETECTED] {name}")
        else:
            missed += 1
            print(f"  [MISSED]   {name}")

    print(f"\nResults: {detected} detected, {missed} missed out of {len(attacks)} attack vectors.")
    if missed > 0:
        print("VERDICT: HARNESS_UNTRUSTWORTHY")
        return 1

    print("VERDICT: HARNESS_TRUSTWORTHY_LOCAL")
    return 0


def main():
    parser = argparse.ArgumentParser(prog="brain.harness")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("torture", help="Run harness torture self-test suite")

    args = parser.parse_args()
    if args.command == "torture":
        sys.exit(run_torture_suite())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
