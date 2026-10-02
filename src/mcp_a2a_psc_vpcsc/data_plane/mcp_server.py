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

    def __init__(
        self,
        token_manager: TokenManager,
        server_label: str,
        allow_gateway_proxied_requests: bool = False,
    ) -> None:
        self._token_manager = token_manager
        self._server_label = server_label
        self._allow_gateway_proxied = allow_gateway_proxied_requests
        self._rate_limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=60.0)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0

    def _authenticate_request(self, request: web.Request) -> tuple[str | None, web.Response | None]:
        """Validate OAuth Bearer JWT and SPIFFE identity; returns (principal, error_response)."""
        if "X-API-Key" in request.headers:
            return None, web.json_response(
                {"error": "Static API keys are prohibited inside VPC-SC; use OAuth 2.0 Bearer JWT."},
                status=401,
            )

        auth_header = request.headers.get("Authorization")
        agent_identity = request.headers.get("X-Agent-Identity", "")

        if not self._allow_gateway_proxied:
            try:
                claims = self._token_manager.verify_bearer_header(auth_header)
                self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
            except SecurityValidationError as exc:
                return None, web.json_response({"error": str(exc)}, status=401)

            if not agent_identity.startswith("principal://agents.global.org-"):
                return None, web.json_response(
                    {"error": "Missing or invalid SPIFFE X-Agent-Identity"},
                    status=403,
                )
            return agent_identity, None

        # Cloud Run behind Agent Gateway + IAP v2 + PSC Network Attachment:
        # Always reject forged alg=none tokens or invalid HS256 tokens if Authorization is supplied.
        if auth_header:
            if not auth_header.startswith("Bearer "):
                return None, web.json_response({"error": "Malformed Authorization header"}, status=401)
            raw_token = auth_header.removeprefix("Bearer ").strip()
            import jwt as pyjwt

            try:
                unverified_hdr = pyjwt.get_unverified_header(raw_token)
            except pyjwt.PyJWTError:
                return None, web.json_response({"error": "Invalid JWT header"}, status=401)
            alg = str(unverified_hdr.get("alg", ""))
            if alg.lower() == "none":
                return None, web.json_response({"error": "Insecure JWT algorithm: none"}, status=401)
            if alg == "HS256":
                try:
                    claims = self._token_manager.verify_bearer_header(auth_header)
                    self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
                except SecurityValidationError as exc:
                    return None, web.json_response({"error": str(exc)}, status=401)

        if agent_identity and not agent_identity.startswith("principal://agents.global.org-"):
            return None, web.json_response(
                {"error": "Missing or invalid SPIFFE X-Agent-Identity"},
                status=403,
            )

        principal = (
            agent_identity
            or "principal://agents.global.org-1060412978793.system.id.goog/resources/discoveryengine/projects/679926387543"
        )
        try:
            self._rate_limiter.check_rate_limit(principal)
        except SecurityValidationError as exc:
            return None, web.json_response({"error": str(exc)}, status=429)
        return principal, None

    async def _handle_mcp_post(self, request: web.Request) -> web.Response:
        _principal, err_resp = self._authenticate_request(request)
        if err_resp is not None:
            return err_resp

        body: dict[str, Any] = await request.json()
        jsonrpc_id = body.get("id")
        method = str(body.get("method", ""))
        params: dict[str, Any] = body.get("params") or {}

        # Handle MCP JSON-RPC notifications (e.g. notifications/initialized)
        if method.startswith("notifications/") or ("id" not in body and method):
            return web.Response(status=202)

        if method == "ping":
            return web.json_response({"jsonrpc": "2.0", "id": jsonrpc_id, "result": {}})

        if method == "initialize":
            requested_version = str(params.get("protocolVersion") or "2025-03-26")
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id if jsonrpc_id is not None else 1,
                    "result": {
                        "protocolVersion": requested_version,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": self._server_label, "version": "1.0.0"},
                    },
                }
            )

        if method == "tools/list":
            return web.json_response(
                {
                    "jsonrpc": "2.0",
                    "id": jsonrpc_id if jsonrpc_id is not None else 1,
                    "result": {
                        "tools": [
                            {
                                "name": "query_financial_metrics",
                                "description": "Query internal financial metrics by ticker and fiscal quarter",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "ticker": {
                                            "type": "string",
                                            "description": "Company ticker symbol (e.g., GOOG)",
                                        },
                                        "quarter": {
                                            "type": "string",
                                            "description": "Fiscal quarter (e.g., Q3-2026)",
                                        },
                                    },
                                    "required": ["ticker", "quarter"],
                                },
                                "annotations": {
                                    "readOnlyHint": True,
                                    "destructiveHint": False,
                                    "idempotentHint": True,
                                    "openWorldHint": False,
                                },
                            },
                            {
                                "name": "delete_ledger_record",
                                "description": "Delete an internal ledger entry (mutating operation blocked by IAP CEL policy)",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "record_id": {
                                            "type": "string",
                                            "description": "Ledger record identifier",
                                        }
                                    },
                                    "required": ["record_id"],
                                },
                                "annotations": {
                                    "readOnlyHint": False,
                                    "destructiveHint": True,
                                    "idempotentHint": False,
                                    "openWorldHint": False,
                                },
                            },
                        ]
                    },
                }
            )

        if method == "tools/call":
            import json

            tool_name = str(params.get("name", ""))
            args = params.get("arguments") or {}
            if tool_name == "query_financial_metrics":
                ticker = str(args.get("ticker", "")).strip().upper()
                quarter = str(args.get("quarter", "")).strip().upper().replace(" ", "-")
                if not SAFE_TICKER_RE.match(ticker) or not SAFE_QUARTER_RE.match(quarter):
                    return web.json_response(
                        {
                            "jsonrpc": "2.0",
                            "id": jsonrpc_id if jsonrpc_id is not None else 1,
                            "error": {"code": -32602, "message": "Invalid input"},
                        },
                        status=400,
                    )
                payload_data = {
                    "server_label": self._server_label,
                    "ticker": ticker,
                    "quarter": quarter,
                    "revenue_usd_millions": 96450,
                    "operating_margin_pct": 33.4,
                }
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": jsonrpc_id if jsonrpc_id is not None else 1,
                        "result": {
                            **payload_data,
                            "structuredContent": payload_data,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(payload_data),
                                }
                            ],
                            "isError": False,
                        },
                    }
                )

        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": jsonrpc_id if jsonrpc_id is not None else 1,
                "error": {"code": -32601, "message": "Method not found"},
            },
            status=404,
        )

    async def _handle_dcr_rejected(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"error": "OAuth Dynamic Client Registration (DCR) is disabled in VPC-SC"},
            status=403,
        )

    async def start(self) -> None:
        if self._runner is not None:
            await self.stop()
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
            self._runner = None
            self._site = None
        self.port = 0

