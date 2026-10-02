"""Live GCP End-to-End Test against the New Gemini Enterprise Instance & Private Cloud Run MCP/A2A.

When `RUN_LIVE_GCP_E2E=true` is set (via `./scripts/run_live_gcp_e2e_proof.sh`), this test
queries real GCP APIs in `your-gcp-project-id` (project number `123456789012`) to verify:
1. The PSC Network Attachment (`ge-agent-psc-attachment`) is provisioned and ACCEPTED (`10.128.20.2`).
2. The Private Cloud Run service (`internal-data-mcp`) is active with `INGRESS_TRAFFIC_INTERNAL_ONLY`.
3. The Agent Gateway (`ge-private-egress-gateway`) is ACTIVE with `AGENT_TO_ANYWHERE`, bound to
   Agent Registry, `ge-agent-psc-attachment`, and Private DNS peering (`run.app.`, `internal.corp.example.com.`).
4. IAP v2 AuthzExtension (`ge-iap-authz-ext`), AuthzPolicy (`ge-gateway-iap-policy`), and IAM v3
   Unified Access Policy (`ge-mcp-a2a-uap` + `ge-mcp-a2a-uap-binding`) are active.
5. The Agent Registry (`internal-data-mcp`, `onprem-erp-mcp`, and `internal-finance-a2a`) exposes
   the read-only `mcpServers` and `agents` views.
6. The new Gemini Enterprise Instance (`ge-mcp-psc-vpcsc-app`) is bound to `ge-private-egress-gateway`,
   connected to the Registry MCP DataConnector (`ge-mcp-psc-collector`) and A2A Agent, and executes
   a live `:streamAssist` tool call (`query_financial_metrics`) over Agent Gateway + PSC to the
   private Cloud Run MCP server, returning the verified `$96,450M` and `33.4%` financial metrics.
"""

from __future__ import annotations

import json
import os
import subprocess
import httpx
import pytest

