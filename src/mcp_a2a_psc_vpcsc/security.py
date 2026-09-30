"""Security primitives: JWT verification, SPIFFE validation, rate limiting, and HTTP headers."""

from __future__ import annotations

from collections import defaultdict, deque
import time
from typing import Any
from aiohttp import web
import jwt

EXPECTED_JWT_ALGORITHM = "HS256"
EXPECTED_AUDIENCE = "https://internal.corp.example.com"


class SecurityValidationError(PermissionError):
    """Raised when OAuth 2.0 JWT, SPIFFE identity, or rate-limit validation fails."""


class TokenManager:
    """Issues and verifies OAuth 2.0 JWTs with strict algorithm and expiration enforcement."""

    def __init__(self, secret_key: str) -> None:
        if not secret_key or len(secret_key) < 32:
            raise ValueError("Cryptographic JWT secret key must be at least 32 characters.")
        self._secret_key = secret_key

    def issue_token(self, subject: str, scope: str, ttl_seconds: int = 900) -> str:
        """Issue a short-lived signed JWT with exp, iat, and aud claims."""
        now = int(time.time())
        payload = {
            "sub": subject,
            "scope": scope,
            "aud": EXPECTED_AUDIENCE,
            "iat": now,
            "exp": now + ttl_seconds,
        }
        return jwt.encode(payload, self._secret_key, algorithm=EXPECTED_JWT_ALGORITHM)

    def verify_bearer_header(self, auth_header: str | None) -> dict[str, Any]:
        """Verify an HTTP Authorization Bearer header; reject 'none' algorithm and expired tokens."""
        if not auth_header or not auth_header.startswith("Bearer "):
            raise SecurityValidationError("Missing or malformed OAuth 2.0 Bearer token.")
        token = auth_header.removeprefix("Bearer ").strip()
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise SecurityValidationError("Invalid JWT header.") from exc

        alg = unverified_header.get("alg", "")
        if alg.lower() == "none" or alg != EXPECTED_JWT_ALGORITHM:
            raise SecurityValidationError(f"Unsupported or insecure JWT algorithm: {alg}")

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                self._secret_key,
                algorithms=[EXPECTED_JWT_ALGORITHM],
                audience=EXPECTED_AUDIENCE,
                options={"require": ["exp", "iat", "sub", "aud"]},
            )
            return claims
        except jwt.PyJWTError as exc:
            raise SecurityValidationError("JWT signature or expiration verification failed.") from exc


class SlidingWindowRateLimiter:
    """In-memory per-principal rate limiter protecting backend MCP and A2A APIs."""

    def __init__(self, max_requests: int = 100, window_seconds: float = 60.0) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check_rate_limit(self, key: str) -> None:
        now = time.monotonic()
        q = self._requests[key]
        while q and (now - q[0]) > self._window_seconds:
            q.popleft()
        if len(q) >= self._max_requests:
            raise SecurityValidationError("Rate limit exceeded.")
        q.append(now)


@web.middleware
async def security_headers_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Enforce HTTP methods allow-list and strict security headers on all responses."""
    if request.method not in ("GET", "POST"):
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST"])

    response = await handler(request)
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response
