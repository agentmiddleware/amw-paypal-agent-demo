"""Ed25519 signing for receipts and permits, and the published key set.

The receipt format is the one ``b2a_sdk.receipt_verifier.verify_bundle``
checks offline: a portable bundle whose ``signing_input`` is canonical JSON,
signed verbatim. Everything a verifier relies on (``kid``, ``alg``,
``canonicalization``, ``receipt_id``) sits inside the signed bytes, because the
envelope around them is not authenticated.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

CANONICALIZATION = "awi-canonical-json/1"
ALG = "Ed25519"


def canonical_json(payload: Any) -> str:
    """Sorted keys, no whitespace, ASCII only. Values are already strings."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class Signer:
    """One signing key. The private half never leaves this object."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey | None = None, *, issuer: str) -> None:
        self.key_id = key_id
        self.issuer = issuer
        self._key = private_key or Ed25519PrivateKey.generate()
        self.public_raw = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def __repr__(self) -> str:
        return f"Signer(key_id={self.key_id!r})"

    def _sign(self, text: str) -> str:
        return base64.b64encode(self._key.sign(text.encode("utf-8"))).decode("ascii")

    def sign_receipt(self, fields: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(receipt, portable_bundle)`` for one receipt's fields.

        ``fields`` must hold strings or None only, so canonical JSON needs no
        number formatting rules.
        """
        payload = dict(fields)
        payload.update(kid=self.key_id, alg=ALG, canonicalization=CANONICALIZATION, issuer=self.issuer)
        payload["payload_hash"] = sha256_hex(payload)
        signing_input = canonical_json(payload)
        signature = self._sign(signing_input)
        receipt = dict(fields, signature=signature, signature_key_id=self.key_id)
        bundle = {
            "receipt_id": fields["receipt_id"],
            "issuer": self.issuer,
            "kid": self.key_id,
            "alg": ALG,
            "canonicalization": CANONICALIZATION,
            "signing_input": signing_input,
            "signature": signature,
        }
        return receipt, bundle

    def sign_permit(self, fields: dict[str, Any]) -> str:
        return self._sign(canonical_json(dict(fields, key_id=self.key_id)))

    def key_set_document(self) -> dict[str, Any]:
        """The ``/.well-known/trust-keys.json`` document."""
        x = base64.urlsafe_b64encode(self.public_raw).rstrip(b"=").decode("ascii")
        return {
            "issuer": self.issuer,
            "keys": [
                {
                    "kid": self.key_id,
                    "alg": ALG,
                    "status": "active",
                    "public_key_b64": base64.b64encode(self.public_raw).decode("ascii"),
                    "jwk": {"kty": "OKP", "crv": "Ed25519", "x": x, "kid": self.key_id},
                }
            ],
        }