RUN_LIVE = os.getenv("RUN_LIVE_GCP_E2E", "false").lower() == "true"
PROJECT_ID = os.getenv("GCP_PROJECT_ID", "your-gcp-project-id")
PROJECT_NUMBER = os.getenv("GCP_PROJECT_NUMBER", "123456789012")
REGION = os.getenv("GCP_REGION", "us-central1")
GE_LOCATION = os.getenv("GE_APP_LOCATION", "global")
GE_ENGINE_ID = os.getenv("GE_ENGINE_ID", "ge-mcp-psc-vpcsc-app")
GE_MCP_DATASTORE_ID = os.getenv("GE_MCP_DATASTORE_ID", "ge-mcp-psc-collector_mcp_data")


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
    """Verify live GCP PSC, Agent Registry, Agent Gateway, IAP/UAP, and Gemini Enterprise App E2E."""
    token = _gcloud_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT_ID,
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60.0) as client:
        # 1. Verify PSC Network Attachment has an ACCEPTED connection from Agent Gateway
        na_url = (
            f"https://compute.googleapis.com/compute/v1/"
            f"projects/{PROJECT_ID}/regions/{REGION}/networkAttachments/ge-agent-psc-attachment"
        )
        na_resp = client.get(na_url, headers=headers)
        assert na_resp.status_code == 200, f"PSC Network Attachment check failed: {na_resp.text}"
        na_body = na_resp.json()
        assert na_body.get("connectionPreference") == "ACCEPT_AUTOMATIC"
        conn_endpoints = na_body.get("connectionEndpoints", [])
        assert any(ep.get("status") == "ACCEPTED" for ep in conn_endpoints), (
            f"Expected ACCEPTED PSC connection endpoint on ge-agent-psc-attachment: {conn_endpoints}"
        )

        # 2. Verify Private Cloud Run MCP & Dedicated A2A Services are deployed with INGRESS_TRAFFIC_INTERNAL_ONLY
        for svc_id in ("internal-data-mcp", "internal-finance-a2a"):
            run_url = (
                f"https://run.googleapis.com/v2/"
                f"projects/{PROJECT_ID}/locations/{REGION}/services/{svc_id}"
            )
            run_resp = client.get(run_url, headers=headers)
            assert run_resp.status_code == 200, f"Cloud Run service check ({svc_id}) failed: {run_resp.text}"
            run_body = run_resp.json()
            assert run_body.get("ingress") == "INGRESS_TRAFFIC_INTERNAL_ONLY"

        # 3. Verify Agent Gateway is active in us-central1 with PSC Network Attachment & Registry
        gw_url = (
            f"https://networkservices.googleapis.com/v1alpha1/"
            f"projects/{PROJECT_ID}/locations/{REGION}/agentGateways/ge-private-egress-gateway"
        )
        gw_resp = client.get(gw_url, headers=headers)
        assert gw_resp.status_code == 200, f"AgentGateway check failed: {gw_resp.text}"
        gw_body = gw_resp.json()
        assert gw_body["googleManaged"]["governedAccessPath"] == "AGENT_TO_ANYWHERE"
        assert "ge-agent-psc-attachment" in gw_body["networkConfig"]["egress"]["networkAttachment"]
        assert any("agentregistry.googleapis.com" in r for r in gw_body.get("registries", []))

        # 3b. Verify Split-Horizon Private Cloud DNS zones for run.app. and internal.corp.example.com.
        dns_url = f"https://dns.googleapis.com/dns/v1/projects/{PROJECT_ID}/managedZones"
        dns_resp = client.get(dns_url, headers=headers)
        assert dns_resp.status_code == 200, f"Cloud DNS check failed: {dns_resp.text}"
        dns_names = [z.get("dnsName", "") for z in dns_resp.json().get("managedZones", [])]
        assert "run.app." in dns_names
        assert "internal.corp.example.com." in dns_names

        # 4. Verify IAP v2 AuthzExtension, AuthzPolicy, and IAM v3 Unified Access Policy
        ext_url = (
            f"https://networkservices.googleapis.com/v1/"
            f"projects/{PROJECT_ID}/locations/{REGION}/authzExtensions/ge-iap-authz-ext"
        )
        ext_resp = client.get(ext_url, headers=headers)
        assert ext_resp.status_code == 200, f"AuthzExtension check failed: {ext_resp.text}"
        ext_body = ext_resp.json()
        assert ext_body.get("failOpen", False) is False
        assert ext_body.get("service") == "iap.googleapis.com"
        assert ext_body.get("metadata", {}).get("iapPolicyVersion") == "V2"

        pol_url = (
            f"https://networksecurity.googleapis.com/v1/"
            f"projects/{PROJECT_ID}/locations/{REGION}/authzPolicies/ge-gateway-iap-policy"
        )
        pol_resp = client.get(pol_url, headers=headers)
        assert pol_resp.status_code == 200, f"AuthzPolicy check failed: {pol_resp.text}"
        assert pol_resp.json().get("action") == "CUSTOM"

        uap_url = (
            f"https://iam.googleapis.com/v3/"
            f"projects/{PROJECT_ID}/locations/global/accessPolicies/ge-mcp-a2a-uap"
        )
        uap_resp = client.get(uap_url, headers=headers)
        assert uap_resp.status_code == 200, f"IAM v3 AccessPolicy check failed: {uap_resp.text}"
        uap_rules_text = json.dumps(uap_resp.json().get("details", {}).get("rules", []))
        assert "destination.is_registered == true" in uap_rules_text
        assert "query_financial_metrics" in uap_rules_text

        # 5. Verify read-only mcpServers and agents views in Agent Registry
        mcp_url = (
            f"https://agentregistry.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{REGION}/mcpServers"
        )
        mcp_resp = client.get(mcp_url, headers=headers)
        assert mcp_resp.status_code == 200, f"AgentRegistry mcpServers check failed: {mcp_resp.text}"
        mcp_servers = mcp_resp.json().get("mcpServers", [])
        mcp_names = [s.get("displayName", "") + " " + s.get("name", "") for s in mcp_servers]
        assert any("Internal Data MCP" in n or "11111111-1111-1111-1111-111111111111" in n for n in mcp_names)
        assert any("On-Prem ERP MCP" in n or "22222222-2222-2222-2222-222222222222" in n for n in mcp_names)

        agents_url = (
            f"https://agentregistry.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{REGION}/agents"
        )
        agents_resp = client.get(agents_url, headers=headers)
        assert agents_resp.status_code == 200, f"AgentRegistry agents check failed: {agents_resp.text}"
        agents_list = agents_resp.json().get("agents", [])
        assert any("Internal Finance Analysis A2A Agent" in a.get("displayName", "") for a in agents_list)
        assert any("internal-finance-a2a" in json.dumps(a) for a in agents_list)

        # 6. Verify New Gemini Enterprise Engine is bound to Agent Gateway & Registry
        engine_url = (
            f"https://discoveryengine.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{GE_LOCATION}/collections/default_collection/"
            f"engines/{GE_ENGINE_ID}"
        )
        engine_resp = client.get(engine_url, headers=headers)
        assert engine_resp.status_code == 200, f"Gemini Enterprise Engine check failed: {engine_resp.text}"
        engine_body = engine_resp.json()
        egress_gw = (
            engine_body.get("agentGatewaySetting", {})
            .get("defaultEgressAgentGateway", {})
            .get("name", "")
        )
        assert "ge-private-egress-gateway" in egress_gw
        assert "agentregistry.googleapis.com" in engine_body.get("associatedAgentRegistry", "")

        # 7. Execute live :streamAssist tool call over Agent Gateway -> PSC -> Private Cloud Run MCP
        assist_url = (
            f"https://discoveryengine.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{GE_LOCATION}/collections/default_collection/"
            f"engines/{GE_ENGINE_ID}/assistants/default_assistant:streamAssist"
        )
        assist_resp = client.post(
            assist_url,
            headers=headers,
            json={
                "query": {
                    "text": "Use query_financial_metrics for ticker GOOG in quarter Q3-2026."
                },
                "toolsSpec": {
                    "vertexAiSearchSpec": {
                        "dataStoreSpecs": [
                            {
                                "dataStore": (
                                    f"projects/{PROJECT_NUMBER}/locations/{GE_LOCATION}/"
                                    f"collections/default_collection/dataStores/{GE_MCP_DATASTORE_ID}"
                                )
                            }
                        ]
                    }
                },
            },
        )
        assert assist_resp.status_code == 200, f"Gemini Enterprise streamAssist failed: {assist_resp.text}"
        chunks = assist_resp.json()
        full_reply_text = "".join(
            reply.get("groundedContent", {}).get("content", {}).get("text", "")
            for chunk in chunks
            for reply in chunk.get("answer", {}).get("replies", [])
        )
        combined_text = full_reply_text + " " + json.dumps(chunks)
        assert "GOOG" in combined_text
        assert "96,450" in combined_text or "96450" in combined_text, (
            f"Expected live MCP revenue metric (96,450) in streamAssist response: {combined_text}"
        )
        assert "33.4" in combined_text, (
            f"Expected live MCP operating margin (33.4%) in streamAssist response: {combined_text}"
        )


