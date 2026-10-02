"""Cloud Run entrypoint serving MCP (/mcp) and A2A (/.well-known/agent-card.json, /a2a)."""

from __future__ import annotations

import asyncio
import os
from aiohttp import web

from mcp_a2a_psc_vpcsc.config import ArchitectureConfig
from mcp_a2a_psc_vpcsc.data_plane.a2a_agent import RunningA2AAgentServer
from mcp_a2a_psc_vpcsc.data_plane.mcp_server import RunningMCPServer
from mcp_a2a_psc_vpcsc.security import TokenManager, security_headers_middleware


async def create_cloud_run_app() -> web.Application:
    """Create unified Cloud Run application serving /mcp, /a2a, and /healthz."""
    cfg = ArchitectureConfig()
    token_mgr = TokenManager(secret_key=cfg.jwt_secret)
    service_url = os.getenv(
        "PUBLIC_SERVICE_URL",
        f"https://internal-data-mcp-{cfg.project_number}.{cfg.region}.run.app",
    )
    mcp = RunningMCPServer(
        token_manager=token_mgr,
        server_label="cloud-run-internal-mcp",
        allow_gateway_proxied_requests=True,
    )
    a2a = RunningA2AAgentServer(
        token_manager=token_mgr,
        allow_gateway_proxied_requests=True,
        endpoint_url=f"{service_url}/a2a",
    )

    app = web.Application(middlewares=[security_headers_middleware])
    app.router.add_get("/healthz", lambda _req: web.json_response({"status": "ok"}))
    app.router.add_post("/mcp", mcp._handle_mcp_post)
    app.router.add_post("/oauth/register", mcp._handle_dcr_rejected)
    app.router.add_get("/.well-known/agent-card.json", a2a._handle_agent_card)
    app.router.add_post("/a2a", a2a._handle_a2a_post)
    return app


def main() -> None:
    # Bind to 127.0.0.1 by default; Cloud Run sets K_SERVICE and requires $PORT binding inside container
    host = "127.0.0.1" if not os.getenv("K_SERVICE") else os.getenv("CLOUD_RUN_BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    web.run_app(create_cloud_run_app(), host=host, port=port)


if __name__ == "__main__":
    main()
