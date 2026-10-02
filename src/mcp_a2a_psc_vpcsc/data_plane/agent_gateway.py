"""Agent Gateway, VPC Service Controls Perimeter, IAP CEL Evaluator, and Model Armor."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
import json
from typing import Any
from urllib.parse import urlparse

from mcp_a2a_psc_vpcsc.control_plane.agent_registry import AgentRegistryStore
from mcp_a2a_psc_vpcsc.data_plane.internal_alb_and_psc import PSCNetworkAttachmentBridge


class VPCServiceControlsError(PermissionError):
    """Raised when VPC-SC perimeter ordering or Managed Organization Policies are violated."""


class IAPAuthorizationError(PermissionError):
    """Raised when IAM v3 / IAP v2 Unified Access Policy CEL evaluation denies a request."""


class ModelArmorBlockedError(PermissionError):
    """Raised when Vertex AI Model Armor INSPECT_AND_BLOCK detects prompt injection."""


@dataclass(frozen=True)
class OrgPolicyState:
    """State of the 4 mandatory Organization Policies required in VPC-SC."""

    disable_custom_mcp_connector: bool
    allowed_data_sources: set[str]
    allowed_egress_fqdns: set[str]
    disable_access_policy_bindings: bool


class VPCServiceControlsPerimeter:
    """Enforces VPC-SC perimeter ordering, auth constraints, and the 4 mandatory Org Policies."""

    REQUIRED_PERIMETER_SERVICES = {
        "discoveryengine.googleapis.com",
        "aiplatform.googleapis.com",
        "networkservices.googleapis.com",
        "agentregistry.googleapis.com",
    }

    def __init__(
        self,
        project_in_perimeter: bool,
        protected_services: set[str],
        org_policies: OrgPolicyState,
    ) -> None:
        self.project_in_perimeter = project_in_perimeter
        self.protected_services = protected_services
        self.org_policies = org_policies

    def is_fqdn_allowed(self, fqdn: str) -> bool:
        return any(fnmatch(fqdn, pat) for pat in self.org_policies.allowed_egress_fqdns)

    def validate_connector_creation(self, auth_mode: str, target_fqdn: str) -> None:
        """Enforce Step 6 VPC-SC perimeter ordering and Custom MCP technical requirements."""
        if not self.project_in_perimeter or not self.REQUIRED_PERIMETER_SERVICES.issubset(
            self.protected_services
        ):
            raise VPCServiceControlsError(
                "Project must be inside the VPC-SC perimeter BEFORE creating any Custom MCP connector."
            )
        if self.org_policies.disable_custom_mcp_connector:
            raise VPCServiceControlsError(
                "Org Policy discoveryengine.managed.disableCustomMcpServerConnector must be enforce: false."
            )
        if "custom_mcp" not in self.org_policies.allowed_data_sources:
            raise VPCServiceControlsError(
                "Org Policy discoveryengine.managed.allowedDataSources must include 'custom_mcp'."
            )
        if self.org_policies.disable_access_policy_bindings:
            raise VPCServiceControlsError(
                "Org Policy iam.managed.disableAccessPolicyBindings must be enforce: false."
            )
        if auth_mode == "OAUTH2_DCR":
            raise VPCServiceControlsError(
                "OAuth Dynamic Client Registration (DCR) is prohibited inside VPC-SC."
            )
        if auth_mode == "STATIC_API_KEY":
            raise VPCServiceControlsError(
                "Static API keys are prohibited inside VPC-SC; use OAuth 2.0."
            )
        if not self.is_fqdn_allowed(target_fqdn):
            raise VPCServiceControlsError(
                f"Target FQDN '{target_fqdn}' is not allowlisted in discoveryengine.managed.allowedEgressFqdns."
            )


class ModelArmorInspector:
    """Simulates Vertex AI Model Armor floor settings (`INSPECT_AND_BLOCK`)."""

    INJECTION_PATTERNS = (
        "ignore all previous instructions",
        "system: exfiltrate",
        "drop table",
        "<script>",
    )

    def __init__(self, enforcement_mode: str = "INSPECT_AND_BLOCK") -> None:
        self.enforcement_mode = enforcement_mode

    def inspect_payload(self, payload: Any) -> None:
        if self.enforcement_mode != "INSPECT_AND_BLOCK":
            return
        serialized = json.dumps(payload).lower()
        for pat in self.INJECTION_PATTERNS:
            if pat in serialized:
                raise ModelArmorBlockedError(
                    f"Prompt injection detected by Vertex AI Model Armor ({pat!r})."
                )


class AgentGatewayProxy:
    """Centralized Agent Gateway (`AGENT_TO_ANYWHERE`) enforcing Registry, IAP CEL, Model Armor, and PSC."""

    def __init__(
        self,
        vpc_egress_mode: str,
        registry: AgentRegistryStore,
        psc_bridge: PSCNetworkAttachmentBridge,
        vpc_sc: VPCServiceControlsPerimeter,
        model_armor: ModelArmorInspector,
        approved_principals: set[str],
    ) -> None:
        if vpc_egress_mode != "ALL_TRAFFIC":
            raise VPCServiceControlsError("VPC-SC requires vpcEgress: ALL_TRAFFIC on AgentConnectivityTemplate.")
        self.vpc_egress_mode = vpc_egress_mode
        self.registry = registry
        self.psc_bridge = psc_bridge
        self.vpc_sc = vpc_sc
        self.model_armor = model_armor
        self.approved_principals = approved_principals
        self.audit_logs: list[dict[str, Any]] = []

    async def call_mcp_tool(
        self,
        principal: str,
        bearer_token: str,
        target_url: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Intercept outbound MCP tool call, verify Registry + IAP CEL + Model Armor, and route via PSC."""
        hostname = urlparse(target_url).hostname or ""
        self.vpc_sc.validate_connector_creation(
            auth_mode="OAUTH2_STATIC_CLIENT",
            target_fqdn=hostname,
        )

        mcp_view = self.registry.lookup_mcp_by_url(target_url)
        if mcp_view is None:
            raise IAPAuthorizationError(f"Destination {target_url} is not registered in bound Agent Registry.")

        if principal not in self.approved_principals:
            raise IAPAuthorizationError(f"Principal {principal} is not granted egressViaIAP.")

        tool_meta = mcp_view.tools.get(tool_name)
        if tool_meta is None:
            raise IAPAuthorizationError(f"Tool {tool_name!r} is not declared in Agent Registry toolspec.")

        # Evaluate IAM v3 / IAP CEL condition:
        # destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true
        if not tool_meta.get("read_only_hint", False):
            raise IAPAuthorizationError(
                f"IAP CEL policy denied tool {tool_name!r}: requires "
                "destination.agent_registry.mcp_server.tool.annotations.read_only_hint == true"
            )

        self.model_armor.inspect_payload(arguments)

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-Agent-Identity": principal,
        }
        json_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        response = await self.psc_bridge.forward_https_request(target_url, headers, json_payload)
        self.model_armor.inspect_payload(response)

        self.audit_logs.append(
            {
                "permission": "iap.googleapis.com/resources.egressViaIAP",
                "principal": principal,
                "destination": mcp_view.name,
                "tool": tool_name,
                "decision": "ALLOW",
            }
        )
        return response

    async def delegate_a2a_task(
        self,
        principal: str,
        bearer_token: str,
        target_url: str,
        task_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Intercept outbound A2A delegation, verify Registry + IAP CEL + Model Armor, and route via PSC."""
        hostname = urlparse(target_url).hostname or ""
        self.vpc_sc.validate_connector_creation(
            auth_mode="OAUTH2_STATIC_CLIENT",
            target_fqdn=hostname,
        )

        agent_view = self.registry.lookup_agent_by_url(target_url)
        if agent_view is None:
            raise IAPAuthorizationError(f"A2A destination {target_url} is not registered in Agent Registry.")

        if principal not in self.approved_principals:
            raise IAPAuthorizationError(f"Principal {principal} is not granted egressViaIAP.")

        self.model_armor.inspect_payload({"task_id": task_id, "message": user_message})

        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-Agent-Identity": principal,
        }
        json_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"id": task_id, "message": {"text": user_message}},
        }
        response = await self.psc_bridge.forward_https_request(target_url, headers, json_payload)
        self.model_armor.inspect_payload(response)

        self.audit_logs.append(
            {
                "permission": "iap.googleapis.com/resources.egressViaIAP",
                "principal": principal,
                "destination": agent_view.name,
                "decision": "ALLOW",
            }
        )
        return response
