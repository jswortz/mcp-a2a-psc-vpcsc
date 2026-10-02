"""Comprehensive test suite for end-to-end idempotency and notebook teaching links."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import pytest

from mcp_a2a_psc_vpcsc.config import ArchitectureConfig
from mcp_a2a_psc_vpcsc.control_plane.agent_registry import AgentRegistryStore
from mcp_a2a_psc_vpcsc.data_plane.a2a_agent import RunningA2AAgentServer
from mcp_a2a_psc_vpcsc.data_plane.agent_gateway import (
    AgentGatewayProxy,
    ModelArmorInspector,
    OrgPolicyState,
    VPCServiceControlsPerimeter,
)
from mcp_a2a_psc_vpcsc.data_plane.internal_alb_and_psc import (
    BackendNEG,
    InternalCrossRegionALB,
    NEGType,
    PSCNetworkAttachmentBridge,
    PrivateCloudDNS,
)
from mcp_a2a_psc_vpcsc.data_plane.mcp_server import RunningMCPServer
from mcp_a2a_psc_vpcsc.security import TokenManager

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_agent_registry_create_service_idempotency() -> None:
    """Verify repeated create_service calls are idempotent and never create duplicates."""
    config = ArchitectureConfig()
    registry = AgentRegistryStore(project_id=config.project_id, location=config.region)
    toolspec_path = REPO_ROOT / "manifests" / "specs" / "toolspec.json"
    agent_card_path = REPO_ROOT / "manifests" / "specs" / "agent-card.json"

    for _ in range(3):
        registry.create_service(
            service_id="internal-data-mcp",
            display_name="Internal Data MCP",
            endpoint_url="https://mcp-data.internal.corp.example.com/mcp",
            protocol_binding="jsonrpc",
            mcp_toolspec_path=toolspec_path,
        )
        registry.create_service(
            service_id="onprem-erp-mcp",
            display_name="On-Prem ERP MCP",
            endpoint_url="https://mcp-onprem.internal.corp.example.com/mcp",
            protocol_binding="jsonrpc",
            mcp_toolspec_path=toolspec_path,
        )
        registry.create_service(
            service_id="internal-finance-a2a",
            display_name="Internal Finance A2A Agent",
            endpoint_url="https://a2a-finance.internal.corp.example.com/a2a",
            protocol_binding="HTTP+JSON",
            a2a_card_path=agent_card_path,
        )

    mcp_servers = registry.list_mcp_servers()
    agents = registry.list_agents()
    assert len(mcp_servers) == 2
    assert set(mcp_servers.keys()) == {"internal-data-mcp", "onprem-erp-mcp"}
    assert len(agents) == 1
    assert set(agents.keys()) == {"internal-finance-a2a"}


@pytest.mark.asyncio
async def test_server_start_stop_lifecycle_idempotency() -> None:
    """Verify RunningMCPServer and RunningA2AAgentServer handle repeated start() and stop() cleanly."""
    config = ArchitectureConfig()
    tm = TokenManager(secret_key=config.jwt_secret)
    mcp_srv = RunningMCPServer(token_manager=tm, server_label="idempotent-mcp")
    a2a_srv = RunningA2AAgentServer(token_manager=tm)

    # Call start() 3 times in a row without explicit stop() in between
    for _ in range(3):
        await mcp_srv.start()
        await a2a_srv.start()
        assert mcp_srv.port > 0
        assert a2a_srv.port > 0

    # Call stop() 3 times in a row
    for _ in range(3):
        await mcp_srv.stop()
        await a2a_srv.stop()
        assert mcp_srv.port == 0
        assert a2a_srv.port == 0


@pytest.mark.asyncio
async def test_data_plane_invocation_idempotency() -> None:
    """Verify 3 consecutive runs of In-VPC MCP, Outside-VPC Hybrid NEG MCP, and A2A return identical results."""
    config = ArchitectureConfig()
    tm = TokenManager(secret_key=config.jwt_secret)
    registry = AgentRegistryStore(project_id=config.project_id, location=config.region)
    toolspec_path = REPO_ROOT / "manifests" / "specs" / "toolspec.json"
    agent_card_path = REPO_ROOT / "manifests" / "specs" / "agent-card.json"

    registry.create_service(
        service_id="internal-data-mcp",
        display_name="Internal Data MCP",
        endpoint_url="https://mcp-data.internal.corp.example.com/mcp",
        protocol_binding="jsonrpc",
        mcp_toolspec_path=toolspec_path,
    )
    registry.create_service(
        service_id="onprem-erp-mcp",
        display_name="On-Prem ERP MCP",
        endpoint_url="https://mcp-onprem.internal.corp.example.com/mcp",
        protocol_binding="jsonrpc",
        mcp_toolspec_path=toolspec_path,
    )
    registry.create_service(
        service_id="internal-finance-a2a",
        display_name="Internal Finance A2A Agent",
        endpoint_url="https://a2a-finance.internal.corp.example.com/a2a",
        protocol_binding="HTTP+JSON",
        a2a_card_path=agent_card_path,
    )

    mcp_in_vpc = RunningMCPServer(token_manager=tm, server_label="in-vpc-cloud-run-mcp")
    mcp_on_prem = RunningMCPServer(token_manager=tm, server_label="outside-vpc-onprem-mcp")
    a2a_srv = RunningA2AAgentServer(token_manager=tm)

    await mcp_in_vpc.start()
    await mcp_on_prem.start()
    await a2a_srv.start()

    try:
        dns = PrivateCloudDNS("internal.corp.example.com.", network_management_sa_has_dns_peer_role=True)
        dns.add_a_record("mcp-data.internal.corp.example.com", config.internal_alb_vip)
        dns.add_a_record("mcp-onprem.internal.corp.example.com", config.internal_alb_vip)
        dns.add_a_record("a2a-finance.internal.corp.example.com", config.internal_alb_vip)

        alb = InternalCrossRegionALB(vip=config.internal_alb_vip)
        alb.register_host_route(
            "mcp-data.internal.corp.example.com",
            BackendNEG("cloud-run-serverless-neg", NEGType.IN_VPC_SERVERLESS_NEG, "127.0.0.1", mcp_in_vpc.port),
        )
        alb.register_host_route(
            "mcp-onprem.internal.corp.example.com",
            BackendNEG("onprem-interconnect-hybrid-neg", NEGType.OUTSIDE_VPC_HYBRID_NEG, "127.0.0.1", mcp_on_prem.port),
        )
        alb.register_host_route(
            "a2a-finance.internal.corp.example.com",
            BackendNEG("gke-a2a-standalone-neg", NEGType.IN_VPC_STANDALONE_NEG, "127.0.0.1", a2a_srv.port),
        )

        psc_bridge = PSCNetworkAttachmentBridge(
            psc_nat_cidr=config.psc_subnet_cidr,
            connection_preference="ACCEPT_AUTOMATIC",
            firewall_allows_psc_to_alb_443=True,
            private_dns=dns,
            internal_alb=alb,
        )
        vpc_sc = VPCServiceControlsPerimeter(
            project_in_perimeter=True,
            protected_services={
                "discoveryengine.googleapis.com",
                "aiplatform.googleapis.com",
                "networkservices.googleapis.com",
                "agentregistry.googleapis.com",
            },
            org_policies=OrgPolicyState(
                disable_custom_mcp_connector=False,
                allowed_data_sources={"custom_mcp"},
                allowed_egress_fqdns={"*.internal.corp.example.com"},
                disable_access_policy_bindings=False,
            ),
        )
        gateway = AgentGatewayProxy(
            vpc_egress_mode="ALL_TRAFFIC",
            registry=registry,
            psc_bridge=psc_bridge,
            vpc_sc=vpc_sc,
            model_armor=ModelArmorInspector(enforcement_mode="INSPECT_AND_BLOCK"),
            approved_principals={config.sample_agent_spiffe_id},
        )

        mcp_token = tm.issue_token(subject=config.sample_agent_spiffe_id, scope="mcp:tools")
        a2a_token = tm.issue_token(subject=config.sample_agent_spiffe_id, scope="a2a:invoke")

        in_vpc_results = [
            await gateway.call_mcp_tool(
                principal=config.sample_agent_spiffe_id,
                bearer_token=mcp_token,
                target_url="https://mcp-data.internal.corp.example.com/mcp",
                tool_name="query_financial_metrics",
                arguments={"ticker": "GOOG", "quarter": "Q3-2026"},
            )
            for _ in range(3)
        ]
        on_prem_results = [
            await gateway.call_mcp_tool(
                principal=config.sample_agent_spiffe_id,
                bearer_token=mcp_token,
                target_url="https://mcp-onprem.internal.corp.example.com/mcp",
                tool_name="query_financial_metrics",
                arguments={"ticker": "GOOG", "quarter": "Q3-2026"},
            )
            for _ in range(3)
        ]
        a2a_results = [
            await gateway.delegate_a2a_task(
                principal=config.sample_agent_spiffe_id,
                bearer_token=a2a_token,
                target_url="https://a2a-finance.internal.corp.example.com/a2a",
                task_id="task-idemp-001",
                user_message="Analyze Q3-2026 GOOG revenue variance",
            )
            for _ in range(3)
        ]

        assert in_vpc_results[0] == in_vpc_results[1] == in_vpc_results[2]
        assert on_prem_results[0] == on_prem_results[1] == on_prem_results[2]
        assert a2a_results[0] == a2a_results[1] == a2a_results[2]
    finally:
        await mcp_in_vpc.stop()
        await mcp_on_prem.stop()
        await a2a_srv.stop()


def test_deploy_script_dry_run_idempotency() -> None:
    """Verify repeated dry-run executions of infrastructure scripts produce identical output."""
    env = {**os.environ, "DRY_RUN": "true", "PATH": "/usr/bin:/bin"}
    for script_name in ("deploy_gcp_infrastructure.sh", "run_live_gcp_e2e_proof.sh"):
        script_path = REPO_ROOT / "scripts" / script_name
        run1 = subprocess.run(["bash", str(script_path)], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=True)
        run2 = subprocess.run(["bash", str(script_path)], cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, check=True)
        assert run1.stdout == run2.stdout
        assert run1.stderr == run2.stderr


def test_notebook_structure_and_teaching_links() -> None:
    """Verify the tutorial notebook has all 8 lessons, rich external docs links, and valid relative file links."""
    nb_path = REPO_ROOT / "notebooks" / "agent_gateway_mcp_a2a_psc_vpcsc_tutorial.ipynb"
    nb_data = json.loads(nb_path.read_text(encoding="utf-8"))
    markdown_cells = [
        "".join(c.get("source", [])) if isinstance(c.get("source"), list) else str(c.get("source", ""))
        for c in nb_data.get("cells", [])
        if c.get("cell_type") == "markdown"
    ]
    full_markdown = "\n\n".join(markdown_cells)

    # 1. Verify all 8 lessons are present
    for lesson_num in range(1, 9):
        assert f"## Lesson {lesson_num}:" in full_markdown, f"Missing Lesson {lesson_num} in tutorial notebook"

    # 2. Extract all markdown links [text](target)
    links = re.findall(r"\[([^\]]*)\]\(([^)]+)\)", full_markdown)
    external_links = [target for _, target in links if target.startswith("https://")]
    relative_links = [target for _, target in links if not target.startswith(("http://", "https://", "#"))]

    assert len(external_links) >= 15, f"Expected >= 15 external teaching links, found {len(external_links)}"
    assert len(relative_links) >= 10, f"Expected >= 10 relative repository links, found {len(relative_links)}"

    # 3. Verify every relative link resolves to an existing file on disk
    for rel_target in relative_links:
        clean_target = rel_target.split("#", 1)[0]
        resolved_path = (nb_path.parent / clean_target).resolve()
        assert resolved_path.is_file(), f"Broken relative link in notebook: '{rel_target}' -> {resolved_path}"

    # 4. Verify every code cell has pre-executed outputs saved in the notebook
    code_cells = [c for c in nb_data.get("cells", []) if c.get("cell_type") == "code"]
    assert len(code_cells) == 9
    for idx, c in enumerate(code_cells):
        assert len(c.get("outputs", [])) >= 1, f"Code cell #{idx + 1} is missing executed outputs"


def test_executive_slides_and_presentation_links() -> None:
    """Verify all 6 high-resolution executive slides and Google Slides deck links are present."""
    slides_dir = REPO_ROOT / "notebooks" / "assets" / "slides"
    expected_slides = [
        "slide-1-executive-overview.png",
        "slide-2-reference-architecture.png",
        "slide-3-private-egress-foundation.png",
        "slide-4-connecting-mcp-and-a2a.png",
        "slide-5-vpc-sc-and-zero-trust.png",
        "slide-6-live-e2e-and-idempotency.png",
    ]
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    nb_text = (REPO_ROOT / "notebooks" / "agent_gateway_mcp_a2a_psc_vpcsc_tutorial.ipynb").read_text(encoding="utf-8")

    gslides_url = "https://docs.google.com/presentation/d/1zETenOHeK_W2swO5CU2c0gRVI2zy6A6UnCx7kTBfqqs/edit"
    assert gslides_url in readme_text
    assert gslides_url in nb_text

    for slide_name in expected_slides:
        slide_path = slides_dir / slide_name
        assert slide_path.is_file(), f"Missing slide asset: {slide_path}"
        raw = slide_path.read_bytes()
        assert raw.startswith(b"\x89PNG\r\n\x1a\n"), f"Invalid PNG header in {slide_name}"
        assert len(raw) > 200_000, f"Expected high-resolution slide (>200KB), got {len(raw)} bytes for {slide_name}"
        assert slide_name in readme_text, f"Missing link to {slide_name} in README.md"
        assert slide_name in nb_text, f"Missing link to {slide_name} in tutorial notebook"

