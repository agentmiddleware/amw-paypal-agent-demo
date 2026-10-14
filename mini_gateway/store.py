"""SQLite storage for wallets, keys, the ledger, permits, operations and receipts.

One connection, used from one event loop. Calls are synchronous and short, so
a governed call can read, check and reserve inside one transaction with no
``await`` in between: two retries on the same loop can not both get through.

Money is stored as decimal strings, never floats.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    wallet_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner TEXT NOT NULL,
    sponsor_wallet_id TEXT,
    balance TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT PRIMARY KEY,
    key_hash TEXT NOT NULL UNIQUE,
    wallet_id TEXT NOT NULL,
    name TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT NOT NULL UNIQUE,
    wallet_id TEXT NOT NULL,
    action TEXT NOT NULL,
    amount TEXT NOT NULL,
    balance_after TEXT NOT NULL,
    description TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permits (
    permit_id TEXT PRIMARY KEY,
    issuer_wallet_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    body TEXT NOT NULL,
    spent TEXT NOT NULL,
    UNIQUE (issuer_wallet_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS operations (
    wallet_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    tool TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    permit_id TEXT NOT NULL,
    credits TEXT NOT NULL,
    ledger_entry_id TEXT NOT NULL,
    state TEXT NOT NULL,
    envelope TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (wallet_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    wallet_id TEXT NOT NULL,
    receipt TEXT NOT NULL,
    bundle TEXT NOT NULL
);
"""

IN_FLIGHT = "in_flight"
COMPLETED = "completed"
DELIVERY_UNCERTAIN = "delivery_uncertain"


def now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._depth = 0

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Nested calls join the outer transaction."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self._conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")
        finally:
            self._depth = 0

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self._conn.execute(sql, args).fetchone()

    # -- wallets and ledger ------------------------------------------------

    def create_wallet(self, *, kind: str, owner: str, credits: Decimal, sponsor_wallet_id: str | None = None) -> dict:
        wallet_id = new_id("wal")
        with self.transaction():
            self._conn.execute(
                "INSERT INTO wallets VALUES (?, ?, ?, ?, '0', ?)",
                (wallet_id, kind, owner, sponsor_wallet_id, now().isoformat()),
            )
            if sponsor_wallet_id is not None:
                self.move(sponsor_wallet_id, -credits, "provision", f"funded agent wallet {wallet_id}")
            self.move(wallet_id, credits, "provision", f"initial credits ({kind})")
        return self.get_wallet(wallet_id)

    def get_wallet(self, wallet_id: str) -> dict | None:
        row = self._one("SELECT * FROM wallets WHERE wallet_id = ?", wallet_id)
        return dict(row) if row else None

    def move(self, wallet_id: str, amount: Decimal, action: str, description: str, key: str | None = None) -> str:
        """Add ``amount`` (negative to debit) and write one ledger entry."""
        with self.transaction():
            row = self._one("SELECT balance FROM wallets WHERE wallet_id = ?", wallet_id)
            balance = Decimal(row["balance"]) + amount
            if balance < 0:
                raise ValueError("balance would go negative")
            entry_id = new_id("led")
            self._conn.execute("UPDATE wallets SET balance = ? WHERE wallet_id = ?", (str(balance), wallet_id))
            self._conn.execute(
                "INSERT INTO ledger (entry_id, wallet_id, action, amount, balance_after, description,"
                " idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry_id, wallet_id, action, str(amount), str(balance), description, key, now().isoformat()),
            )
        return entry_id

    def ledger(self, wallet_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT entry_id, wallet_id, action, amount, balance_after, description, idempotency_key,"
            " created_at FROM ledger WHERE wallet_id = ? ORDER BY seq",
            (wallet_id,),
        )
        return [dict(r) for r in rows]

    # -- api keys ----------------------------------------------------------

    def create_api_key(self, *, wallet_id: str, name: str, expires_at: datetime) -> tuple[str, str]:
        key_id = new_id("key")
        secret = "mgw_" + secrets.token_urlsafe(32)
        self._conn.execute(
            "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?)",
            (key_id, hash_secret(secret), wallet_id, name, expires_at.isoformat()),
        )
        return key_id, secret

    def resolve_api_key(self, secret: str) -> dict | None:
        row = self._one("SELECT * FROM api_keys WHERE key_hash = ?", hash_secret(secret))
        if row is None or datetime.fromisoformat(row["expires_at"]) <= now():
            return None
        return dict(row)

    # -- permits -----------------------------------------------------------

    def find_permit_request(self, issuer_wallet_id: str, key: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM permits WHERE issuer_wallet_id = ? AND idempotency_key = ?", issuer_wallet_id, key
        )

    def insert_permit(self, body: dict, *, key: str, request_hash: str) -> None:
        self._conn.execute(
            "INSERT INTO permits VALUES (?, ?, ?, ?, ?, '0')",
            (body["permit_id"], body["issuer_wallet_id"], key, request_hash, json.dumps(body)),
        )

    def get_permit(self, permit_id: str) -> dict | None:
        row = self._one("SELECT body, spent FROM permits WHERE permit_id = ?", permit_id)
        if row is None:
            return None
        return dict(json.loads(row["body"]), spent_credits=row["spent"])

    def add_permit_spend(self, permit_id: str, amount: Decimal) -> None:
        row = self._one("SELECT spent FROM permits WHERE permit_id = ?", permit_id)
        self._conn.execute(
            "UPDATE permits SET spent = ? WHERE permit_id = ?", (str(Decimal(row["spent"]) + amount), permit_id)
        )

    # -- operations (one row per wallet and idempotency key) ---------------

    def get_operation(self, wallet_id: str, key: str) -> dict | None:
        row = self._one("SELECT * FROM operations WHERE wallet_id = ? AND idempotency_key = ?", wallet_id, key)
        if row is None:
            return None
        op = dict(row)
        op["envelope"] = json.loads(op["envelope"]) if op["envelope"] else None
        return op

    def insert_operation(self, **fields: Any) -> None:
        fields.setdefault("state", IN_FLIGHT)
        fields.setdefault("envelope", None)
        fields.setdefault("created_at", now().isoformat())
        columns = ", ".join(fields)
        self._conn.execute(
            f"INSERT INTO operations ({columns}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )

    def finish_operation(self, wallet_id: str, key: str, state: str, envelope: dict) -> None:
        self._conn.execute(
            "UPDATE operations SET state = ?, envelope = ? WHERE wallet_id = ? AND idempotency_key = ?",
            (state, json.dumps(envelope), wallet_id, key),
        )

    def in_flight_operations(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM operations WHERE state = ?", (IN_FLIGHT,))
        return [dict(r) for r in rows]

    # -- receipts ----------------------------------------------------------

    def save_receipt(self, receipt: dict, bundle: dict) -> None:
        self._conn.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?)",
            (receipt["receipt_id"], receipt["wallet_id"], json.dumps(receipt), json.dumps(bundle)),
        )

    def get_receipt(self, receipt_id: str) -> tuple[dict, dict] | None:
        row = self._one("SELECT receipt, bundle FROM receipts WHERE receipt_id = ?", receipt_id)
        if row is None:
            return None
        return json.loads(row["receipt"]), json.loads(row["bundle"])
