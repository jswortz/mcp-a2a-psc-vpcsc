#!/usr/bin/env bash
# End-to-End Live GCP Provisioning & Fresh Gemini Enterprise App Verification Script
# Deploys the real Cloud Run MCP Server, PSC/ILB/DNS/AgentGateway/AgentRegistry stack,
# provisions a fresh Gemini Enterprise App (Engine) in your-gcp-project-id, binds it to
# the Agent Gateway + Agent Registry + MCP DataConnector, and runs the live E2E test suite.
#
# Usage:
#   ./scripts/run_live_gcp_e2e_proof.sh           # Full live execution in GCP
#   DRY_RUN=true ./scripts/run_live_gcp_e2e_proof.sh  # Safe dry-run preview
set -euo pipefail

export GCP_PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"
export GCP_PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-123456789012}"
export GCP_REGION="${GCP_REGION:-us-central1}"
export GE_APP_LOCATION="${GE_APP_LOCATION:-global}"
export GE_ENGINE_ID="${GE_ENGINE_ID:-ge-mcp-psc-vpcsc-app}"
export GE_MCP_CONNECTOR_ID="${GE_MCP_CONNECTOR_ID:-ge-mcp-psc-collector}"
export GE_MCP_DATASTORE_ID="${GE_MCP_DATASTORE_ID:-ge-mcp-psc-collector_mcp_data}"
DRY_RUN="${DRY_RUN:-false}"

run_cmd() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    echo "[EXEC] $*"
    "$@"
  fi
}

echo "=== 1. Deploy Live Cloud Run MCP & Dedicated A2A Servers (Internal Only over PSC) ==="
run_cmd gcloud run deploy internal-data-mcp \
  --source=. \
  --region="${GCP_REGION}" \
  --ingress=internal \
  --no-invoker-iam-check \
  --project="${GCP_PROJECT_ID}"

run_cmd gcloud run deploy internal-finance-a2a \
  --source=. \
  --region="${GCP_REGION}" \
  --ingress=internal \
  --no-invoker-iam-check \
  --set-env-vars="PUBLIC_SERVICE_URL=https://internal-finance-a2a-${GCP_PROJECT_NUMBER}.${GCP_REGION}.run.app" \
  --project="${GCP_PROJECT_ID}"

for svc in internal-data-mcp internal-finance-a2a; do
  for sa in \
    "serviceAccount:service-${GCP_PROJECT_NUMBER}@gcp-sa-discoveryengine.iam.gserviceaccount.com" \
    "serviceAccount:service-${GCP_PROJECT_NUMBER}@gcp-sa-agentgateway.iam.gserviceaccount.com"; do
    run_cmd gcloud run services add-iam-policy-binding "${svc}" \
      --region="${GCP_REGION}" \
      --member="${sa}" \
      --role="roles/run.invoker" \
      --project="${GCP_PROJECT_ID}"
  done
done

echo "=== 2. Provision PSC Network Attachment, Private DNS, Agent Gateway & Agent Registry ==="
DRY_RUN="${DRY_RUN}" ./scripts/deploy_gcp_infrastructure.sh

echo "=== 3. Create / Bind New Gemini Enterprise App (Discovery Engine) to Agent Gateway & A2A Agent ==="
if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[DRY-RUN] POST/PATCH https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_APP_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}"
  echo "[DRY-RUN] POST/PATCH https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_NUMBER}/locations/${GE_APP_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}/assistants/default_assistant/agents"
else
  ACCESS_TOKEN="$(gcloud auth print-access-token)"
  curl -sS -X PATCH \
    -H "Authorization: Bearer ${ACCESS_TOKEN}" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GCP_PROJECT_ID}" \
    "https://discoveryengine.googleapis.com/v1alpha/projects/${GCP_PROJECT_ID}/locations/${GE_APP_LOCATION}/collections/default_collection/engines/${GE_ENGINE_ID}?updateMask=agentGatewaySetting,associatedAgentRegistry,dataStoreIds,observabilityConfig" \
    -d "{
      \"observabilityConfig\": {
        \"observabilityEnabled\": true,
        \"sensitiveLoggingEnabled\": true
      },
      \"agentGatewaySetting\": {
        \"defaultEgressAgentGateway\": {
          \"name\": \"projects/${GCP_PROJECT_ID}/locations/${GCP_REGION}/agentGateways/ge-private-egress-gateway\"
        }
      },
      \"associatedAgentRegistry\": \"//agentregistry.googleapis.com/projects/${GCP_PROJECT_ID}/locations/${GCP_REGION}\",
      \"dataStoreIds\": [\"${GE_MCP_DATASTORE_ID}\"]
    }"
fi

echo "=== 4. Execute Live GCP + New Gemini Enterprise App End-to-End Pytest Suite ==="
if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[DRY-RUN] RUN_LIVE_GCP_E2E=true PYTHONPATH=src python3 -m pytest tests/test_live_gcp_gemini_enterprise.py -v"
else
  RUN_LIVE_GCP_E2E=true PYTHONPATH=src python3 -m pytest tests/test_live_gcp_gemini_enterprise.py -v
fi