@pytest.mark.skipif(
    not RUN_LIVE,
    reason="Live GCP test requires unsandboxed gcloud access; run via ./scripts/run_live_gcp_e2e_proof.sh",
)
def test_live_gemini_enterprise_a2a_agent_invocation_and_idempotency() -> None:
    """Verify live Gemini Enterprise A2A Agent invocation using official documented A2A endpoints.

    Follows https://docs.cloud.google.com/gemini/enterprise/docs/invoke-agent-a2a:
    1. Discover agent in v1alpha assistant agents list & Agent Registry.
    2. Fetch agent card via GET .../assistants/default_assistant/agents/{AGENT_ID}/a2a/v1/card.
    3. Invoke agent twice consecutively via POST .../a2a/v1/message:send with role=ROLE_USER,
       content=[{"text": ...}], and unique messageId.
    """
    import uuid

    token = _gcloud_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT_ID,
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=60.0) as client:
        # Step 1: Discover the registered A2A Agent on the Gemini Enterprise Assistant (v1alpha per docs)
        agents_list_url = (
            f"https://discoveryengine.googleapis.com/v1alpha/"
            f"projects/{PROJECT_ID}/locations/{GE_LOCATION}/collections/default_collection/"
            f"engines/{GE_ENGINE_ID}/assistants/default_assistant/agents"
        )
        agents_resp = client.get(agents_list_url, headers=headers)
        assert agents_resp.status_code == 200, f"Assistant agents list failed: {agents_resp.text}"
        agents_list = agents_resp.json().get("agents", [])
        finance_agents = [
            a for a in agents_list if "Internal Finance" in a.get("displayName", "")
        ]
        assert finance_agents, f"Expected 'Internal Finance' A2A Agent on assistant: {agents_list}"
        a2a_agent_resource = finance_agents[0]["name"]
        a2a_agent_id = a2a_agent_resource.split("/")[-1]
        assert finance_agents[0].get("state") == "ENABLED"
        assert "internal-finance-a2a" in finance_agents[0].get("a2aAgentDefinition", {}).get("jsonAgentCard", "")

        # Step 2: Fetch the agent card via documented GET /a2a/v1/card endpoint (uses PROJECT_NUMBER on v1)
        base_a2a_url = (
            f"https://discoveryengine.googleapis.com/v1/"
            f"projects/{PROJECT_NUMBER}/locations/{GE_LOCATION}/collections/default_collection/"
            f"engines/{GE_ENGINE_ID}/assistants/default_assistant/agents/{a2a_agent_id}/a2a"
        )
        card_resp = client.get(f"{base_a2a_url}/v1/card", headers=headers)
        assert card_resp.status_code == 200, f"A2A GET /v1/card failed: {card_resp.text}"
        card_body = card_resp.json()
        assert "Internal Finance" in card_body.get("name", "")

        # Step 3: Send 2 consecutive messages via documented POST /a2a/v1/message:send endpoint
        for attempt in (1, 2):
            send_resp = client.post(
                f"{base_a2a_url}/v1/message:send",
                headers=headers,
                json={
                    "message": {
                        "role": "ROLE_USER",
                        "content": [
                            {
                                "text": "Analyze GOOG Q3-2026 financial variance and executive margin highlights."
                            }
                        ],
                        "messageId": str(uuid.uuid4()),
                    }
                },
            )
            assert send_resp.status_code == 200, (
                f"A2A POST /v1/message:send attempt #{attempt} failed: {send_resp.text}"
            )
            send_body = send_resp.json()
            msg = send_body.get("message", {})
            assert msg.get("role") == "ROLE_AGENT", f"Expected ROLE_AGENT: {send_body}"
            reply_text = "".join(part.get("text", "") for part in msg.get("content", []))
            assert "96,450" in reply_text and "33.4%" in reply_text, (
                f"Expected Q3-2026 variance report from internal-finance-a2a on attempt #{attempt}: {reply_text}"
            )




