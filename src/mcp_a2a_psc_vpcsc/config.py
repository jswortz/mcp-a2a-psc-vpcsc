"""Configuration management with multi-tiered secret resolution (no hardcoded secrets)."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import secrets

logger = logging.getLogger(__name__)


def resolve_jwt_secret() -> str:
    """Resolve JWT signing secret using multi-tiered fallback (Env -> File -> CSPRNG).

    Strictly complies with mandatory-secure-web-skills: never hardcodes secrets
    or uses static literal fallbacks.
    """
    env_secret = os.getenv("MCP_OAUTH_JWT_SECRET", "").strip()
    if env_secret:
        return env_secret

    secret_file = Path("jwt_secret.txt")
    if secret_file.is_file():
        file_secret = secret_file.read_text(encoding="utf-8").strip()
        if file_secret:
            return file_secret

    logger.warning(
        "Generating ephemeral CSPRNG JWT secret for local/test execution. "
        "Instance-isolated; configure Secret Manager in production!"
    )
    return secrets.token_hex(32)


@dataclass(frozen=True)
class ArchitectureConfig:
    """Centralized configuration for the PSC + VPC-SC Reference Architecture."""

    project_id: str = field(default_factory=lambda: os.getenv("GCP_PROJECT_ID", "your-gcp-project-id"))
    project_number: str = field(default_factory=lambda: os.getenv("GCP_PROJECT_NUMBER", "123456789012"))
    org_id: str = field(default_factory=lambda: os.getenv("GCP_ORG_ID", "987654321098"))
    region: str = field(default_factory=lambda: os.getenv("GCP_REGION", "us-central1"))
    ge_app_location: str = field(default_factory=lambda: os.getenv("GE_APP_LOCATION", "global"))
    vpc_network_name: str = field(default_factory=lambda: os.getenv("VPC_NETWORK_NAME", "enterprise-vpc"))
    psc_subnet_cidr: str = field(default_factory=lambda: os.getenv("PSC_SUBNET_CIDR", "10.128.20.0/28"))
    internal_alb_vip: str = field(default_factory=lambda: os.getenv("INTERNAL_ALB_VIP", "10.128.10.50"))
    consumer_psc_endpoint_ip: str = field(
        default_factory=lambda: os.getenv("CONSUMER_PSC_ENDPOINT_IP", "10.128.10.99")
    )
    jwt_secret: str = field(default_factory=resolve_jwt_secret)

    @property
    def sample_agent_spiffe_id(self) -> str:
        """Return the cryptographic SPIFFE Agent Identity for the reasoning engine."""
        return (
            f"principal://agents.global.org-{self.org_id}.system.id.goog/"
            f"resources/aiplatform/projects/{self.project_number}/"
            f"locations/{self.region}/reasoningEngines/1234567890"
        )
