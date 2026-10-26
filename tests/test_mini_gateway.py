"""The mini gateway on its own, driven through the vendored b2a_sdk client."""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from b2a_sdk import AgentMiddlewareClient
from b2a_sdk.errors import (
    APIError,
    AuthorizationError,
    DeliveryUncertainError,
    IdempotencyConflictError,
    PermitDeniedError,
)
from b2a_sdk.models import PermitRequest
from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

from mini_gateway import MiniGateway, Signer

PRICE = 40


class Tools:
    """Async tools that count how often their body actually ran."""

    def __init__(self) -> None:
        self.runs = 0
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def echo(self, text: str) -> dict:
        self.runs += 1
        return {"echo": text}

    async def broken(self, text: str) -> dict:
        self.runs += 1
        raise RuntimeError("upstream said no")

    async def slow(self, text: str) -> dict:
        self.runs += 1
        self.entered.set()
        await self.release.wait()
        return {"slow": text}


@pytest.fixture
def tools() -> Tools:
    return Tools()


@pytest.fixture
def gw(tmp_path, tools):
    gateway = MiniGateway(tmp_path / "gw.db", admin_key=secrets.token_urlsafe(32))
    gateway.start()
    for name in ("echo", "broken", "slow"):
        gateway.registry.register_local(
            service_id=name, name=name, description=name, func=getattr(tools, name), credits_per_unit=PRICE
        )
    yield gateway
    gateway.stop()


def http(gw: MiniGateway, key: str | None) -> httpx.AsyncClient:
    headers = {"X-API-Key": key} if key else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gw.app), base_url="http://gw", headers=headers)


async def provision(gw: MiniGateway, credits: int = 1000) -> tuple[str, str]:
    async with http(gw, gw.admin_key) as admin:
        sponsor = (await admin.post("/v1/billing/wallets/sponsor", json={"initial_credits": credits})).json()
        agent = (
            await admin.post(
                "/v1/billing/wallets/agent",
                json={"sponsor_wallet_id": sponsor["wallet_id"], "agent_id": "a", "budget_credits": credits},
            )
        ).json()
        key = (await admin.post("/v1/api-keys", json={"wallet_id": agent["wallet_id"]})).json()
    return agent["wallet_id"], key["api_key"]


def sdk(gw: MiniGateway, key: str) -> AgentMiddlewareClient:
    return AgentMiddlewareClient(api_key=key, base_url="http://gw", transport=httpx.ASGITransport(app=gw.app))


async def permit(client, wallet_id: str, *, max_credits: int, tools=("echo", "broken", "slow"), key="p1",
                 expires_at: datetime | None = None):
    request = PermitRequest(
        issuer_wallet_id=wallet_id,
        subject_wallet_id=wallet_id,
        max_credits=Decimal(max_credits),
        expires_at=expires_at or datetime.now(UTC) + timedelta(minutes=5),
        allowed_tools=list(tools),
        scopes=[f"tool:{t}:invoke" for t in tools],
    )
    return await client.create_permit(request, idempotency_key=key)


async def balance(gw: MiniGateway, key: str, wallet_id: str) -> Decimal:
    async with http(gw, key) as client:
        return Decimal((await client.get(f"/v1/billing/wallets/{wallet_id}")).json()["balance"])


async def mcp_call(gw, api_key, wallet_id, permit_id, *, meta=None, context_key=None, header_key=None,
                   text="x", request_id=1):
    params = {
        "name": "echo",
        "arguments": {"text": text},
        "mcpContext": {"wallet_id": wallet_id, "permit_id": permit_id},
    }
    if context_key is not None:
        params["mcpContext"]["idempotency_key"] = context_key
    if meta is not None:
        params["_meta"] = {"io.agentmiddleware/idempotency_key": meta}
    async with http(gw, api_key) as client:
        response = await client.post(
            "/mcp/messages",
            json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params},
            headers={"Idempotency-Key": header_key} if header_key is not None else {},
        )
    assert response.status_code == 200
    return response.json()


async def test_meta_key_is_accepted_and_conflicts_with_legacy_paths_are_rejected(gw, tools):
    wallet_id, api_key = await provision(gw)
    async with sdk(gw, api_key) as client:
        p = await permit(client, wallet_id, max_credits=1000)

    first = await mcp_call(gw, api_key, wallet_id, p.permit_id, meta="meta-only")
    replay = await mcp_call(gw, api_key, wallet_id, p.permit_id, meta="meta-only", request_id=2)
    assert first["result"]["receipt"] == replay["result"]["receipt"]
    assert replay["id"] == 2
    assert tools.runs == 1

    for conflicting in (
        {"meta": "meta-key", "context_key": "other-key"},
        {"meta": "meta-key", "header_key": "other-key"},
        {"context_key": "context-key", "header_key": "other-key"},
    ):
        result = await mcp_call(gw, api_key, wallet_id, p.permit_id, **conflicting)
        assert result["error"]["message"] == "idempotency_key_mismatch"
    malformed = await mcp_call(gw, api_key, wallet_id, p.permit_id, meta=0, header_key="header-key")
    assert malformed["error"]["message"] == "invalid_idempotency_key"
    assert tools.runs == 1
    assert await balance(gw, api_key, wallet_id) == 1000 - PRICE

    legacy = await mcp_call(gw, api_key, wallet_id, p.permit_id, context_key="legacy")
    assert legacy["result"]["receipt"]["outcome"] == "success"
    assert tools.runs == 2


