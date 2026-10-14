"""Runs the demo's MIT reference gateway (``mini_gateway``) in this process.

The admin key and the receipt signing key are generated fresh for each run and
held only in this process.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from mini_gateway import MiniGateway


@dataclass
class AgentCredentials:
    wallet_id: str
    api_key: str = field(repr=False)


class Gateway:
    """One in-process gateway on a SQLite file."""

    def __init__(self, db_path: Path) -> None:
        self.admin_key = secrets.token_urlsafe(32)
        self._gw = MiniGateway(db_path, admin_key=self.admin_key, key_id="demo-ephemeral-ed25519")
        self.app = self._gw.app
        self.registry = self._gw.registry

    async def start(self) -> None:
        self._gw.start()

    async def stop(self) -> None:
        self._gw.stop()

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.app)

    def http(self, api_key: str | None = None) -> httpx.AsyncClient:
        headers = {"X-API-Key": api_key} if api_key else {}
        return httpx.AsyncClient(
            transport=self.transport(), base_url="http://amw", headers=headers
        )

    async def provision_agent(self, agent_id: str, budget_credits: int = 10_000) -> AgentCredentials:
        """Sponsor wallet, agent wallet and a wallet scoped key, via the gateway's routes."""
        async with self.http(self.admin_key) as admin:
            sponsor = await _post(
                admin,
                "/v1/billing/wallets/sponsor",
                {
                    "sponsor_name": "PayPal demo sponsor",
                    "email": "sponsor@example.com",
                    "initial_credits": budget_credits * 10,
                    "require_kyc": False,
                },
            )
            agent = await _post(
                admin,
                "/v1/billing/wallets/agent",
                {
                    "sponsor_wallet_id": sponsor["wallet_id"],
                    "agent_id": agent_id,
                    "budget_credits": budget_credits,
                    "daily_limit": budget_credits,
                },
            )
            key = await _post(
                admin,
                "/v1/api-keys",
                {"wallet_id": agent["wallet_id"], "key_name": agent_id, "expires_in_days": 1},
            )
        return AgentCredentials(wallet_id=agent["wallet_id"], api_key=key["api_key"])


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    response = await client.post(path, json=body)
    if response.status_code != 201:
        # The path and status only: the body could echo request fields.
        raise RuntimeError(f"gateway POST {path} returned {response.status_code}")
    return response.json()
