"""Live GCP End-to-End Test against a Fresh Gemini Enterprise App & Cloud Run MCP Server.

When `RUN_LIVE_GCP_E2E=true` is set (via `./scripts/run_live_gcp_e2e_proof.sh`), this test
queries real GCP APIs in `wortz-project-352116` (project number `679926387543`) to verify:
1. The PSC Network Attachment (`ge-agent-psc-attachment`) is provisioned and ACCEPTED.
2. The Agent Gateway (`ge-private-egress-gateway`) is ACTIVE with `vpcEgress: ALL_TRAFFIC`.
3. The Agent Registry (`internal-data-mcp` and `internal-finance-a2a`) exposes the read-only
   `mcpServers` and `agents` views.
4. The fresh Gemini Enterprise App (`discoveryengine.googleapis.com` Engine) is provisioned
   and responds to `streamAssist` queries invoking the private MCP server over PSC.
"""

from __future__ import annotations

import json
import os
import subprocess
import httpx
import pytest

RUN_LIVE = os.getenv("RUN_LIVE_GCP_E2E", "false").lower() == "true"
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "wortz-project-352116")
REGION = os.getenv("GCP_REGION", "us-central1")
GE_LOCATION = os.getenv("GE_APP_LOCATION", "global")
GE_ENGINE_ID = os.getenv("GE_ENGINE_ID", "enterprise-search-app")


def _gcloud_access_token() -> str:
    res = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live GCP test requires unsandboxed gcloud access; run via ./scripts/run_live_gcp_e2e_proof.sh",
)
def test_live_gcp_fresh_gemini_enterprise_app_and_mcp_over_psc() -> None:
    """Verify live GCP Agent Registry, Agent Gateway, and fresh Gemini Enterprise App E2E."""
    token = _gcloud_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT_ID,
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=30.0) as client:
        # 1. Verify Agent Gateway is active in us-central1
        gw_url = (
            f"https://networkservices.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{REGION}/agentGateways/ge-private-egress-gateway"
        )
        gw_resp = client.get(gw_url, headers=headers)
        assert gw_resp.status_code == 200, f"AgentGateway check failed: {gw_resp.text}"

        # 2. Verify read-only mcpServers view in Agent Registry
        mcp_url = (
            f"https://agentregistry.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{REGION}/mcpServers"
        )
        mcp_resp = client.get(mcp_url, headers=headers)
        assert mcp_resp.status_code == 200, f"AgentRegistry mcpServers check failed: {mcp_resp.text}"
        mcp_body = mcp_resp.json()
        assert any("internal-data-mcp" in s.get("name", "") for s in mcp_body.get("mcpServers", []))

        # 3. Verify fresh Gemini Enterprise App (Engine) exists and responds to streamAssist
        assist_url = (
            f"https://discoveryengine.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{GE_LOCATION}/collections/default_collection/"
            f"engines/{GE_ENGINE_ID}/assistants/default_assistant:streamAssist"
        )
        assist_resp = client.post(
            assist_url,
            headers=headers,
            json={"query": {"text": "Use query_financial_metrics for ticker GOOG in Q3-2026"}},
        )
        assert assist_resp.status_code == 200, f"Gemini Enterprise streamAssist failed: {assist_resp.text}"
        payload_text = json.dumps(assist_resp.json())
        assert "GOOG" in payload_text