async def test_budget_denial_is_signed_stored_and_replayed_without_charge(gw, tools):
    wallet_id, api_key = await provision(gw)
    async with sdk(gw, api_key) as client:
        p = await permit(client, wallet_id, max_credits=PRICE - 1)

    first = await mcp_call(gw, api_key, wallet_id, p.permit_id, meta="deny-key")
    gw.stop()
    gw.start()
    replay = await mcp_call(
        gw, api_key, wallet_id, p.permit_id, meta="deny-key", request_id=2
    )
    assert first["error"]["message"] == "permit_budget_exceeded"
    assert first["error"]["data"]["receipt"] == replay["error"]["data"]["receipt"]
    assert replay["id"] == 2
    receipt = first["error"]["data"]["receipt"]
    assert receipt["outcome"] == "permit_budget_exceeded"
    assert receipt["credits_charged"] == "0"
    assert receipt["ledger_entry_id"] is None
    assert tools.runs == 0
    assert await balance(gw, api_key, wallet_id) == 1000
    assert gw.store.get_operation(wallet_id, "deny-key")["state"] == "completed"

    async with http(gw, api_key) as client:
        bundle = (await client.get(f"/v1/receipts/{receipt['receipt_id']}/portable")).json()
        keys = (await client.get("/.well-known/trust-keys.json")).json()
        ledger = (await client.get(f"/v1/billing/ledger/{wallet_id}")).json()["entries"]
    assert verify_bundle(bundle, key_set_from_document(keys)).ok
    assert [entry["action"] for entry in ledger] == ["provision"]

    conflict = await mcp_call(gw, api_key, wallet_id, p.permit_id, meta="deny-key", text="changed")
    assert conflict["error"]["message"] == "idempotency_key_reused"


async def test_budget_is_enforced_before_the_tool_runs(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=PRICE + 10)
        await client.invoke_tool("echo", {"text": "a"}, wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="k1")
        with pytest.raises(PermitDeniedError) as denied:
            await client.invoke_tool(
                "echo", {"text": "b"}, wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="k2"
            )
    assert denied.value.reason == "permit_budget_exceeded"
    assert tools.runs == 1
    assert await balance(gw, key, wallet_id) == 1000 - PRICE


async def test_retry_with_same_key_replays_the_same_receipt_and_charges_once(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=1000)
        args = dict(wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="same-key")
        first = await client.invoke_tool("echo", {"text": "hi"}, **args)
        again = await client.invoke_tool("echo", {"text": "hi"}, **args)
        with pytest.raises(IdempotencyConflictError, match="idempotency_key_reused"):
            await client.invoke_tool("echo", {"text": "different"}, **args)
    assert again.receipt.receipt_id == first.receipt.receipt_id
    assert again.structured_content == {"echo": "hi"}
    assert tools.runs == 1
    assert await balance(gw, key, wallet_id) == 1000 - PRICE


async def test_tool_that_raises_is_refunded_with_a_signed_receipt(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=PRICE)
        args = dict(wallet_id=wallet_id, permit_id=p.permit_id)
        with pytest.raises(APIError, match="tool_failed") as failed:
            await client.invoke_tool("broken", {"text": "x"}, idempotency_key="b1", **args)
        receipt = failed.value.payload["result"]["receipt"]
        # The refund also gives the permit budget back, so a new call fits.
        ok = await client.invoke_tool("echo", {"text": "x"}, idempotency_key="b2", **args)
    assert receipt["outcome"] == "tool_error"
    assert receipt["credits_charged"] == "0"
    assert ok.receipt.outcome == "success"
    assert await balance(gw, key, wallet_id) == 1000 - PRICE
    async with http(gw, key) as c:
        actions = [e["action"] for e in (await c.get(f"/v1/billing/ledger/{wallet_id}")).json()["entries"]]
    assert actions == ["provision", "charge", "refund", "charge"]


async def test_receipt_verifies_offline_and_a_tampered_copy_fails(gw):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=PRICE)
        result = await client.invoke_tool(
            "echo", {"text": "v"}, wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="v1"
        )
    async with http(gw, key) as c:
        bundle = (await c.get(f"/v1/receipts/{result.receipt.receipt_id}/portable")).json()
    async with http(gw, None) as anonymous:
        keys = (await anonymous.get("/.well-known/trust-keys.json")).json()
    gw.stop()  # from here on, plain data only

    key_set = key_set_from_document(keys)
    verified = verify_bundle(bundle, key_set)
    assert verified.ok, verified.reason
    assert verified.claims["credits_charged"] == str(Decimal(PRICE))

    for field, old, new in [("signing_input", '"outcome":"success"', '"outcome":"failure"'),
                            ("signing_input", f'"credits_charged":"{PRICE}"', '"credits_charged":"1"')]:
        tampered = dict(bundle, **{field: bundle[field].replace(old, new)})
        assert tampered[field] != bundle[field]
        assert verify_bundle(tampered, key_set).is_tampered

    # A different key published under the same kid does not verify it either.
    impostor = Signer(gw.signer.key_id, issuer=gw.signer.issuer)
    assert verify_bundle(bundle, key_set_from_document(impostor.key_set_document())).is_tampered


