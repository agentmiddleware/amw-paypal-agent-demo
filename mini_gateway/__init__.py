"""A small MIT reference gateway written for this demo.

It serves the HTTP contract the vendored ``b2a_sdk`` client speaks (wallets,
API keys, permits, MCP ``tools/call``, signed receipts, the published key
set) so the demo runs with no private dependency. It is not the production
Agent Middleware core and makes no claim to match it beyond that contract.

Usage::

    gw = MiniGateway("demo.db", admin_key=secrets.token_urlsafe(32))
    gw.start()
    gw.registry.register_local(service_id="echo", name="Echo", description="...",
                               func=echo, credits_per_unit=1)
    httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw")
"""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .app import build_app
from .governance import Governor
from .signing import Signer
from .store import Store
from .tools import LocalTool, ToolRegistry

__all__ = ["LocalTool", "MiniGateway", "Signer", "ToolRegistry"]

DEFAULT_ISSUER = "amw-paypal-agent-demo/mini_gateway"


class MiniGateway:
    """One gateway: a SQLite file, a tool registry and one signing key.

    ``start()`` opens the database and turns any call a previous process left
    in flight into ``delivery_uncertain``. ``stop()`` closes it. The registry
    and signing key live as long as this object, across start and stop.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        admin_key: str,
        signing_key: Ed25519PrivateKey | None = None,
        key_id: str = "mini-gateway-ed25519",
        issuer: str = DEFAULT_ISSUER,
    ) -> None:
        if len(admin_key) < 16:
            raise ValueError("admin_key must be at least 16 characters")
        self.db_path = Path(db_path)
        self.admin_key = admin_key
        self.signer = Signer(key_id, signing_key, issuer=issuer)
        self.registry = ToolRegistry()
        self.store: Store | None = None
        self.governor: Governor | None = None
        self.app = build_app(self)

    def __repr__(self) -> str:
        return f"MiniGateway(db_path={str(self.db_path)!r}, key_id={self.signer.key_id!r})"

    def start(self) -> int:
        """Open storage. Returns how many stale in-flight calls were marked uncertain."""
        if self.store is None:
            self.store = Store(self.db_path)
            self.governor = Governor(self.store, self.registry, self.signer)
        return self.governor.recover()

    def stop(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
            self.governor = None
