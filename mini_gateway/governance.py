"""The governed tool call: permit check, charge, run, receipt.

Order of operations for ``tools/call``:

1. The idempotency key is looked up first. For a finished key:
   a retry with the same key returns the original receipt instead of dispatching again.
   A key whose first attempt is still running answers ``idempotency_in_progress``.
   A key whose first attempt never reported back answers
   ``delivery_uncertain`` with a signed receipt saying so.
2. The permit is checked: right wallet, not expired, tool and scope allowed,
   and the price fits in what is left of ``max_credits``. A denial happens
   here, before the tool runs and before any charge.
3. The wallet is debited and the key is marked in flight, in the same
   transaction as the checks above.
4. The tool runs. If it raises, the charge is refunded. Either way a signed
   receipt is written and the response is stored under the key.

If the tool is cancelled midway (or the process dies), nobody knows whether
its side effect happened. The charge is kept, the key is marked
``delivery_uncertain``, and the case is left for a human.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .signing import Signer, sha256_hex
from .store import COMPLETED, DELIVERY_UNCERTAIN, IN_FLIGHT, Store, new_id, now
from .tools import LocalTool, ToolRegistry

MAX_KEY_LENGTH = 128

# JSON-RPC error codes. The SDK maps -32003 to AuthorizationError; everything
# else it maps by message (permit_*, insufficient_funds, idempotency_*).
INVALID_PARAMS = -32602
METHOD_NOT_FOUND = -32601
FORBIDDEN = -32003
DENIED = -32001
CONFLICT = -32009
UNCERTAIN = -32010


class Refusal(Exception):
    """A call refused before the tool ran. Nothing was charged."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def rpc_error(request_id: Any, code: int, message: str, data: dict | None = None) -> dict:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Governor:
    def __init__(self, store: Store, registry: ToolRegistry, signer: Signer) -> None:
        self.store = store
        self.registry = registry
        self.signer = signer

    # -- receipts ----------------------------------------------------------

    def write_receipt(self, op: dict, *, outcome: str, charged: Decimal, key_id: str | None,
                      response: Any = None, ledger_entry_id: str | None = None) -> dict:
        fields = {
            "receipt_id": new_id("rcpt"),
            "idempotency_record_id": f"{op['wallet_id']}:{op['idempotency_key']}",
            "permit_id": op["permit_id"],
            "wallet_id": op["wallet_id"],
            "key_id": key_id,
            "tool": op["tool"],
            "request_hash": op["request_hash"],
            "response_hash": sha256_hex(response) if response is not None else None,
            "ledger_entry_id": ledger_entry_id or op["ledger_entry_id"],
            "dispatch_attempt_id": None,
            "credits_authorized": op["credits"],
            "credits_charged": str(charged),
            "outcome": outcome,
            "audit_event_id": None,
            "created_at": now().isoformat(),
        }
        receipt, bundle = self.signer.sign_receipt(fields)
        self.store.save_receipt(receipt, bundle)
        return receipt

    # -- the call ----------------------------------------------------------

    async def call(self, request_id: Any, params: dict, *, caller: dict, header_key: str | None) -> dict:
        try:
            replay, admitted = self._admit(params, caller=caller, header_key=header_key)
        except Refusal as refusal:
            return rpc_error(request_id, refusal.code, refusal.message)
        if replay is not None:
            return dict(replay, id=request_id)
        tool, op, arguments = admitted
        return await self._run(request_id, tool, op, arguments, caller)

    def _admit(self, params: dict, *, caller: dict, header_key: str | None):
        """Steps 1 to 3. Synchronous on purpose: no other call can interleave."""
        name = params.get("name")
        arguments = params.get("arguments") or {}
        context = params.get("mcpContext") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict) or not isinstance(context, dict):
            raise Refusal(INVALID_PARAMS, "invalid_params")

        key = (header_key or context.get("idempotency_key") or "").strip()
        if not key or len(key) > MAX_KEY_LENGTH:
            raise Refusal(INVALID_PARAMS, "invalid_idempotency_key")
        if header_key and context.get("idempotency_key") and context["idempotency_key"].strip() != key:
            raise Refusal(INVALID_PARAMS, "invalid_idempotency_key")

        wallet_id = context.get("wallet_id")
        if not isinstance(wallet_id, str) or self.store.get_wallet(wallet_id) is None:
            raise Refusal(FORBIDDEN, "wallet_access_denied")
        if not caller["admin"] and caller["wallet_id"] != wallet_id:
            raise Refusal(FORBIDDEN, "wallet_access_denied")

        tool = self.registry.get(name)
        if tool is None:
            raise Refusal(INVALID_PARAMS, "unknown_tool")
        if tool.idempotency_key_arg:
            arguments = dict(arguments, **{tool.idempotency_key_arg: key})
        try:
            inspect.signature(tool.func).bind(**arguments)
        except TypeError:
            raise Refusal(INVALID_PARAMS, "invalid_arguments") from None

        permit_id = context.get("permit_id")
        request_hash = sha256_hex({"tool": name, "arguments": arguments, "wallet_id": wallet_id})

        with self.store.transaction():
            op = self.store.get_operation(wallet_id, key)
            if op is not None:
                if op["request_hash"] != request_hash or op["permit_id"] != permit_id:
                    raise Refusal(CONFLICT, "idempotency_key_reused")
                if op["state"] == IN_FLIGHT:
                    raise Refusal(CONFLICT, "idempotency_in_progress")
                return op["envelope"], None  # completed or delivery_uncertain

            price = tool.credits_per_unit
            self._check_permit(permit_id, wallet_id=wallet_id, tool=name, price=price)
            wallet = self.store.get_wallet(wallet_id)
            if Decimal(wallet["balance"]) < price:
                raise Refusal(DENIED, "insufficient_funds")

            entry_id = self.store.move(wallet_id, -price, "charge", f"charge: {name}", key)
            self.store.add_permit_spend(permit_id, price)
            op = {
                "wallet_id": wallet_id,
                "idempotency_key": key,
                "tool": name,
                "request_hash": request_hash,
                "permit_id": permit_id,
                "credits": str(price),
                "ledger_entry_id": entry_id,
            }
            self.store.insert_operation(**op)
        return None, (tool, op, arguments)

    def _check_permit(self, permit_id: Any, *, wallet_id: str, tool: str, price: Decimal) -> None:
        permit = self.store.get_permit(permit_id) if isinstance(permit_id, str) else None
        if permit is None:
            raise Refusal(DENIED, "permit_not_found")
        if permit["subject_wallet_id"] != wallet_id:
            raise Refusal(DENIED, "permit_wallet_mismatch")
        if permit["status"] != "active":
            raise Refusal(DENIED, "permit_revoked")
        if parse_time(permit["expires_at"]) <= now():
            raise Refusal(DENIED, "permit_expired")
        if permit["allowed_tools"] and tool not in permit["allowed_tools"]:
            raise Refusal(DENIED, "permit_tool_not_allowed")
        if permit["scopes"] and f"tool:{tool}:invoke" not in permit["scopes"]:
            raise Refusal(DENIED, "permit_scope_missing")
        if Decimal(permit["spent_credits"]) + price > Decimal(permit["max_credits"]):
            raise Refusal(DENIED, "permit_budget_exceeded")

    async def _run(self, request_id: Any, tool: LocalTool, op: dict, arguments: dict, caller: dict) -> dict:
        price = Decimal(op["credits"])
        try:
            result = await tool.func(**arguments)
        except Exception as exc:
            with self.store.transaction():
                refund_id = self.store.move(
                    op["wallet_id"], price, "refund", f"refund of {op['ledger_entry_id']}", op["idempotency_key"]
                )
                self.store.add_permit_spend(op["permit_id"], -price)
                receipt = self.write_receipt(
                    op, outcome="tool_error", charged=Decimal(0), key_id=caller["key_id"], ledger_entry_id=refund_id
                )
                envelope = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "isError": True,
                        "error": "tool_failed",
                        "content": [{"type": "text", "text": f"tool raised {type(exc).__name__}; credits refunded"}],
                        "receipt": receipt,
                    },
                }
                self.store.finish_operation(op["wallet_id"], op["idempotency_key"], COMPLETED, envelope)
            return envelope
        except BaseException:
            # Cancelled mid call: the side effect may or may not have happened.
            self.mark_uncertain(op, key_id=caller["key_id"])
            raise

        structured = result if isinstance(result, dict) else None
        text = json.dumps(result, default=str, sort_keys=True)
        with self.store.transaction():
            receipt = self.write_receipt(op, outcome="success", charged=price, key_id=caller["key_id"], response=text)
            envelope = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "structuredContent": structured,
                    "isError": False,
                    "receipt": receipt,
                },
            }
            self.store.finish_operation(op["wallet_id"], op["idempotency_key"], COMPLETED, envelope)
        return envelope

    def mark_uncertain(self, op: dict, *, key_id: str | None = None) -> None:
        """Keep the charge, sign a ``delivery_uncertain`` receipt, store it."""
        with self.store.transaction():
            receipt = self.write_receipt(op, outcome=DELIVERY_UNCERTAIN, charged=Decimal(op["credits"]), key_id=key_id)
            envelope = rpc_error(None, UNCERTAIN, DELIVERY_UNCERTAIN, {"receipt": receipt})
            self.store.finish_operation(op["wallet_id"], op["idempotency_key"], DELIVERY_UNCERTAIN, envelope)

    def recover(self) -> int:
        """On start: keys left in flight by a dead process become uncertain."""
        stale = self.store.in_flight_operations()
        for op in stale:
            self.mark_uncertain(op)
        return len(stale)