async def test_retry_while_first_attempt_runs_is_refused_not_run_twice(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=1000)
        args = dict(wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="slow-1")
        first = asyncio.create_task(client.invoke_tool("slow", {"text": "s"}, **args))
        await tools.entered.wait()
        with pytest.raises(IdempotencyConflictError, match="idempotency_in_progress"):
            await client.invoke_tool("slow", {"text": "s"}, **args)
        tools.release.set()
        done = await first
        replay = await client.invoke_tool("slow", {"text": "s"}, **args)
    assert replay.receipt.receipt_id == done.receipt.receipt_id
    assert tools.runs == 1


async def test_interrupted_call_becomes_delivery_uncertain_and_keeps_the_charge(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=1000)
        args = dict(wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="cut-1")
        first = asyncio.create_task(client.invoke_tool("slow", {"text": "s"}, **args))
        await tools.entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        with pytest.raises(DeliveryUncertainError) as uncertain:
            await client.invoke_tool("slow", {"text": "s"}, **args)
    assert tools.runs == 1
    assert await balance(gw, key, wallet_id) == 1000 - PRICE
    async with http(gw, key) as c:
        receipt = (await c.get(f"/v1/receipts/{uncertain.value.receipt_id}")).json()
    assert receipt["outcome"] == "delivery_uncertain"


async def test_call_left_in_flight_by_a_dead_process_is_uncertain_after_restart(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=1000)
    # Simulate a crash: reserve the call, then never run or finish it.
    gw.governor._admit(
        {"name": "echo", "arguments": {"text": "z"},
         "mcpContext": {"wallet_id": wallet_id, "permit_id": p.permit_id, "idempotency_key": "crash-1"}},
        caller={"admin": False, "wallet_id": wallet_id, "key_id": None},
        header_key="crash-1",
    )
    gw.stop()
    assert gw.start() == 1
    async with sdk(gw, key) as client:
        with pytest.raises(DeliveryUncertainError):
            await client.invoke_tool(
                "echo", {"text": "z"}, wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="crash-1"
            )
    assert tools.runs == 0


async def test_another_wallets_key_can_not_spend_or_read(gw, tools):
    victim_wallet, victim_key = await provision(gw)
    _, attacker_key = await provision(gw)
    async with sdk(gw, victim_key) as victim:
        p = await permit(victim, victim_wallet, max_credits=1000)
    async with sdk(gw, attacker_key) as attacker:
        with pytest.raises(AuthorizationError, match="wallet_access_denied"):
            await attacker.invoke_tool(
                "echo", {"text": "x"}, wallet_id=victim_wallet, permit_id=p.permit_id, idempotency_key="steal"
            )
        with pytest.raises(AuthorizationError, match="wallet_access_denied"):
            await permit(attacker, victim_wallet, max_credits=10, key="steal-permit")
    async with http(gw, attacker_key) as c:
        assert (await c.get(f"/v1/billing/wallets/{victim_wallet}")).status_code == 403
    async with http(gw, "mgw_not-a-real-key") as c:
        assert (await c.get(f"/v1/billing/wallets/{victim_wallet}")).status_code == 401
    assert tools.runs == 0
    assert await balance(gw, victim_key, victim_wallet) == 1000


async def test_permit_replays_on_same_key_and_refuses_a_changed_body(gw):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        expires = datetime.now(UTC) + timedelta(minutes=5)
        first = await permit(client, wallet_id, max_credits=100, expires_at=expires)
        same = await permit(client, wallet_id, max_credits=100, expires_at=expires)
        with pytest.raises(IdempotencyConflictError, match="idempotency_key_reused"):
            await permit(client, wallet_id, max_credits=999, expires_at=expires)
    assert same.permit_id == first.permit_id


async def test_permit_for_another_tool_or_scope_is_denied(gw, tools):
    wallet_id, key = await provision(gw)
    async with sdk(gw, key) as client:
        p = await permit(client, wallet_id, max_credits=1000, tools=("broken",))
        with pytest.raises(PermitDeniedError, match="permit_tool_not_allowed"):
            await client.invoke_tool(
                "echo", {"text": "x"}, wallet_id=wallet_id, permit_id=p.permit_id, idempotency_key="t1"
            )
    assert tools.runs == 0


async def test_tools_are_advertised_and_trust_keys_need_no_credential(gw):
    _, key = await provision(gw)
    async with sdk(gw, key) as client:
        names = {t.name for t in await client.discover_tools()}
    async with http(gw, None) as anonymous:
        assert (await anonymous.get("/mcp/tools.json")).status_code == 401
        doc = (await anonymous.get("/.well-known/trust-keys.json")).json()
    assert names == {"echo", "broken", "slow"}
    assert json.dumps(doc).count("public_key_b64") == 1
