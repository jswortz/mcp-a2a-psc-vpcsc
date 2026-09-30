"""A2A v0.3+ Domain Agent & GEAP Reasoning Engine Endpoint bound to 127.0.0.1."""

from __future__ import annotations

from typing import Any
from aiohttp import web

from mcp_a2a_psc_vpcsc.security import (
    SecurityValidationError,
    SlidingWindowRateLimiter,
    TokenManager,
    security_headers_middleware,
)


class RunningA2AAgentServer:
    """Live HTTP A2A v0.3+ Agent Server listening strictly on 127.0.0.1 (never 0.0.0.0)."""

    def __init__(self, token_manager: TokenManager) -> None:
        self._token_manager = token_manager
        self._rate_limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=60.0)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0

    async def _handle_agent_card(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "protocolVersion": "0.3.0",
                "name": "Internal Finance Analysis A2A Agent",
                "url": "https://a2a-finance.internal.corp.example.com/a2a",
                "version": "1.0.0",
            }
        )

    async def _handle_a2a_post(self, request: web.Request) -> web.Response:
        try:
            claims = self._token_manager.verify_bearer_header(request.headers.get("Authorization"))
            self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
        except SecurityValidationError as exc:
            return web.json_response({"error": str(exc)}, status=401)

        body: dict[str, Any] = await request.json()
        jsonrpc_id = body.get("id", 1)
        params: dict[str, Any] = body.get("params", {})
        task_id = str(params.get("id", "task-default"))
        user_message = str(params.get("message", {}).get("text", ""))

        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {
                    "taskId": task_id,
                    "status": "COMPLETED",
                    "artifacts": [
                        {
                            "name": "variance_report",
                            "summary": f"Processed private A2A request: {user_message[:60]}",
                        }
                    ],
                },
            }
        )

    async def start(self) -> None:
        app = web.Application(middlewares=[security_headers_middleware])
        app.router.add_get("/.well-known/agent-card.json", self._handle_agent_card)
        app.router.add_post("/a2a", self._handle_a2a_post)
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
