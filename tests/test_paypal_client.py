from __future__ import annotations

import hashlib
from decimal import Decimal

import pytest

from fakes import FakePayPal
from paypal_client import PayPalClient, PayPalError, paypal_request_id


def test_request_id_is_a_stable_hash_of_the_amw_key():
    key = "oai-call_abc123"
    assert paypal_request_id(key, "capture") == hashlib.sha256(b"capture:" + key.encode()).hexdigest()
    assert paypal_request_id(key, "capture") == paypal_request_id(key, "capture")
    assert paypal_request_id(key, "create") != paypal_request_id(key, "capture")
    assert paypal_request_id("oai-call_other", "capture") != paypal_request_id(key, "capture")


def test_blank_key_is_refused():
    with pytest.raises(ValueError):
        paypal_request_id("  ", "capture")


def test_missing_credentials_refused(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET")
    with pytest.raises(PayPalError):
        PayPalClient()


@pytest.mark.parametrize(
    "url",
    ["http://api-m.sandbox.paypal.com", "https://paypal.com.attacker.example", "https://example.com"],
)
def test_credentials_only_sent_to_https_paypal_hosts(monkeypatch, url):
    monkeypatch.setenv("PAYPAL_BASE_URL", url)
    with pytest.raises(PayPalError, match="https paypal.com"):
        PayPalClient()


def test_sandbox_default_host_accepted():
    assert "api-m.sandbox.paypal.com" in repr(PayPalClient())


def test_card_number_without_expiry_refused(monkeypatch):
    monkeypatch.setenv("PAYPAL_SANDBOX_CARD_NUMBER", "4111111111111111")
    monkeypatch.delenv("PAYPAL_SANDBOX_CARD_EXPIRY", raising=False)
    with pytest.raises(PayPalError, match="EXPIRY"):
        PayPalClient()


async def test_secret_not_exposed_in_repr_or_errors():
    fake = FakePayPal()
    client = PayPalClient(base_url="https://paypal.invalid", transport=fake.transport())
    assert "test-client-secret-do-not-log" not in repr(client)
    with pytest.raises(PayPalError) as excinfo:
        await client.capture_order("ORDER-missing", amw_idempotency_key="oai-call_x")
    assert "test-client-secret-do-not-log" not in str(excinfo.value)
    await client.aclose()


async def test_capture_timeout_retry_reuses_request_id_and_captures_once():
    fake = FakePayPal()
    client = PayPalClient(base_url="https://paypal.invalid", transport=fake.transport())
    key = "oai-call_timeout"
    order = await client.create_order(
        amount=Decimal("25.00"), currency="USD", description="stickers", amw_idempotency_key=key
    )
    fake.drop_capture_responses = 1

    captured = await client.capture_order(order["id"], amw_idempotency_key=key)

    assert captured["status"] == "COMPLETED"
    assert fake.captures == ["CAPTURE-0001"]
    assert client.distinct_captures == 1
    await client.aclose()
