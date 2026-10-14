"""HTTP surface of the mini gateway, as an ASGI app.

Routes are the ones the vendored ``b2a_sdk`` client and this demo call:

* ``POST /v1/billing/wallets/sponsor``, ``POST /v1/billing/wallets/agent``,
  ``POST /v1/api-keys`` (admin key only)
* ``GET /v1/billing/wallets/{id}``, ``GET /v1/billing/ledger/{id}``
* ``POST /v1/permits`` (``Idempotency-Key`` required)
* ``POST /mcp/messages`` (JSON-RPC ``tools/list`` and ``tools/call``),
  ``GET /mcp/tools.json``
* ``GET /v1/receipts/{id}``, ``GET /v1/receipts/{id}/portable``,
  ``POST /v1/receipts/verify``
* ``GET /.well-known/trust-keys.json`` (no key needed), ``GET /health``

Every other route needs ``X-API-Key``: either the admin key or a key bound to
one wallet, which can only see and spend from that wallet.
"""

from __future__ import annotations

import hmac
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .governance import METHOD_NOT_FOUND, parse_time, rpc_error
from .signing import sha256_hex
from .store import new_id, now

if TYPE_CHECKING:
    from . import MiniGateway


class HTTPFailure(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPFailure(422, f"invalid_{field}") from None
    if not amount.is_finite() or amount < 0 or (positive and amount == 0):
        raise HTTPFailure(422, f"invalid_{field}")
    return amount


async def _body(request: Request) -> dict:
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        raise HTTPFailure(400, "invalid_json") from None
    if not isinstance(body, dict):
        raise HTTPFailure(400, "invalid_json")
    return body


def build_app(gw: MiniGateway) -> Starlette:
    def caller(request: Request) -> dict:
        if gw.store is None:
            raise HTTPFailure(503, "gateway_not_started")
        presented = request.headers.get("x-api-key", "")
        if presented and hmac.compare_digest(presented.encode(), gw.admin_key.encode()):
            return {"admin": True, "wallet_id": None, "key_id": "admin"}
        key = gw.store.resolve_api_key(presented) if presented else None
        if key is None:
            raise HTTPFailure(401, "invalid_api_key")
        return {"admin": False, "wallet_id": key["wallet_id"], "key_id": key["key_id"]}

    def require_admin(request: Request) -> dict:
        who = caller(request)
        if not who["admin"]:
            raise HTTPFailure(403, "admin_key_required")
        return who

    def require_wallet(who: dict, wallet_id: str) -> dict:
        wallet = gw.store.get_wallet(wallet_id)
        if wallet is None or not (who["admin"] or who["wallet_id"] == wallet_id):
            raise HTTPFailure(403, "wallet_access_denied")
        return wallet

    # -- wallets, keys, ledger ---------------------------------------------

    async def sponsor_wallet(request: Request):
        require_admin(request)
        body = await _body(request)
        owner = str(body.get("sponsor_name") or "sponsor")
        credits = _decimal(body.get("initial_credits", 0), "initial_credits")
        wallet = gw.store.create_wallet(kind="sponsor", owner=owner, credits=credits)
        return JSONResponse(wallet, status_code=201)

    async def agent_wallet(request: Request):
        require_admin(request)
        body = await _body(request)
        sponsor = gw.store.get_wallet(str(body.get("sponsor_wallet_id")))
        if sponsor is None or sponsor["kind"] != "sponsor":
            raise HTTPFailure(404, "sponsor_wallet_not_found")
        credits = _decimal(body.get("budget_credits", 0), "budget_credits")
        if Decimal(sponsor["balance"]) < credits:
            raise HTTPFailure(402, "insufficient_funds")
        wallet = gw.store.create_wallet(
            kind="agent",
            owner=str(body.get("agent_id") or "agent"),
            credits=credits,
            sponsor_wallet_id=sponsor["wallet_id"],
        )
        return JSONResponse(wallet, status_code=201)

    async def api_key(request: Request):
        require_admin(request)
        body = await _body(request)
        wallet = gw.store.get_wallet(str(body.get("wallet_id")))
        if wallet is None:
            raise HTTPFailure(404, "wallet_not_found")
        days = body.get("expires_in_days", 30)
        if not isinstance(days, int) or not 1 <= days <= 365:
            raise HTTPFailure(422, "invalid_expires_in_days")
        expires_at = now() + timedelta(days=days)
        key_id, secret = gw.store.create_api_key(
            wallet_id=wallet["wallet_id"], name=str(body.get("key_name") or "key"), expires_at=expires_at
        )
        return JSONResponse(
            {"key_id": key_id, "api_key": secret, "wallet_id": wallet["wallet_id"], "expires_at": expires_at.isoformat()},
            status_code=201,
        )

    async def get_wallet(request: Request):
        wallet = require_wallet(caller(request), request.path_params["wallet_id"])
        return JSONResponse(wallet)

    async def get_ledger(request: Request):
        wallet = require_wallet(caller(request), request.path_params["wallet_id"])
        return JSONResponse({"wallet_id": wallet["wallet_id"], "entries": gw.store.ledger(wallet["wallet_id"])})

    # -- permits -----------------------------------------------------------

    async def create_permit(request: Request):
        who = caller(request)
        key = request.headers.get("idempotency-key", "").strip()
        if not key or len(key) > 128:
            raise HTTPFailure(422, "invalid_idempotency_key")
        body = await _body(request)
        issuer = str(body.get("issuer_wallet_id"))
        subject = str(body.get("subject_wallet_id"))
        require_wallet(who, issuer)
        subject_wallet = gw.store.get_wallet(subject)
        # A wallet can permit itself, and a sponsor can permit its own agents.
        if subject_wallet is None or (subject != issuer and subject_wallet["sponsor_wallet_id"] != issuer):
            raise HTTPFailure(403, "wallet_access_denied")
        request_hash = sha256_hex(body)

        with gw.store.transaction():
            existing = gw.store.find_permit_request(issuer, key)
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPFailure(409, "idempotency_key_reused")
                return JSONResponse(gw.store.get_permit(existing["permit_id"]), status_code=200)

            max_credits = _decimal(body.get("max_credits"), "max_credits", positive=True)
            try:
                expires_at = parse_time(str(body.get("expires_at")))
            except ValueError:
                raise HTTPFailure(422, "invalid_expires_at") from None
            if expires_at <= now():
                raise HTTPFailure(422, "permit_already_expired")
            scopes, tools = body.get("scopes") or [], body.get("allowed_tools") or []
            if not all(isinstance(s, str) for s in [*scopes, *tools]):
                raise HTTPFailure(422, "invalid_scopes")

            permit = {
                "permit_id": new_id("pmt"),
                "issuer_wallet_id": issuer,
                "subject_wallet_id": subject,
                "subject_key_id": body.get("subject_key_id"),
                "scopes": list(scopes),
                "allowed_tools": list(tools),
                "max_credits": str(max_credits),
                "expires_at": expires_at.isoformat(),
                "nonce": str(body.get("nonce") or new_id("nonce")),
                "status": "active",
                "issued_at": now().isoformat(),
                "revoked_at": None,
            }
            permit["signature"] = gw.signer.sign_permit(permit)
            permit["key_id"] = gw.signer.key_id
            gw.store.insert_permit(permit, key=key, request_hash=request_hash)
        return JSONResponse(gw.store.get_permit(permit["permit_id"]), status_code=201)

    # -- MCP ---------------------------------------------------------------

    async def mcp_messages(request: Request):
        who = caller(request)
        message = await _body(request)
        request_id = message.get("id")
        method = message.get("method")
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_list()}})
        if method != "tools/call":
            return JSONResponse(rpc_error(request_id, METHOD_NOT_FOUND, "method_not_found"))
        params = message.get("params")
        envelope = await gw.governor.call(
            request_id,
            params if isinstance(params, dict) else {},
            caller=who,
            header_key=request.headers.get("idempotency-key"),
        )
        return JSONResponse(envelope)

    def tool_list() -> list[dict]:
        return [tool.describe() for tool in gw.registry.all()]

    async def tools_json(request: Request):
        caller(request)
        return JSONResponse({"tools": tool_list()})

    # -- receipts and keys ---------------------------------------------------

    def load_receipt(request: Request, receipt_id: str) -> tuple[dict, dict]:
        who = caller(request)
        found = gw.store.get_receipt(receipt_id)
        if found is None or not (who["admin"] or who["wallet_id"] == found[0]["wallet_id"]):
            # Same answer for "missing" and "not yours", so ids can not be probed.
            raise HTTPFailure(404, "receipt_not_found")
        return found

    async def get_receipt(request: Request):
        return JSONResponse(load_receipt(request, request.path_params["receipt_id"])[0])

    async def portable_receipt(request: Request):
        return JSONResponse(load_receipt(request, request.path_params["receipt_id"])[1])

    async def verify_receipt(request: Request):
        from b2a_sdk.receipt_verifier import key_set_from_document, verify_bundle

        body = await _body(request)
        receipt, bundle = load_receipt(request, str(body.get("receipt_id")))
        result = verify_bundle(bundle, key_set_from_document(gw.signer.key_set_document()))
        return JSONResponse({"valid": result.ok, "reason": result.reason, "receipt": receipt})

    async def trust_keys(request: Request):
        return JSONResponse(gw.signer.key_set_document())

    async def health(request: Request):
        return JSONResponse({"status": "ok" if gw.store is not None else "stopped"})

    async def on_failure(request: Request, exc: HTTPFailure):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    routes = [
        Route("/v1/billing/wallets/sponsor", sponsor_wallet, methods=["POST"]),
        Route("/v1/billing/wallets/agent", agent_wallet, methods=["POST"]),
        Route("/v1/billing/wallets/{wallet_id}", get_wallet, methods=["GET"]),
        Route("/v1/billing/ledger/{wallet_id}", get_ledger, methods=["GET"]),
        Route("/v1/api-keys", api_key, methods=["POST"]),
        Route("/v1/permits", create_permit, methods=["POST"]),
        Route("/mcp/messages", mcp_messages, methods=["POST"]),
        Route("/mcp/tools.json", tools_json, methods=["GET"]),
        Route("/v1/receipts/verify", verify_receipt, methods=["POST"]),
        Route("/v1/receipts/{receipt_id}", get_receipt, methods=["GET"]),
        Route("/v1/receipts/{receipt_id}/portable", portable_receipt, methods=["GET"]),
        Route("/.well-known/trust-keys.json", trust_keys, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ]
    return Starlette(routes=routes, exception_handlers={HTTPFailure: on_failure})
