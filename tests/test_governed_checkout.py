"""The agent, the in-process mini gateway, and a fake PayPal, end to end."""

from __future__ import annotations

import json
from decimal import Decimal

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle
from openai_b2a import B2AClient

from agent import run_checkout_agent
from fakes import LostResponseTransport, ScriptedLLM
from paypal_client import paypal_request_id
from paypal_tool import TOOL_NAME

from .conftest import PRICE


async def _checkout(gateway, *, agent_id, cap, llm=None, transport=None, creds=None, wallet_id=None):
    creds = creds or await gateway.provision_agent(agent_id)
    async with B2AClient(
        api_key=creds.api_key, base_url="http://amw", transport=transport or gateway.transport()
    ) as trust:
        outcome = await run_checkout_agent(
            llm=llm or ScriptedLLM(),
            trust=trust,
            wallet_id=wallet_id or creds.wallet_id,
            run_id=f"{agent_id}-run",
            cap_usd=cap,
            request=f"Buy stickers, up to {cap} USD.",
            model="test-model",
        )
    return creds, outcome


async def _tool_debits(gateway, creds) -> int:
    async with gateway.http(creds.api_key) as client:
        entries = (await client.get(f"/v1/billing/ledger/{creds.wallet_id}")).json()["entries"]
    return sum(1 for e in entries if TOOL_NAME in e.get("description", ""))


async def _balance(gateway, creds) -> Decimal:
    async with gateway.http(creds.api_key) as client:
        wallet = (await client.get(f"/v1/billing/wallets/{creds.wallet_id}")).json()
    return Decimal(str(wallet["balance"]))


async def test_happy_path_charges_the_price_and_captures_once(live_gateway, fake_paypal):
    creds, outcome = await _checkout(live_gateway, agent_id="happy", cap=PRICE)

    assert outcome.status == "captured"
    assert outcome.receipt.outcome == "success"
    assert outcome.receipt.credits_charged == Decimal("2500")
    assert outcome.payment["capture_id"] == "CAPTURE-0001"
    assert fake_paypal.captures == ["CAPTURE-0001"]
    assert await _tool_debits(live_gateway, creds) == 1


async def test_lost_response_retry_returns_same_receipt_and_one_capture_per_key(
    live_gateway, fake_paypal
):
    # This exercises the gateway's replay: the tool body does not run on the retry.
    # PayPal-Request-Id reuse is covered in test_paypal_client.py.
    lossy = LostResponseTransport(live_gateway.transport())
    creds, outcome = await _checkout(live_gateway, agent_id="retry", cap=PRICE, transport=lossy)

    lost_receipt = lossy.lost[0]["result"]["receipt"]["receipt_id"]
    assert outcome.status == "captured"
    assert outcome.attempts == 2
    assert outcome.receipt.receipt_id == lost_receipt
    assert len(fake_paypal.captures) == 1
    assert await _tool_debits(live_gateway, creds) == 1

    # A different tool call is a different key, so it is a new checkout (new
    # run, because the first run's permit budget is spent).
    _, second = await _checkout(live_gateway, agent_id="retry-2", cap=PRICE, creds=creds)
    assert second.receipt.receipt_id != lost_receipt
    assert len(fake_paypal.captures) == 2


async def test_over_budget_is_denied_before_paypal(live_gateway, fake_paypal):
    creds, outcome = await _checkout(live_gateway, agent_id="over", cap=Decimal("10.00"))

    assert outcome.status == "denied"
    assert outcome.denial_reason == "permit_budget_exceeded"
    assert fake_paypal.writes == 0
    assert fake_paypal.captures == []
    assert await _tool_debits(live_gateway, creds) == 0


async def test_cross_wallet_call_is_refused_before_paypal(live_gateway, fake_paypal):
    victim = await live_gateway.provision_agent("victim")
    attacker = await live_gateway.provision_agent("attacker")

    _, outcome = await _checkout(
        live_gateway, agent_id="attacker", cap=PRICE, creds=attacker, wallet_id=victim.wallet_id
    )
    assert outcome.status == "failed"
    assert outcome.denial_reason == "wallet_access_denied", outcome.denial_reason
    assert fake_paypal.writes == 0
    assert await _tool_debits(live_gateway, victim) == 0
    assert await _tool_debits(live_gateway, attacker) == 0


async def test_capture_with_lost_responses_is_looked_up_not_refunded(live_gateway, fake_paypal):
    # PayPal captures but both capture responses are lost. The tool must not
    # raise (the gateway would refund the credits while the money moved).
    fake_paypal.drop_capture_responses = 2
    creds, outcome = await _checkout(live_gateway, agent_id="ambiguous", cap=PRICE)

    assert outcome.status == "captured"
    assert outcome.payment["capture_status"] == "COMPLETED"
    assert fake_paypal.captures == ["CAPTURE-0001"]
    assert await _balance(live_gateway, creds) == Decimal("7500")  # 10000 minus one 2500 charge


async def test_rejected_capture_fails_and_is_refunded(live_gateway, fake_paypal):
    fake_paypal.fail_captures_with = "ORDER_NOT_APPROVED"
    creds, outcome = await _checkout(live_gateway, agent_id="unapproved", cap=PRICE)

    assert outcome.status == "failed"
    assert fake_paypal.captures == []
    assert await _balance(live_gateway, creds) == Decimal("10000")


async def test_model_cannot_choose_the_paypal_request_id(live_gateway, fake_paypal):
    class InjectingLLM(ScriptedLLM):
        async def _create(self, **kwargs):
            completion = await super()._create(**kwargs)
            call = completion.choices[0].message.tool_calls[0]
            call.function.arguments = json.dumps(
                {"item": "stickers", "amw_idempotency_key": "attacker-chosen", "amount": "0.01"}
            )
            return completion

    _, outcome = await _checkout(live_gateway, agent_id="inject", cap=PRICE, llm=InjectingLLM())

    assert outcome.status == "captured"
    assert outcome.payment["amount"] == "25.00"
    expected = paypal_request_id(f"oai-{outcome.tool_call_id}", "capture")
    assert expected in fake_paypal._replays
    assert paypal_request_id("attacker-chosen", "capture") not in fake_paypal._replays


async def test_receipt_verifies_offline_and_edits_are_rejected(live_gateway):
    creds, outcome = await _checkout(live_gateway, agent_id="verify", cap=PRICE)
    receipt_id = outcome.receipt.receipt_id
    async with live_gateway.http(creds.api_key) as client:
        bundle = (await client.get(f"/v1/receipts/{receipt_id}/portable")).json()
        keys = (await client.get("/.well-known/trust-keys.json")).json()

    # From here on only plain data is used; no gateway call.
    key_set = key_set_from_document(json.loads(json.dumps(keys)))
    assert verify_bundle(json.loads(json.dumps(bundle)), key_set).ok

    edited = dict(bundle)
    edited["signing_input"] = bundle["signing_input"].replace("success", "failure", 1)
    assert edited["signing_input"] != bundle["signing_input"]
    assert not verify_bundle(edited, key_set).ok
