"""Agent Registry Control-Plane Catalog API (agentregistry.googleapis.com).

Demonstrates how Agent Registry fits into the architecture (Comment 1):
- You create writable `Service` resources (`gcloud agent-registry services create`).
- Agent Registry automatically populates the read-only `mcp-servers` and `agents` views.
- Agent Gateway binds to Agent Registry (`registryBindings`) to verify approved destinations
  and evaluate IAM v3 / IAP CEL rules against MCP tool annotations (`read_only_hint`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


class RegionalAlignmentError(ValueError):
    """Raised when Gemini Enterprise App location and Agent Registry/Gateway region mismatch."""


REQUIRED_REGIONAL_MAPPING: dict[str, str] = {
    "global": "us-central1",
    "us": "us-central1",
    "eu": "europe-west1",
}


@dataclass(frozen=True)
class ReadOnlyMcpServerView:
    """Read-only McpServer resource automatically generated from an Agent Registry Service."""

    name: str
    service_id: str
    display_name: str
    endpoint_url: str
    protocol_binding: str
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ReadOnlyAgentView:
    """Read-only Agent resource automatically generated from an Agent Registry Service."""

    name: str
    service_id: str
    display_name: str
    endpoint_url: str
    protocol_binding: str
    protocol_version: str
    skills: list[dict[str, Any]] = field(default_factory=list)


class AgentRegistryStore:
    """Control-plane catalog modeling `agentregistry.googleapis.com` inside the customer project."""

    def __init__(self, project_id: str, location: str) -> None:
        self.project_id = project_id
        self.location = location
        self._services: dict[str, dict[str, Any]] = {}
        self._mcp_servers_view: dict[str, ReadOnlyMcpServerView] = {}
        self._agents_view: dict[str, ReadOnlyAgentView] = {}

    def validate_regional_alignment(self, ge_app_location: str) -> None:
        """Enforce mandatory regional alignment between Gemini Enterprise App and Agent Registry."""
        expected_region = REQUIRED_REGIONAL_MAPPING.get(ge_app_location)
        if expected_region != self.location:
            raise RegionalAlignmentError(
                f"Regional mismatch: Gemini Enterprise App in '{ge_app_location}' requires "
                f"Agent Registry & Agent Gateway in '{expected_region}', got '{self.location}'."
            )

    def create_service(
        self,
        service_id: str,
        display_name: str,
        endpoint_url: str,
        protocol_binding: str,
        mcp_toolspec_path: Path | None = None,
        a2a_card_path: Path | None = None,
    ) -> dict[str, Any]:
        """Register an Agent Registry Service and auto-populate read-only mcp-servers or agents views."""
        if not endpoint_url.startswith("https://"):
            raise ValueError("VPC-SC requires HTTPS endpoints for all Agent Registry services.")

        full_service_name = (
            f"projects/{self.project_id}/locations/{self.location}/services/{service_id}"
        )
        service_record: dict[str, Any] = {
            "name": full_service_name,
            "serviceId": service_id,
            "displayName": display_name,
            "interfaces": [{"url": endpoint_url, "protocolBinding": protocol_binding}],
        }
        self._services[service_id] = service_record

        if mcp_toolspec_path is not None:
            raw_spec = json.loads(mcp_toolspec_path.read_text(encoding="utf-8"))
            parsed_tools: dict[str, dict[str, Any]] = {}
            for tool in raw_spec.get("tools", []):
                annotations = tool.get("annotations", {})
                parsed_tools[tool["name"]] = {
                    "description": tool.get("description", ""),
                    "read_only_hint": bool(annotations.get("readOnlyHint", False)),
                    "destructive_hint": bool(annotations.get("destructiveHint", False)),
                    "idempotent_hint": bool(annotations.get("idempotentHint", False)),
                    "open_world_hint": bool(annotations.get("openWorldHint", False)),
                }
            mcp_name = (
                f"projects/{self.project_id}/locations/{self.location}/mcpServers/{service_id}"
            )
            self._mcp_servers_view[service_id] = ReadOnlyMcpServerView(
                name=mcp_name,
                service_id=service_id,
                display_name=display_name,
                endpoint_url=endpoint_url,
                protocol_binding=protocol_binding,
                tools=parsed_tools,
            )

        if a2a_card_path is not None:
            raw_card = json.loads(a2a_card_path.read_text(encoding="utf-8"))
            agent_name = (
                f"projects/{self.project_id}/locations/{self.location}/agents/{service_id}"
            )
            self._agents_view[service_id] = ReadOnlyAgentView(
                name=agent_name,
                service_id=service_id,
                display_name=display_name,
                endpoint_url=endpoint_url,
                protocol_binding=protocol_binding,
                protocol_version=raw_card.get("protocolVersion", "0.3.0"),
                skills=raw_card.get("skills", []),
            )

        return service_record

    def list_mcp_servers(self) -> dict[str, ReadOnlyMcpServerView]:
        """Return the read-only mcp-servers catalog view (`gcloud agent-registry mcp-servers list`)."""
        return dict(self._mcp_servers_view)

    def list_agents(self) -> dict[str, ReadOnlyAgentView]:
        """Return the read-only agents catalog view (`gcloud agent-registry agents list`)."""
        return dict(self._agents_view)

    def lookup_mcp_by_url(self, target_url: str) -> ReadOnlyMcpServerView | None:
        for mcp in self._mcp_servers_view.values():
            if mcp.endpoint_url == target_url:
                return mcp
        return None

    def lookup_agent_by_url(self, target_url: str) -> ReadOnlyAgentView | None:
        for agent in self._agents_view.values():
            if agent.endpoint_url == target_url:
                return agent
        return None
