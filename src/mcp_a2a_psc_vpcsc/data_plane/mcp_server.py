"""Custom BYO-MCP Server (MCP v2025-03-26 Streamable HTTP JSON-RPC 2.0) bound to 127.0.0.1."""

from __future__ import annotations

import re
from typing import Any
from aiohttp import web

from mcp_a2a_psc_vpcsc.security import (
    SecurityValidationError,
    SlidingWindowRateLimiter,
    TokenManager,
    security_headers_middleware,
)

SAFE_TICKER_RE = re.compile(r"^[A-Z]{1,8}$")
SAFE_QUARTER_RE = re.compile(r"^Q[1-4]-20[2-3][0-9]$")


class RunningMCPServer:
    """Live HTTP MCP Server listening strictly on 127.0.0.1 (never 0.0.0.0)."""

    def __init__(self, token_manager: TokenManager, server_label: str) -> None:
        self._token_manager = token_manager
        self._server_label = server_label
        self._rate_limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=60.0)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0

    async def _handle_mcp_post(self, request: web.Request) -> web.Response:
        # Reject static API keys inside VPC-SC
        if "X-API-Key" in request.headers:
            return web.json_response(
                {"error": "Static API keys are prohibited inside VPC-SC; use OAuth 2.0 Bearer JWT."},
                status=401,
            )

        try:
            claims = self._token_manager.verify_bearer_header(request.headers.get("Authorization"))
            self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
        except SecurityValidationError as exc:
            return web.json_response({"error": str(exc)}, status=401)

        agent_identity = request.headers.get("X-Agent-Identity", "")
        if not agent_identity.startswith("principal://agents.global.org-"):
            return web.json_response({"error": "Missing or invalid SPIFFE X-Agent-Identity"}, status=403)

        body: dict[str, Any] = await request.json()
        jsonrpc_id = body.get("id", 1)
        method = body.get("method", "")
        params: dict[str, Any] = body.get("params", {})

        if method == "initialize":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "serverInfo": {"name": self._server_label, "version": "1.0.0"},
                    },
                }
            )

        if method == "tools/call":
            tool_name = str(params.get("name", ""))
            args = params.get("arguments", {})
            if tool_name == "query_financial_metrics":
                ticker = str(args.get("ticker", ""))
                quarter = str(args.get("quarter", ""))
                if not SAFE_TICKER_RE.match(ticker) or not SAFE_QUARTER_RE.match(quarter):
                    return web.json_response(
                        {"jsonrpc": "2.0", "id": jsonrpc_id, "error": {"code": -32602, "message": "Invalid input"}},
                        status=400,
                    )
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": jsonrpc_id,
                        "result": {
                            "server_label": self._server_label,
                            "ticker": ticker,
                            "quarter": quarter,
                            "revenue_usd_millions": 96450,
                            "operating_margin_pct": 33.4,
                        },
                    }
                )

        return web.json_response(
            {"jsonrpc": "2.0", "id": jsonrpc_id, "error": {"code": -32601, "message": "Method not found"}},
            status=404,
        )

    async def _handle_dcr_rejected(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"error": "OAuth Dynamic Client Registration (DCR) is disabled in VPC-SC"},
            status=403,
        )

    async def start(self) -> None:
        app = web.Application(middlewares=[security_headers_middleware])
        app.router.add_post("/mcp", self._handle_mcp_post)
        app.router.add_post("/oauth/register", self._handle_dcr_rejected)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        # Bind strictly to 127.0.0.1 per mandatory-secure-web-skills
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()
        sockets = self._site._server.sockets  # type: ignore[union-attr]
        self.port = int(sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
