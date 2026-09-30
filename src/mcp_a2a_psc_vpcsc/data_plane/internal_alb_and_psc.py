"""Data Plane Zone 2 & Zone 3: Private Cloud DNS Peering, PSC Bridge, and Internal Cross-Region ALB."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse
import httpx


class NEGType(str, Enum):
    """Network Endpoint Group placement inside or outside the Consumer VPC (Comment 2)."""

    IN_VPC_SERVERLESS_NEG = "IN_VPC_SERVERLESS_NEG"
    IN_VPC_STANDALONE_NEG = "IN_VPC_STANDALONE_NEG"
    OUTSIDE_VPC_HYBRID_NEG = "OUTSIDE_VPC_HYBRID_NEG"
    OUTSIDE_VPC_SWP_NAT = "OUTSIDE_VPC_SWP_NAT"


@dataclass(frozen=True)
class BackendNEG:
    """Represents a backend Network Endpoint Group attached to the Internal Cross-Region ALB."""

    name: str
    neg_type: NEGType
    target_host: str
    target_port: int


class PrivateCloudDNS:
    """Simulates Split-Horizon Private Cloud DNS Zone with Cloud DNS Peering IAM check."""

    def __init__(self, zone_domain: str, network_management_sa_has_dns_peer_role: bool) -> None:
        self.zone_domain = zone_domain
        self.network_management_sa_has_dns_peer_role = network_management_sa_has_dns_peer_role
        self._a_records: dict[str, str] = {}

    def add_a_record(self, fqdn: str, ip_address: str) -> None:
        self._a_records[fqdn.rstrip(".")] = ip_address

    def resolve(self, fqdn: str) -> str:
        if not self.network_management_sa_has_dns_peer_role:
            raise PermissionError(
                "Cloud DNS Peering failed: service-PROJECT_NUMBER@gcp-sa-networkmanagement "
                "is missing roles/dns.peer."
            )
        normalized = fqdn.rstrip(".")
        if normalized not in self._a_records:
            raise LookupError(f"NXDOMAIN in Private Cloud DNS zone for '{normalized}'")
        return self._a_records[normalized]


class InternalCrossRegionALB:
    """Internal Cross-Region Application Load Balancer (`INTERNAL_MANAGED`) inside Consumer VPC."""

    def __init__(self, vip: str) -> None:
        self.vip = vip
        self._host_routes: dict[str, BackendNEG] = {}
        self._swp_nat_routes: dict[str, BackendNEG] = {}

    def register_host_route(self, hostname: str, neg: BackendNEG) -> None:
        self._host_routes[hostname.rstrip(".")] = neg

    def register_swp_nat_route(self, hostname: str, target_host: str, target_port: int) -> None:
        self._swp_nat_routes[hostname.rstrip(".")] = BackendNEG(
            name="vpc-swp-cloud-nat-egress",
            neg_type=NEGType.OUTSIDE_VPC_SWP_NAT,
            target_host=target_host,
            target_port=target_port,
        )

    def resolve_backend_neg(self, hostname: str) -> BackendNEG:
        normalized = hostname.rstrip(".")
        if normalized in self._host_routes:
            return self._host_routes[normalized]
        if normalized in self._swp_nat_routes:
            return self._swp_nat_routes[normalized]
        raise LookupError(f"No Internal ALB URL map host rule or SWP route for '{normalized}'")


class PSCNetworkAttachmentBridge:
    """Simulates the Private Service Connect Network Attachment (`/28` NAT subnet) & DNS Peering."""

    def __init__(
        self,
        psc_nat_cidr: str,
        connection_preference: str,
        firewall_allows_psc_to_alb_443: bool,
        private_dns: PrivateCloudDNS,
        internal_alb: InternalCrossRegionALB,
    ) -> None:
        self.psc_nat_cidr = psc_nat_cidr
        self.connection_preference = connection_preference
        self.firewall_allows_psc_to_alb_443 = firewall_allows_psc_to_alb_443
        self.private_dns = private_dns
        self.internal_alb = internal_alb

    async def forward_https_request(
        self,
        target_url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Inject packet from Google Tenant into Consumer VPC via PSC Network Attachment."""
        if not self.firewall_allows_psc_to_alb_443:
            raise ConnectionError(f"VPC firewall blocked ingress from PSC NAT subnet {self.psc_nat_cidr} to tcp:443")

        parsed = urlparse(target_url)
        hostname = parsed.hostname or ""
        path = parsed.path or "/"

        # If internal domain, resolve via Cloud DNS Peering -> Internal ALB VIP
        if hostname.endswith(".internal.corp.example.com"):
            resolved_ip = self.private_dns.resolve(hostname)
            if resolved_ip != self.internal_alb.vip:
                raise ConnectionError(f"Resolved IP {resolved_ip} does not match Internal ALB VIP {self.internal_alb.vip}")
        else:
            resolved_ip = self.internal_alb.vip

        neg = self.internal_alb.resolve_backend_neg(hostname)
        local_url = f"http://{neg.target_host}:{neg.target_port}{path}"

        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.post(local_url, headers=headers, json=json_payload)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()

        body["routing_metadata"] = {
            "entered_vpc_via_psc": True,
            "psc_nat_cidr": self.psc_nat_cidr,
            "resolved_vip": resolved_ip,
            "neg_name": neg.name,
            "neg_type": neg.neg_type.value,
        }
        return body


class ConsumerPSCEndpoint:
    """Scenario B (Inbound A2A): Consumer VPC ForwardingRule PSC Endpoint targeting GEAP Service Attachment."""

    def __init__(
        self,
        forwarding_rule_ip: str,
        target_service_attachment: str,
        private_dns: PrivateCloudDNS,
        target_host: str,
        target_port: int,
    ) -> None:
        self.forwarding_rule_ip = forwarding_rule_ip
        self.target_service_attachment = target_service_attachment
        self.private_dns = private_dns
        self.target_host = target_host
        self.target_port = target_port

    async def invoke_reasoning_engine_a2a(
        self,
        hostname: str,
        bearer_token: str,
        principal: str,
        task_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        """Resolve `.a2a.hyperlane.ucaip.goog` to Consumer PSC IP and invoke ReasoningEngine privately."""
        resolved_ip = self.private_dns.resolve(hostname)
        if resolved_ip != self.forwarding_rule_ip:
            raise ConnectionError("Private DNS for .hyperlane.ucaip.goog did not resolve to Consumer PSC Endpoint IP")

        local_url = f"http://{self.target_host}:{self.target_port}/a2a"
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "X-Agent-Identity": principal,
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"id": task_id, "message": {"text": user_message}},
        }
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.post(local_url, headers=headers, json=payload)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()

        data["psc_endpoint_ip"] = resolved_ip
        data["target_service_attachment"] = self.target_service_attachment
        return data
