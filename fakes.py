"""Offline stand-ins used by the tests and by demo.py when credentials are absent.

* FakePayPal: an in-process PayPal Orders v2 sandbox that honors
  PayPal-Request-Id the way PayPal documents it (same id, same response, no
  second action) and counts real captures.
* ScriptedLLM: answers chat.completions.create with one paypal_checkout call.
* LostResponseTransport: lets the gateway finish a call, then drops the
  response and raises a timeout, which is the ambiguous retry case.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from typing import Any

import httpx

_CAPTURE_PATH = re.compile(r"^/v2/checkout/orders/([^/]+)/capture$")


class FakePayPal:
    def __init__(self) -> None:
        self.orders: dict[str, dict[str, Any]] = {}
        self.captures: list[str] = []  # one entry per capture actually executed
        self.writes = 0  # create and capture requests received, replays included
        self._replays: dict[str, tuple[int, dict[str, Any]]] = {}
        self.drop_capture_responses = 0  # lose this many capture responses
        self.fail_captures_with: str | None = None  # e.g. "ORDER_NOT_APPROVED"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            if not request.headers.get("Authorization", "").startswith("Basic "):
                return httpx.Response(401, json={"error": "invalid_client"})
            return httpx.Response(200, json={"access_token": "fake-token", "expires_in": 3600})
        if request.headers.get("Authorization") != "Bearer fake-token":
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        if request.method == "GET" and path.startswith("/v2/checkout/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=order) if order else httpx.Response(404, json={})

        self.writes += 1
        request_id = request.headers.get("PayPal-Request-Id", "")
        match = _CAPTURE_PATH.match(path)
        if request_id in self._replays:
            status, body = self._replays[request_id]
        elif path == "/v2/checkout/orders":
            status, body = self._create(json.loads(request.content))
        elif match:
            status, body = self._capture(match.group(1))
        else:
            status, body = 404, {"name": "RESOURCE_NOT_FOUND"}
        if request_id:
            self._replays[request_id] = (status, body)

        # Applies to replays too, so a response can be lost on every attempt.
        if match and status == 201 and self.drop_capture_responses > 0:
            self.drop_capture_responses -= 1
            raise httpx.ReadTimeout("simulated: capture done, response lost", request=request)
        return httpx.Response(status, json=body)

    def _create(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        order_id = f"ORDER-{len(self.orders) + 1:04d}"
        order = {
            "id": order_id,
            "status": "CREATED",
            "intent": body["intent"],
            "purchase_units": body["purchase_units"],
        }
        self.orders[order_id] = order
        return 201, order

    def _capture(self, order_id: str) -> tuple[int, dict[str, Any]]:
        order = self.orders.get(order_id)
        if order is None:
            return 404, {"name": "RESOURCE_NOT_FOUND"}
        if self.fail_captures_with:
            return 422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": self.fail_captures_with}]}
        if order["status"] == "COMPLETED":
            return 422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]}
        capture_id = f"CAPTURE-{len(self.captures) + 1:04d}"
        self.captures.append(capture_id)
        amount = order["purchase_units"][0]["amount"]
        order["status"] = "COMPLETED"
        order["purchase_units"][0]["payments"] = {
            "captures": [{"id": capture_id, "status": "COMPLETED", "amount": amount}]
        }
        return 201, order


class ScriptedLLM:
    """Duck-types ``openai.AsyncOpenAI`` for chat.completions.create."""

    def __init__(self, item: str = "AMW sticker pack") -> None:
        self.item = item
        self.requests: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        function_name = kwargs["tools"][0]["function"]["name"]
        tool_call = SimpleNamespace(
            id=f"call_{uuid.uuid4().hex[:24]}",
            type="function",
            function=SimpleNamespace(name=function_name, arguments=json.dumps({"item": self.item})),
        )
        message = SimpleNamespace(role="assistant", content=None, tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class LostResponseTransport(httpx.AsyncBaseTransport):
    """Forwards to the gateway, then loses the first ``drops`` tool-call responses."""

    def __init__(self, inner: httpx.AsyncBaseTransport, drops: int = 1) -> None:
        self._inner = inner
        self.drops = drops
        self.lost: list[dict[str, Any]] = []  # bodies the caller never received

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if request.url.path == "/mcp/messages" and self.drops > 0:
            self.drops -= 1
            self.lost.append(json.loads(await response.aread()))
            raise httpx.ReadTimeout("simulated: gateway committed, response lost", request=request)
        return response
