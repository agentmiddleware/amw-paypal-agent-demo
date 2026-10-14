"""An OpenAI tool-calling agent that pays through AMW.

Flow: the model sees ``paypal_checkout`` as a function tool. When it calls it,
AMW's OpenAI wrapper (``GovernedToolRunner``) obtains a permit for this run
capped at the user's spending limit, then invokes the tool under the
idempotency key ``oai-<tool_call.id>``. A lost response is retried with the
same tool call, so AMW replays the stored receipt instead of paying again.

Model: ``OPENAI_MODEL`` (default below). The OpenAI client is passed in, so
tests and the offline demo can supply a scripted one.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from b2a_sdk.errors import AgentMiddlewareError, PermitDeniedError, TransportError
from b2a_sdk.models import Receipt
from openai_b2a import B2AClient, GovernedToolRunner, normalize_tool_call
from openai_b2a.runner import operation_key_for

from paypal_tool import TOOL_DESCRIPTION, TOOL_NAME, usd_to_credits

DEFAULT_MODEL = "gpt-4.1-mini"

SYSTEM_PROMPT = (
    "You are a purchasing agent. When the user asks to buy something, call "
    "paypal_checkout exactly one time with a short description of the item. "
    "The merchant sets the price; never put a price in the arguments."
)

TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"item": {"type": "string", "description": "What is being bought."}},
    "required": ["item"],
    "additionalProperties": False,
}


def model_from_env() -> str:
    return os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL


@dataclass
class CheckoutOutcome:
    status: str  # "captured", "denied", "failed" or "no_tool_call"
    tool_call_id: str | None = None
    receipt: Receipt | None = None
    payment: dict[str, Any] | None = None
    denial_reason: str | None = None
    attempts: int = 0


async def run_checkout_agent(
    *,
    llm: Any,
    trust: B2AClient,
    wallet_id: str,
    run_id: str,
    cap_usd: Decimal,
    request: str,
    model: str | None = None,
    max_attempts: int = 3,
) -> CheckoutOutcome:
    runner = GovernedToolRunner(
        trust,
        wallet_id=wallet_id,
        run_id=run_id,
        permit_budget=usd_to_credits(cap_usd),
    )
    tools = [runner.register_tool(TOOL_NAME, description=TOOL_DESCRIPTION, parameters=TOOL_PARAMETERS)]
    completion = await llm.chat.completions.create(
        model=model or model_from_env(),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request},
        ],
        tools=tools,
    )
    tool_calls = completion.choices[0].message.tool_calls or []
    if not tool_calls:
        return CheckoutOutcome(status="no_tool_call")

    # One checkout per request: extra tool calls in the same message are ignored
    # rather than turned into extra payments.
    call = normalize_tool_call(tool_calls[0])
    # Only the item comes from the model. The AMW key is derived from the tool
    # call id here, so the model cannot choose or reuse one.
    governed_call = {
        "id": call.id,
        "function": {
            "name": call.name,
            "arguments": json.dumps(
                {
                    "item": str(call.arguments.get("item", ""))[:127],
                    "amw_idempotency_key": operation_key_for(call.id),
                }
            ),
        },
    }

    for attempt in range(1, max_attempts + 1):
        try:
            result = await runner.run(governed_call)
        except TransportError:
            # Response lost: retry the same tool call. AMW replays the stored
            # outcome for this key instead of running the tool again.
            if attempt == max_attempts:
                raise
            continue
        except PermitDeniedError as exc:
            return CheckoutOutcome(
                status="denied",
                tool_call_id=call.id,
                denial_reason=exc.reason,
                attempts=attempt,
            )
        except AgentMiddlewareError as exc:
            # Tool failed (AMW refunded the charge) or AMW refused the call.
            return CheckoutOutcome(
                status="failed",
                tool_call_id=call.id,
                denial_reason=str(exc),
                attempts=attempt,
            )
        return CheckoutOutcome(
            status="captured",
            tool_call_id=call.id,
            receipt=result.receipt,
            payment=_payment_from(result.result),
            attempts=attempt,
        )
    raise AssertionError("unreachable")


def _payment_from(result: Any) -> dict[str, Any] | None:
    if result.structured_content:
        return result.structured_content
    for item in result.content:
        if item.get("type") == "text":
            try:
                return json.loads(item["text"])
            except (ValueError, KeyError):
                return None
    return None
