"""Peer identity: Ed25519 keypair and signed JSON envelopes.

Protects invariant 3: every message between peers is a signed JSON
envelope, and a peer id is the hash of the peer's public key (like a
torrent node id). An envelope is rejected unless its signature verifies
against the embedded public key AND the sender id embedded in the
payload equals the hash of that public key.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

log = logging.getLogger("agenttorrent.identity")

KEY_FILENAME = "ed25519.key"


class EnvelopeError(Exception):
    """Raised when a signed envelope fails validation."""


def canonical_json(payload: dict) -> bytes:
    """Deterministic byte serialization used for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def peer_id_from_pubkey(pubkey: bytes) -> str:
    """A peer id is the SHA-1 hex digest of the raw public key bytes."""
    return hashlib.sha1(pubkey).hexdigest()


class Identity:
    """A peer's Ed25519 keypair, persisted in its data directory."""

    def __init__(self, signing_key: SigningKey) -> None:
        self._signing_key = signing_key
        self.pubkey: bytes = bytes(signing_key.verify_key)
        self.peer_id: str = peer_id_from_pubkey(self.pubkey)

    @property
    def short_id(self) -> str:
        return self.peer_id[:8]

    @classmethod
    def load_or_create(cls, data_dir: Path) -> "Identity":
        """Load the keypair from ``data_dir``, generating it on first run."""
        data_dir.mkdir(parents=True, exist_ok=True)
        key_path = data_dir / KEY_FILENAME
        if key_path.exists():
            signing_key = SigningKey(key_path.read_bytes())
            log.info("loaded identity from %s", key_path)
        else:
            signing_key = SigningKey.generate()
            key_path.touch(mode=0o600)
            key_path.write_bytes(bytes(signing_key))
            os.chmod(key_path, 0o600)
            log.info("generated new identity at %s", key_path)
        return cls(signing_key)

    def seal(self, msg_type: str, body: dict) -> dict:
        """Wrap ``body`` in a signed envelope carrying ``msg_type``."""
        payload = {
            "type": msg_type,
            "sender": self.peer_id,
            "ts": time.time(),
            "body": body,
        }
        signature = self._signing_key.sign(canonical_json(payload)).signature
        return {
            "v": 1,
            "payload": payload,
            "pubkey": self.pubkey.hex(),
            "sig": signature.hex(),
        }

    @staticmethod
    def open(envelope: dict) -> tuple[str, dict]:
        """Verify an envelope; return ``(sender_peer_id, payload)``.

        Raises :class:`EnvelopeError` on any structural or cryptographic
        failure. Nothing from an unverified envelope may be processed.
        """
        try:
            payload = envelope["payload"]
            pubkey = bytes.fromhex(envelope["pubkey"])
            sig = bytes.fromhex(envelope["sig"])
            claimed_sender = payload["sender"]
            msg_type = payload["type"]
        except (KeyError, TypeError, ValueError) as exc:
            raise EnvelopeError(f"malformed envelope: {exc!r}") from exc
        if not isinstance(msg_type, str) or not isinstance(claimed_sender, str):
            raise EnvelopeError("malformed envelope: bad type/sender")
        if peer_id_from_pubkey(pubkey) != claimed_sender:
            raise EnvelopeError("sender id does not match public key hash")
        try:
            VerifyKey(pubkey).verify(canonical_json(payload), sig)
        except BadSignatureError as exc:
            raise EnvelopeError("bad signature") from exc
        return claimed_sender, payload
