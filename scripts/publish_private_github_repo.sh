#!/usr/bin/env bash
# Creates a private GitHub repository and pushes the local git history.
# Usage: ./scripts/publish_private_github_repo.sh [repo-name]
set -euo pipefail

REPO_NAME="${1:-mcp-a2a-psc-vpcsc}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${REPO_DIR}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: GitHub CLI (gh) is not installed. Run: sudo apt install -y gh" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Authenticating with GitHub..."
  gh auth login --web --git-protocol https
fi

echo "Creating private GitHub repository '${REPO_NAME}' and pushing main branch..."
gh repo create "${REPO_NAME}" \
  --private \
  --description "Private Connectivity Reference Architecture & E2E Tests: Custom MCP (BYO-MCP) & A2A with PSC and VPC-SC" \
  --source=. \
  --remote=origin \
  --push

echo "Successfully published private repository:"
gh repo view --web || true
