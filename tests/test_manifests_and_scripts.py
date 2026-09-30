"""Unit tests validating declarative GCP manifests, schemas, and shell scripts."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_connectivity_template_manifest() -> None:
    """Verify AgentConnectivityTemplate enforces AGENT_TO_ANYWHERE and vpcEgress: ALL_TRAFFIC."""
    path = REPO_ROOT / "manifests" / "connectivity-template.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["accessPath"] == "AGENT_TO_ANYWHERE"
    assert data["vpcEgress"] == "ALL_TRAFFIC", "VPC-SC requires vpcEgress: ALL_TRAFFIC"
    assert "networkAttachments/" in data["networkAttachment"]
    dns_configs = data["dnsPeeringConfigs"]
    assert len(dns_configs) == 1
    assert dns_configs[0]["domain"].endswith(".")
    assert dns_configs[0]["targetNetwork"] == "enterprise-vpc"


def test_agent_gateway_manifest() -> None:
    """Verify AgentGateway binds to AgentConnectivityTemplate and Agent Registry."""
    path = REPO_ROOT / "manifests" / "agent-gateway.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "MCP" in data["protocols"]
    assert data["googleManaged"]["governedAccessPath"] == "AGENT_TO_ANYWHERE"
    assert "agentConnectivityTemplates/" in data["networkConfig"]["egress"]["agentConnectivityTemplate"]
    assert len(data["registryBindings"]) >= 1
    assert any("discoveryengine.googleapis.com" in r for r in data["governedResources"])


def test_iap_authz_manifests_and_uap_policy() -> None:
    """Verify AuthzExtension, AuthzPolicy, and IAM v3 UAP CEL condition."""
    ext = yaml.safe_load((REPO_ROOT / "manifests" / "iap-authz-extension.yaml").read_text(encoding="utf-8"))
    assert ext["service"] == "iap.googleapis.com"
    assert ext["failOpen"] is False
    assert ext["metadata"]["iapPolicyVersion"] == "V2"

    pol = yaml.safe_load((REPO_ROOT / "manifests" / "iap-authz-policy.yaml").read_text(encoding="utf-8"))
    assert pol["action"] == "CUSTOM"
    assert pol["policyProfile"] == "REQUEST_AUTHZ"

    uap = json.loads((REPO_ROOT / "manifests" / "iap-uap-policy.json").read_text(encoding="utf-8"))
    rules = uap["details"]["rules"]
    assert len(rules) == 1
    rule = rules[0]
    assert rule["effect"] == "ALLOW"
    assert "iap.googleapis.com/resources.egressViaIAP" in rule["permissions"]
    expr = rule["condition"]["expression"]
    assert "destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true" in expr


def test_four_mandatory_org_policies() -> None:
    """Verify the 4 mandatory Organization Policies required for Custom MCP + Agent Gateway in VPC-SC."""
    org_dir = REPO_ROOT / "manifests" / "org-policies"
    mcp_disable = yaml.safe_load((org_dir / "disable-custom-mcp-connector.yaml").read_text(encoding="utf-8"))
    assert mcp_disable["spec"]["rules"][0]["enforce"] is False

    sources = yaml.safe_load((org_dir / "allowed-data-sources.yaml").read_text(encoding="utf-8"))
    assert "custom_mcp" in sources["spec"]["rules"][0]["values"]["allowedValues"]

    fqdns = yaml.safe_load((org_dir / "allowed-egress-fqdns.yaml").read_text(encoding="utf-8"))
    allowed_fqdns = fqdns["spec"]["rules"][0]["values"]["allowedValues"]
    assert "*.internal.corp.example.com" in allowed_fqdns

    iam_disable = yaml.safe_load((org_dir / "disable-access-policy-bindings.yaml").read_text(encoding="utf-8"))
    assert iam_disable["spec"]["rules"][0]["enforce"] is False


def test_mcp_toolspec_and_a2a_agent_card() -> None:
    """Verify MCP v2025-03-26 toolspec.json and A2A v0.3+ agent-card.json."""
    toolspec = json.loads((REPO_ROOT / "manifests" / "specs" / "toolspec.json").read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in toolspec["tools"]}
    assert tools["query_financial_metrics"]["annotations"]["readOnlyHint"] is True
    assert tools["delete_ledger_record"]["annotations"]["readOnlyHint"] is False

    card = json.loads((REPO_ROOT / "manifests" / "specs" / "agent-card.json").read_text(encoding="utf-8"))
    assert card["protocolVersion"].startswith("0.3")
    assert card["url"].startswith("https://")
    assert len(card["skills"]) >= 1


def test_shell_scripts_syntax_and_dry_run() -> None:
    """Verify shell scripts pass bash -n syntax validation and DRY_RUN execution."""
    for script_name in ("deploy_gcp_infrastructure.sh", "publish_private_github_repo.sh"):
        script_path = REPO_ROOT / "scripts" / script_name
        check = subprocess.run(["bash", "-n", str(script_path)], capture_output=True, text=True, check=False)
        assert check.returncode == 0, f"Syntax error in {script_name}: {check.stderr}"

    deploy_res = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "deploy_gcp_infrastructure.sh")],
        env={"DRY_RUN": "true", "PATH": "/usr/bin:/bin"},
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert deploy_res.returncode == 0
    assert "=== Provisioning Sequence Completed Successfully ===" in deploy_res.stdout
