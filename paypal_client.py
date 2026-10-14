"""Minimal PayPal Orders v2 client for the sandbox.

Credentials come only from the environment (PAYPAL_CLIENT_ID and
PAYPAL_CLIENT_SECRET). They are never logged, never put in exception text and
never shown by repr().

Every write carries a PayPal-Request-Id derived from the AMW idempotency key,
so a retry of the same governed action reaches PayPal with the same request
id and PayPal returns the original result instead of acting again.
"""

from __future__ import annotations

import hashlib
import os
import time
from decimal import Decimal
from typing import Any

import httpx

SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """A PayPal call failed. Carries status and PayPal's error name only."""

    def __init__(self, message: str, *, status_code: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        # True when no response arrived: PayPal may or may not have acted.
        self.ambiguous = ambiguous


def paypal_request_id(amw_idempotency_key: str, operation: str) -> str:
    """PayPal-Request-Id for one PayPal write made under one AMW key.

    A SHA-256 hash of the AMW idempotency key. The operation name is mixed in
    because create-order and capture are different PayPal calls: reusing one
    request id for both could make PayPal answer the capture with the cached
    create-order response.
    """
    key = amw_idempotency_key.strip()
    if not key:
        raise ValueError("amw_idempotency_key must not be blank")
    return hashlib.sha256(f"{operation}:{key}".encode()).hexdigest()


class PayPalClient:
    """Sandbox OAuth, create order, capture order.

    ``transport`` exists so tests can route calls to an in-process fake.
    Network timeouts on a write are retried with the same PayPal-Request-Id.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
        max_attempts: int = 2,
    ) -> None:
        client_id = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
        client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set")
        if os.environ.get("PAYPAL_SANDBOX_CARD_NUMBER", "").strip() and not os.environ.get(
            "PAYPAL_SANDBOX_CARD_EXPIRY", ""
        ).strip():
            raise PayPalError("PAYPAL_SANDBOX_CARD_EXPIRY must be set with the card number")
        base_url = base_url or os.environ.get("PAYPAL_BASE_URL", SANDBOX_BASE_URL)
        host = httpx.URL(base_url).host
        # Credentials only ever go to PayPal over https (tests pass a transport).
        if transport is None and not (
            httpx.URL(base_url).scheme == "https" and (host == "paypal.com" or host.endswith(".paypal.com"))
        ):
            raise PayPalError("PAYPAL_BASE_URL must be an https paypal.com host")
        self._auth = (client_id, client_secret)
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._token: str | None = None
        self._token_expires_at = 0.0
        # Evidence for the demo: every capture id PayPal returned, and how
        # many capture HTTP requests this process sent.
        self.capture_ids: list[str] = []
        self.capture_requests = 0

    def __repr__(self) -> str:
        return f"PayPalClient(base_url={str(self._http.base_url)!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = await self._http.post(
            "/v1/oauth2/token",
            auth=self._auth,
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            raise PayPalError("PayPal OAuth failed", status_code=response.status_code)
        body = response.json()
        self._token = body["access_token"]
        # Refresh a minute early so a token never expires mid-request.
        self._token_expires_at = time.monotonic() + max(int(body.get("expires_in", 0)) - 60, 0)
        return self._token

    async def _post(self, path: str, *, request_id: str, json: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "PayPal-Request-Id": request_id,
            "Prefer": "return=representation",
        }
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._http.post(path, headers=headers, json=json)
                break
            except httpx.TransportError:
                # Same PayPal-Request-Id on the retry, so PayPal replays.
                if attempt == self._max_attempts:
                    raise PayPalError(f"PayPal unreachable on {path}", ambiguous=True) from None
        return self._check(path, response)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        token = await self._access_token()
        path = f"/v2/checkout/orders/{order_id}"
        response = await self._http.get(path, headers={"Authorization": f"Bearer {token}"})
        return self._check(path, response)

    @staticmethod
    def _check(path: str, response: httpx.Response) -> dict[str, Any]:
        if response.status_code not in (200, 201):
            name = ""
            try:
                name = response.json().get("name", "")
            except ValueError:
                pass
            raise PayPalError(
                f"PayPal {path} failed: {name or 'error'}", status_code=response.status_code
            )
        return response.json()

    async def create_order(
        self,
        *,
        amount: Decimal,
        currency: str,
        description: str,
        amw_idempotency_key: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "description": description[:127],
                    "amount": {"currency_code": currency, "value": f"{amount:.2f}"},
                }
            ],
        }
        # A server-side agent has no buyer to approve the order in a browser.
        # With a sandbox test card as the payment source PayPal can process
        # the order without that approval step. Not verified against the live
        # sandbox; see README.
        card_number = os.environ.get("PAYPAL_SANDBOX_CARD_NUMBER", "").strip()
        if card_number:
            body["payment_source"] = {
                "card": {
                    "number": card_number,
                    "expiry": os.environ.get("PAYPAL_SANDBOX_CARD_EXPIRY", "").strip(),
                }
            }
        order = await self._post(
            "/v2/checkout/orders",
            request_id=paypal_request_id(amw_idempotency_key, "create"),
            json=body,
        )
        capture_id = first_capture_id(order)
        if capture_id:  # PayPal completed the capture in the create call
            self.capture_ids.append(capture_id)
        return order

    async def capture_order(self, order_id: str, *, amw_idempotency_key: str) -> dict[str, Any]:
        self.capture_requests += 1
        result = await self._post(
            f"/v2/checkout/orders/{order_id}/capture",
            request_id=paypal_request_id(amw_idempotency_key, "capture"),
            json={},
        )
        capture_id = first_capture_id(result)
        if capture_id:
            self.capture_ids.append(capture_id)
        return result

    @property
    def distinct_captures(self) -> int:
        return len(set(self.capture_ids))


def first_capture_id(order: dict[str, Any]) -> str | None:
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None
