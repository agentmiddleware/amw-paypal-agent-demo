"""Runs the pinned AMW gateway in this process, the way AMW's own demos do.

AMW's server package (``app``) is not pip-installable at the pinned commit, so
it is loaded from a source checkout: ``AMW_SOURCE_DIR``, defaulting to
``.amw/agent-middleware-api`` (created by scripts/fetch_amw.sh).

The admin key and the receipt signing key are generated fresh for each run and
held only in this process.
"""

from __future__ import annotations

import base64
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
AMW_SOURCE_DIR = Path(os.environ.get("AMW_SOURCE_DIR", ROOT / ".amw" / "agent-middleware-api"))


@dataclass
class AgentCredentials:
    wallet_id: str
    api_key: str = field(repr=False)


class Gateway:
    """One in-process AMW app on a SQLite file. Create at most one per process."""

    def __init__(self, db_path: Path) -> None:
        if not (AMW_SOURCE_DIR / "app" / "main.py").exists():
            raise RuntimeError(
                f"AMW gateway source not found at {AMW_SOURCE_DIR}. "
                "Run scripts/fetch_amw.sh or set AMW_SOURCE_DIR."
            )
        self.admin_key = secrets.token_urlsafe(32)
        # Settings are read when app.main is imported, so set them first.
        os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
        os.environ["VALID_API_KEYS"] = self.admin_key
        os.environ["TRUST_MODE_ENABLED"] = "true"
        os.environ["ALLOW_LEGACY_UNPERMITTED_MCP"] = "false"
        os.environ["ENABLE_PROOF_SURFACES"] = "false"
        # Prices are in cents here, so AMW's default hourly alert (1000) fires
        # on a single 25 USD checkout.
        os.environ.setdefault("VELOCITY_HOURLY_LIMIT", "100000")
        os.environ["TRUST_SIGNING_KEY_ID"] = "demo-ephemeral-ed25519"
        os.environ["TRUST_SIGNING_PRIVATE_KEY_B64"] = base64.b64encode(
            secrets.token_bytes(32)
        ).decode()
        if str(AMW_SOURCE_DIR) not in sys.path:
            sys.path.insert(0, str(AMW_SOURCE_DIR))

        from app.main import app
        from app.services.service_registry import get_service_registry

        self.app = app
        self.registry = get_service_registry()

    async def start(self) -> None:
        from app.db.database import init_db

        await init_db()

    async def stop(self) -> None:
        from app.db.database import close_db

        await close_db()

    def transport(self) -> httpx.AsyncBaseTransport:
        return httpx.ASGITransport(app=self.app)

    def http(self, api_key: str | None = None) -> httpx.AsyncClient:
        headers = {"X-API-Key": api_key} if api_key else {}
        return httpx.AsyncClient(
            transport=self.transport(), base_url="http://amw", headers=headers
        )

    async def provision_agent(self, agent_id: str, budget_credits: int = 10_000) -> AgentCredentials:
        """Sponsor wallet, agent wallet and a wallet-scoped key, via AMW's routes."""
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
        raise RuntimeError(f"AMW POST {path} returned {response.status_code}")
    return response.json()
