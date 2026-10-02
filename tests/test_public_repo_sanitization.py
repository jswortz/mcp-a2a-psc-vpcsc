"""Automated verification that the repository is sanitized and safe for public release."""

from __future__ import annotations

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

FORBIDDEN_SUBSTRINGS = (
    "wortz-project-352116",
    "jwortz",
    "/usr/local/google/home/",
    "679926387543",
    "1060412978793",
    "16bjMEo",
    "1zETenOHe",
    "docs.google.com/document/",
    "docs.google.com/presentation/",
    "booking-mcp",
    "expense-mcp",
    "bq-flywheel",
    "adk-finops-agent",
    "simple_time_agent_gw",
    "4213-411a00778a8c",
    "cf69-cfc1c5295c6f",
    "b48f-cf11f0cd6985",
    "15947165608667115817",
    "35.199.192.230",
)

SECRET_REGEXES = {
    "gcp_api_key": re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    "gcp_oauth_token": re.compile(r"ya29\.[0-9A-Za-z\-_]+"),
    "github_token": re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}"),
    "hardcoded_jwt": re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "pem_private_key_block": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]{32,}?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}

SKIP_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "mcp_a2a_psc_vpcsc.egg-info",
    ".benchmarks",
}


def _iter_repo_text_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix in {".png", ".pyc", ".so"}:
            continue
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue
        files.append(path)
    return files


def test_no_sensitive_identifiers_in_repo_files() -> None:
    """Verify no internal project IDs, org IDs, local paths, or internal doc URLs exist."""
    violations: list[str] = []
    for path in _iter_repo_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO_ROOT)
        for forbidden in FORBIDDEN_SUBSTRINGS:
            if forbidden.lower() in text.lower():
                violations.append(f"{rel}: contains forbidden substring '{forbidden}'")
    assert not violations, "Found unsanitized internal identifiers:\n" + "\n".join(violations)


def test_no_cryptographic_secrets_or_tokens() -> None:
    """Verify no API keys, OAuth tokens, JWTs, or PEM private key blocks exist."""
    violations: list[str] = []
    for path in _iter_repo_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        rel = path.relative_to(REPO_ROOT)
        for label, pattern in SECRET_REGEXES.items():
            if pattern.search(text):
                violations.append(f"{rel}: matched secret pattern '{label}'")
    assert not violations, "Found potential cryptographic secrets:\n" + "\n".join(violations)


def test_license_and_secret_gitignore_rules() -> None:
    """Verify Apache 2.0 LICENSE and secret-blocking .gitignore entries exist."""
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), "Missing LICENSE file for public release"
    assert "Apache License" in license_path.read_text(encoding="utf-8")

    gitignore_text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for required_pattern in (".env", "jwt_secret.txt", "*.pem", "*.key"):
        assert required_pattern in gitignore_text


def test_sanitized_placeholders_consistent() -> None:
    """Verify placeholder project ID, project number, and org ID are used consistently."""
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "GCP_PROJECT_ID=your-gcp-project-id" in env_example
    assert "GCP_PROJECT_NUMBER=123456789012" in env_example
    assert "GCP_ORG_ID=987654321098" in env_example


def test_git_branch_history_is_clean() -> None:
    """Verify that the HEAD commit on the current branch adds no sensitive identifiers."""
    import subprocess
    git_dir = REPO_ROOT / ".git"
    if not git_dir.exists():
        return
    res = subprocess.run(
        ["git", "show", "-p", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return
    # Check commit metadata header and added ('+') lines outside test_public_repo_sanitization.py
    checked_lines = []
    in_diff = False
    skip_file = False
    for line in res.stdout.splitlines():
        if line.startswith("diff --git "):
            in_diff = True
            skip_file = "test_public_repo_sanitization.py" in line
            continue
        if not in_diff:
            checked_lines.append(line)
        elif not skip_file and line.startswith("+") and not line.startswith("+++"):
            checked_lines.append(line)
    history_text = "\n".join(checked_lines)
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden.lower() not in history_text.lower(), (
            f"HEAD commit still introduces forbidden identifier: {forbidden}"
        )

