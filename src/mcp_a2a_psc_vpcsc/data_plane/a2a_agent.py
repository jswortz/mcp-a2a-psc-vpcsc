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

    def __init__(
        self,
        token_manager: TokenManager,
        allow_gateway_proxied_requests: bool = False,
        endpoint_url: str = "https://a2a-finance.internal.corp.example.com/a2a",
    ) -> None:
        self._token_manager = token_manager
        self._allow_gateway_proxied = allow_gateway_proxied_requests
        self._endpoint_url = endpoint_url
        self._rate_limiter = SlidingWindowRateLimiter(max_requests=100, window_seconds=60.0)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int = 0

    async def _handle_agent_card(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "protocolVersion": "0.3.0",
                "name": "Internal Finance Analysis A2A Agent",
                "description": "Private domain A2A agent reachable over PSC and Agent Gateway",
                "url": self._endpoint_url,
                "version": "1.0.0",
                "capabilities": {"streaming": False, "pushNotifications": False},
                "defaultInputModes": ["text/plain", "application/json"],
                "defaultOutputModes": ["application/json"],
                "skills": [
                    {
                        "id": "quarterly-variance-analysis",
                        "name": "Quarterly Financial Variance Analysis",
                        "description": "Computes revenue and margin variance across private enterprise ledgers",
                        "tags": ["finance", "variance", "private-vpc"],
                    }
                ],
            }
        )

    async def _handle_a2a_post(self, request: web.Request) -> web.Response:
        if "X-API-Key" in request.headers:
            return web.json_response({"error": "Static API keys are prohibited inside VPC-SC"}, status=401)

        auth_header = request.headers.get("Authorization")
        if not self._allow_gateway_proxied:
            try:
                claims = self._token_manager.verify_bearer_header(auth_header)
                self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
            except SecurityValidationError as exc:
                return web.json_response({"error": str(exc)}, status=401)
        elif auth_header:
            import jwt as pyjwt

            raw_token = auth_header.removeprefix("Bearer ").strip()
            try:
                unverified_hdr = pyjwt.get_unverified_header(raw_token)
            except pyjwt.PyJWTError:
                return web.json_response({"error": "Invalid JWT header"}, status=401)
            alg = str(unverified_hdr.get("alg", ""))
            if alg.lower() == "none":
                return web.json_response({"error": "Insecure JWT algorithm: none"}, status=401)
            if alg == "HS256":
                try:
                    claims = self._token_manager.verify_bearer_header(auth_header)
                    self._rate_limiter.check_rate_limit(str(claims.get("sub", "unknown")))
                except SecurityValidationError as exc:
                    return web.json_response({"error": str(exc)}, status=401)

        body: dict[str, Any] = await request.json()
        jsonrpc_id = body.get("id", 1)
        params: dict[str, Any] = body.get("params") or {}
        task_id = str(params.get("id", "task-default"))
        msg_obj = params.get("message") or {}
        user_message = str(msg_obj.get("text", ""))
        if not user_message and isinstance(msg_obj.get("parts"), list) and msg_obj["parts"]:
            user_message = str(msg_obj["parts"][0].get("text", ""))

        summary_text = f"Processed private A2A request: {user_message[:60]}"
        return web.json_response(
            {
                "jsonrpc": "2.0",
                "id": jsonrpc_id,
                "result": {
                    "kind": "task",
                    "id": task_id,
                    "taskId": task_id,
                    "status": "COMPLETED",
                    "artifacts": [
                        {
                            "name": "variance_report",
                            "summary": summary_text,
                            "parts": [{"kind": "text", "text": summary_text}],
                        }
                    ],
                },
            }
        )

    async def start(self) -> None:
        if self._runner is not None:
            await self.stop()
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
            self._runner = None
            self._site = None
        self.port = 0

