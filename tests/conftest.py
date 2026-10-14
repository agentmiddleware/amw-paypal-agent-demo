"""Fixtures: the mini gateway in process, PayPal and OpenAI faked.

Nothing here opens a network connection: the gateway runs over httpx.ASGITransport,
PayPal over httpx.MockTransport, and the LLM is ScriptedLLM.
"""

from __future__ import annotations

import socket
from decimal import Decimal

import pytest
import pytest_asyncio

from fakes import FakePayPal
from gateway import Gateway
from paypal_client import PayPalClient
from paypal_tool import TOOL_NAME, register_checkout_tool

PRICE = Decimal("25.00")


@pytest.fixture(autouse=True, scope="session")
def no_network():
    """Fail any test that tries to open a network connection."""
    real_connect = socket.socket.connect

    def guarded(sock, address):
        if sock.family == socket.AF_UNIX:
            return real_connect(sock, address)
        raise RuntimeError(f"tests must run offline; blocked connect to {address!r}")

    socket.socket.connect = guarded
    yield
    socket.socket.connect = real_connect


@pytest.fixture(autouse=True)
def paypal_env(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "test-client-secret-do-not-log")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("PAYPAL_SANDBOX_CARD_NUMBER", raising=False)
    monkeypatch.delenv("PAYPAL_BASE_URL", raising=False)


@pytest.fixture(scope="session")
def gateway(tmp_path_factory) -> Gateway:
    return Gateway(tmp_path_factory.mktemp("amw") / "amw.db")


@pytest.fixture
def fake_paypal() -> FakePayPal:
    return FakePayPal()


@pytest_asyncio.fixture
async def paypal(fake_paypal):
    client = PayPalClient(base_url="https://paypal.invalid", transport=fake_paypal.transport())
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def live_gateway(gateway, paypal):
    """Gateway started on this test's event loop with paypal_checkout registered."""
    await gateway.start()
    register_checkout_tool(gateway.registry, paypal, price_usd=PRICE)
    yield gateway
    gateway.registry.unregister_local(TOOL_NAME)
    await gateway.stop()
