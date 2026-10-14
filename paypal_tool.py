"""Registers ``paypal_checkout`` as a governed tool on the mini gateway.

The b2a_sdk ``@mcp_tool`` decorator carries the tool metadata, and
``ToolRegistry.register_local`` makes it executable. Every tool on the mini
gateway needs a permit; there is no unpermitted path.

Pricing convention for this demo: 1 credit = 1 US cent. The checkout price is
fixed by the merchant at registration, so the gateway charges the order amount
and checks it against the permit before the tool body runs. The LLM never
chooses the price.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from b2a_sdk.decorators import mcp_tool

from mini_gateway import ToolRegistry

from paypal_client import PayPalClient, PayPalError

TOOL_NAME = "paypal_checkout"
TOOL_DESCRIPTION = "Create and capture one PayPal sandbox order for the listed item."


def usd_to_credits(amount_usd: Decimal) -> Decimal:
    return (Decimal(amount_usd) * 100).quantize(Decimal("1"))


def build_checkout_tool(paypal: PayPalClient, *, price_usd: Decimal, currency: str = "USD"):
    """Return the decorated tool handler bound to one PayPal client and price."""

    @mcp_tool(
        service_id=TOOL_NAME,
        name="PayPal Checkout",
        description=TOOL_DESCRIPTION,
        category="agent_comms",
        credits_per_unit=float(usd_to_credits(price_usd)),
        unit_name="checkout",
    )
    async def paypal_checkout(item: str, amw_idempotency_key: str) -> dict[str, Any]:
        order = await paypal.create_order(
            amount=price_usd,
            currency=currency,
            description=item,
            amw_idempotency_key=amw_idempotency_key,
        )
        if order.get("status") == "COMPLETED":
            captured = order  # captured in one step (card payment source)
        else:
            try:
                captured = await paypal.capture_order(order["id"], amw_idempotency_key=amw_idempotency_key)
            except PayPalError as exc:
                # No response does not mean no capture. Raising here makes the gateway
                # refund the credits, so first ask PayPal what happened.
                if not exc.ambiguous:
                    raise
                captured = await paypal.get_order(order["id"])
        capture = _first_capture(captured)
        if captured.get("status") != "COMPLETED" or capture.get("status") not in ("COMPLETED", "PENDING"):
            raise PayPalError(f"PayPal order {order['id']} was not captured")
        return {
            "order_id": captured.get("id", order["id"]),
            "capture_id": capture.get("id"),
            "capture_status": capture.get("status"),
            "status": captured.get("status"),
            "amount": f"{price_usd:.2f}",
            "currency": currency,
            "item": item,
        }

    return paypal_checkout


def _first_capture(order: dict[str, Any]) -> dict[str, Any]:
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]
    except (KeyError, IndexError, TypeError):
        return {}


def register_checkout_tool(registry: ToolRegistry, paypal: PayPalClient, *, price_usd: Decimal) -> dict:
    """Register the tool on a mini gateway ``ToolRegistry``."""
    handler = build_checkout_tool(paypal, price_usd=price_usd)
    meta = handler._b2a_mcp_metadata
    return registry.register_local(
        service_id=meta["service_id"],
        name=meta["name"],
        description=meta["description"],
        category=meta["category"],
        func=handler,
        credits_per_unit=meta["credits_per_unit"],
        unit_name=meta["unit_name"],
        # The gateway fills this in from the call's own idempotency key.
        idempotency_key_arg="amw_idempotency_key",
    )
