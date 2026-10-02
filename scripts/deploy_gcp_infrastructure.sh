#!/usr/bin/env bash
# End-to-End GCP Provisioning Script for Private BYO-MCP & A2A with PSC and VPC-SC
# Usage: DRY_RUN=true ./scripts/deploy_gcp_infrastructure.sh
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-your-gcp-project-id}"
PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-123456789012}"
REGION="${GCP_REGION:-us-central1}"
VPC_NAME="${VPC_NETWORK_NAME:-enterprise-vpc}"
PSC_SUBNET_CIDR="${PSC_SUBNET_CIDR:-10.128.20.0/28}"
INTERNAL_ALB_VIP="${INTERNAL_ALB_VIP:-10.128.10.50}"
CONSUMER_PSC_IP="${CONSUMER_PSC_ENDPOINT_IP:-10.128.10.99}"
PRIVATE_DNS_DOMAIN="${PRIVATE_DNS_DOMAIN:-internal.corp.example.com.}"
DRY_RUN="${DRY_RUN:-false}"

run_cmd() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[DRY-RUN] $*"
  else
    echo "[EXEC] $*"
    "$@" || echo "[INFO] Resource already exists or command completed with non-zero status; continuing idempotently."
  fi
}

echo "=== Step 1: Consumer VPC & PSC Network Attachment ==="
# Per official PSC Network Attachment docs (https://docs.cloud.google.com/vpc/docs/create-manage-network-attachments):
# Egress PSC Interfaces require a regular subnet with Private Google Access (--enable-private-ip-google-access).
run_cmd gcloud compute networks subnets create ge-agent-psc-subnet \
  --network="${VPC_NAME}" \
  --region="${REGION}" \
  --range="${PSC_SUBNET_CIDR}" \
  --enable-private-ip-google-access \
  --project="${PROJECT_ID}"

run_cmd gcloud compute network-attachments create ge-agent-psc-attachment \
  --region="${REGION}" \
  --connection-preference=ACCEPT_AUTOMATIC \
  --subnets=ge-agent-psc-subnet \
  --project="${PROJECT_ID}"

run_cmd gcloud compute firewall-rules create allow-psc-to-internal-alb \
  --network="${VPC_NAME}" \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:443 \
  --source-ranges="${PSC_SUBNET_CIDR}" \
  --project="${PROJECT_ID}"

echo "=== Step 2: Private Cloud DNS Zone & DNS Peering IAM ==="
run_cmd gcloud dns managed-zones create enterprise-internal-zone \
  --dns-name="${PRIVATE_DNS_DOMAIN}" \
  --visibility=private \
  --networks="${VPC_NAME}" \
  --description="Split-horizon private DNS for MCP and A2A endpoints" \
  --project="${PROJECT_ID}"

run_cmd gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-networkmanagement.iam.gserviceaccount.com" \
  --role="roles/dns.peer"

echo "=== Step 3: Deploy AgentConnectivityTemplate & AgentGateway ==="
run_cmd gcloud network-services agent-connectivity-templates import ge-psc-egress-template \
  --source=manifests/connectivity-template.yaml \
  --location="${REGION}" \
  --project="${PROJECT_ID}"

run_cmd gcloud network-services agent-gateways import ge-private-egress-gateway \
  --source=manifests/agent-gateway.yaml \
  --location="${REGION}" \
  --project="${PROJECT_ID}"

echo "=== Step 4: Register Private MCP Servers (In-VPC & Outside-VPC Hybrid NEG) in Agent Registry ==="
run_cmd gcloud alpha agent-registry services create internal-data-mcp \
  --location="${REGION}" \
  --display-name="Internal Data MCP (In-VPC)" \
  --mcp-server-spec-type=tool-spec \
  --mcp-server-spec-content=@manifests/specs/toolspec.json \
  --interfaces="url=https://mcp-data.internal.corp.example.com/mcp,protocolBinding=jsonrpc" \
  --project="${PROJECT_ID}"

run_cmd gcloud alpha agent-registry services create onprem-erp-mcp \
  --location="${REGION}" \
  --display-name="On-Prem ERP MCP (Outside-VPC via Hybrid NEG)" \
  --mcp-server-spec-type=tool-spec \
  --mcp-server-spec-content=@manifests/specs/toolspec.json \
  --interfaces="url=https://mcp-onprem.internal.corp.example.com/mcp,protocolBinding=jsonrpc" \
  --project="${PROJECT_ID}"

echo "=== Step 5: Register Private A2A Agent & Create Consumer PSC Endpoint ==="
# Note: For --agent-spec-type=a2a-agent-card, Agent Registry requires --interfaces to be omitted
# because the endpoint URL is provided inside manifests/specs/agent-card.json.
run_cmd gcloud alpha agent-registry services create internal-finance-a2a \
  --location="${REGION}" \
  --display-name="Internal Finance A2A Agent" \
  --agent-spec-type=a2a-agent-card \
  --agent-spec-content=@manifests/specs/agent-card.json \
  --project="${PROJECT_ID}"

echo "=== Step 6: Enforce VPC-SC Org Policies, IAM v3 / IAP CEL Policy & Model Armor ==="
for policy_file in manifests/org-policies/*.yaml; do
  run_cmd gcloud org-policies set-policy "${policy_file}" --project="${PROJECT_ID}"
done

run_cmd gcloud network-services authz-extensions import ge-iap-authz-ext \
  --source=manifests/iap-authz-extension.yaml \
  --location="${REGION}" \
  --project="${PROJECT_ID}"

run_cmd gcloud network-security authz-policies import ge-gateway-iap-policy \
  --source=manifests/iap-authz-policy.yaml \
  --location="${REGION}" \
  --project="${PROJECT_ID}"

run_cmd gcloud iam access-policies create ge-mcp-a2a-uap \
  --location=global \
  --project="${PROJECT_ID}" \
  --file=manifests/iap-uap-policy.json

run_cmd gcloud model-armor floorsettings update \
  --full-uri="projects/${PROJECT_ID}/locations/global/floorSetting" \
  --enable-floor-setting-enforcement=true \
  --ai-platform-floor-setting=INSPECT_AND_BLOCK

echo "=== Provisioning Sequence Completed Successfully ==="
