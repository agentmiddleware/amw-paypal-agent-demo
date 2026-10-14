#!/usr/bin/env python3
"""Four scenes: an LLM agent pays through PayPal behind a governed gateway.

1. Happy path: permit capped at the price, one checkout, one signed receipt.
2. Lost response: the gateway captures, the response times out, the agent
   retries the same tool call. Same receipt, at most one PayPal capture.
3. Over budget: the permit cap is below the price. The gateway denies before the tool
   runs, so PayPal is never called.
4. Offline verification: the receipt from scene 1 is checked against the
   published key set from files on disk, after the gateway has shut down.

PayPal runs against the sandbox when PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET
are set, otherwise against an in-process fake. The LLM is OpenAI when
OPENAI_API_KEY is set, otherwise a scripted stand-in.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path

from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle
from openai_b2a import B2AClient

from agent import CheckoutOutcome, model_from_env, run_checkout_agent
from fakes import FakePayPal, LostResponseTransport, ScriptedLLM
from gateway import Gateway
from paypal_client import PayPalClient
from paypal_tool import TOOL_NAME, register_checkout_tool

WIDTH = 64
REQUEST = "Buy one AMW sticker pack for the team. Spend no more than ${cap}."


def say(text: str = "") -> None:
    print(text, flush=True)


def scene(title: str) -> None:
    say()
    say("=" * WIDTH)
    say(f"  {title}")
    say("=" * WIDTH)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"DEMO CHECK FAILED: {message}")


class Demo:
    def __init__(self, gateway: Gateway, paypal: PayPalClient, fake: FakePayPal | None, llm) -> None:
        self.gw = gateway
        self.paypal = paypal
        self.fake = fake
        self.llm = llm
        self.lossy: LostResponseTransport | None = None

    def captures(self) -> int:
        # The fake counts captures it executed; live mode counts distinct
        # capture ids PayPal returned to this process.
        return len(self.fake.captures) if self.fake else self.paypal.distinct_captures

    async def checkout(self, agent_id: str, cap: Decimal, *, lose_response: bool = False):
        creds = await self.gw.provision_agent(agent_id)
        transport = self.gw.transport()
        if lose_response:
            transport = self.lossy = LostResponseTransport(transport)
        async with B2AClient(api_key=creds.api_key, base_url="http://amw", transport=transport) as trust:
            outcome = await run_checkout_agent(
                llm=self.llm,
                trust=trust,
                wallet_id=creds.wallet_id,
                run_id=f"{agent_id}-run",
                cap_usd=cap,
                request=REQUEST.format(cap=cap),
            )
        return creds, outcome

    async def debits(self, creds) -> int:
        async with self.gw.http(creds.api_key) as client:
            ledger = (await client.get(f"/v1/billing/ledger/{creds.wallet_id}")).json()
        return sum(1 for e in ledger["entries"] if TOOL_NAME in e.get("description", ""))


def show(outcome: CheckoutOutcome) -> None:
    say(f"  agent status:      {outcome.status} after {outcome.attempts} attempt(s)")
    if outcome.receipt:
        r = outcome.receipt
        say(f"  receipt:           {r.receipt_id}  outcome={r.outcome}")
        say(f"  credits charged:   {r.credits_charged} (1 credit = 1 US cent)")
        say(f"  permit:            {r.permit_id}")
    if outcome.payment:
        p = outcome.payment
        say(f"  PayPal:            order {p['order_id']}  capture {p['capture_id']}  {p['status']}")
        say(f"  amount:            {p['amount']} {p['currency']}")
    if outcome.denial_reason:
        say(f"  reason:            {outcome.denial_reason}")


async def run(args: argparse.Namespace) -> dict:
    price = Decimal(os.environ.get("CHECKOUT_PRICE_USD", "25.00"))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    live_paypal = not args.mock_paypal and bool(
        os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET")
    )
    live_llm = not args.mock_llm and bool(os.environ.get("OPENAI_API_KEY"))
    fake = None
    if live_paypal:
        paypal = PayPalClient()
    else:
        fake = FakePayPal()
        # Placeholder credentials for the fake only; nothing leaves the process.
        os.environ["PAYPAL_CLIENT_ID"] = "offline-placeholder-id"
        os.environ["PAYPAL_CLIENT_SECRET"] = "offline-placeholder-secret"
        paypal = PayPalClient(base_url="https://paypal.invalid", transport=fake.transport())
    if live_llm:
        from openai import AsyncOpenAI

        llm = AsyncOpenAI()
    else:
        llm = ScriptedLLM()

    scene("Setup")
    say(f"  PayPal:  {'sandbox (live)' if live_paypal else 'offline fake'}")
    say(f"  LLM:     {'OpenAI ' + model_from_env() if live_llm else 'scripted stand-in'}")
    say(f"  Price:   {price} USD, set by the merchant at tool registration")

    gw = Gateway(Path(tempfile.mkdtemp(prefix="amw-paypal-")) / "amw.db")
    await gw.start()
    register_checkout_tool(gw.registry, paypal, price_usd=price)
    demo = Demo(gw, paypal, fake, llm)
    summary: dict = {"paypal": "live" if live_paypal else "fake", "llm": "live" if live_llm else "scripted"}
    try:
        scene("Scene 1: happy path")
        say(f"  The user allows up to {price} USD. The permit is capped at that.")
        creds1, happy = await demo.checkout("agent-happy", price)
        show(happy)
        require(happy.status == "captured", "happy path did not capture")
        require(happy.receipt.outcome == "success", "happy path receipt is not success")
        summary["happy_receipt_id"] = happy.receipt.receipt_id
        summary["captures_after_happy"] = demo.captures()

        scene("Scene 2: response lost after capture, then retry")
        before = demo.captures()
        creds2, retried = await demo.checkout("agent-retry", price, lose_response=True)
        say("  First attempt: gateway charged, PayPal captured, receipt written,")
        say("  then the response timed out before it reached the agent.")
        say("  The agent retried the same tool call (same idempotency key).")
        show(retried)
        lost_receipt_id = demo.lossy.lost[0]["result"]["receipt"]["receipt_id"]
        debits = await demo.debits(creds2)
        new_captures = demo.captures() - before
        say(f"  receipt in the lost response:  {lost_receipt_id}")
        say(f"  receipt returned on retry:     {retried.receipt.receipt_id}")
        say(f"  PayPal captures for this checkout: {new_captures}")
        say(f"  Wallet debits for this checkout: {debits}")
        require(retried.status == "captured" and retried.attempts == 2, "retry did not recover")
        require(lost_receipt_id == retried.receipt.receipt_id, "retry returned a different receipt")
        require(new_captures == 1, f"expected one PayPal capture, saw {new_captures}")
        require(debits == 1, "a retry produced a second wallet debit")
        summary.update(retry_receipt_id=retried.receipt.receipt_id, retry_captures=new_captures, retry_debits=debits)

        scene("Scene 3: over budget")
        cap = (price / 2).quantize(Decimal("0.01"))
        say(f"  The user allows only {cap} USD. The price is {price} USD.")
        before = demo.captures()
        writes_before = fake.writes if fake else None
        _, denied = await demo.checkout("agent-over-budget", cap)
        show(denied)
        require(denied.status == "denied", "over-budget checkout was not denied")
        require(demo.captures() == before, "over-budget checkout reached PayPal")
        if fake:
            require(fake.writes == writes_before, "over-budget checkout sent a PayPal request")
            say("  PayPal requests sent: 0")
        summary["over_budget_reason"] = denied.denial_reason

        scene("Scene 4: offline receipt verification")
        receipt_id = happy.receipt.receipt_id
        async with gw.http(creds1.api_key) as client:
            bundle = (await client.get(f"/v1/receipts/{receipt_id}/portable")).json()
            keys = (await client.get("/.well-known/trust-keys.json")).json()
        bundle_path = out_dir / f"{receipt_id}.json"
        keys_path = out_dir / "trust-keys.json"
        bundle_path.write_text(json.dumps(bundle, indent=2))
        keys_path.write_text(json.dumps(keys, indent=2))
    finally:
        gw.registry.unregister_local(TOOL_NAME)
        await gw.stop()
        await paypal.aclose()

    say("  Gateway stopped. Verifying from files only:")
    say(f"    {bundle_path}")
    say(f"    {keys_path}")
    key_set = key_set_from_document(json.loads(keys_path.read_text()))
    result = verify_bundle(json.loads(bundle_path.read_text()), key_set)
    tampered = json.loads(bundle_path.read_text())
    tampered["signing_input"] = tampered["signing_input"].replace("success", "failure", 1)
    forged = verify_bundle(tampered, key_set)
    say(f"  signature verified: {result.ok} (key {result.key_id})")
    say(f"  edited copy:        {forged.status.value}")
    require(result.ok, f"receipt failed offline verification: {result.reason}")
    require(not forged.ok, "an edited receipt verified")
    say()
    say(f"  Same check with the SDK CLI: b2a-verify-receipt --bundle {bundle_path} --keys {keys_path}")
    summary.update(offline_verified=result.ok, edited_copy=forged.status.value)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mock-paypal", action="store_true", help="use the offline PayPal fake")
    parser.add_argument("--mock-llm", action="store_true", help="use the scripted LLM")
    parser.add_argument("--out", default="out", help="where receipt files are written")
    parser.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = asyncio.run(run(args))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
