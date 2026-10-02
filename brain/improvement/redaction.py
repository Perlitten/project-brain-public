"""Two-Pass Fail-Closed Secret Redaction Engine for Telemetry and Trajectories."""

import re

# Pass 1: Deterministic secret patterns & high-entropy key patterns
SECRET_PATTERNS = [
    (re.compile(r"nvapi-[A-Za-z0-9_\-]+"), lambda m: "[REDACTED_SECRET]"),
    (re.compile(r"ghp_[A-Za-z0-9_\-]+"), lambda m: "[REDACTED_SECRET]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]+"), lambda m: "[REDACTED_SECRET]"),
    (re.compile(r"POSTGRES_PASSWORD=[^\s]+"), lambda m: "POSTGRES_PASSWORD=[REDACTED_SECRET]"),
    (re.compile(r"Authorization:\s*Bearer\s+[A-Za-z0-9_\.\-]+", re.IGNORECASE), lambda m: "Authorization: Bearer [REDACTED_SECRET]"),
]

# Pass 2: Path & identity pseudonymization
PATH_PATTERNS = [
    (re.compile(r"C:\\Users\\[A-Za-z0-9_]+\\"), lambda m: "C:\\Users\\[REDACTED_USER]\\"),
    (re.compile(r"/home/[A-Za-z0-9_]+/"), lambda m: "/home/[REDACTED_USER]/"),
]


def redact_text_pass1(text: str) -> str:
    """Pass 1: Deterministic secret & credential pattern redaction."""
    redacted = text
    for pat, repl_fn in SECRET_PATTERNS:
        redacted = pat.sub(repl_fn, redacted)
    return redacted


def redact_text_pass2(text: str) -> str:
    """Pass 2: Context minimization & path pseudonymization."""
    redacted = text
    for pat, repl_fn in PATH_PATTERNS:
        redacted = pat.sub(repl_fn, redacted)
    return redacted


def redact_trajectory_content(text: str) -> str:
    """Full two-pass fail-closed redaction pipeline."""
    if not text:
        return ""
    p1 = redact_text_pass1(text)
    p2 = redact_text_pass2(p1)

    # Fail-closed check: Ensure no unredacted API key patterns remain
    if "nvapi-" in p2 or "ghp_" in p2 or "sk-proj-" in p2:
        raise ValueError("CRITICAL: Redaction failed to neutralize credential pattern!")
    return p2
