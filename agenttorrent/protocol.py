"""TCP JSON-line protocol for direct peer messages.

Protects invariant 3: every message on the wire is a signed JSON
envelope, one per line, and is verified (signature + peer-id-matches-
pubkey-hash) before any field of it is processed.

Peer message types: HANDSHAKE, TASK_OFFER, TASK_ACCEPT, TASK_REJECT,
TASK_RESULT. Two local-only types, CONTROL and CONTROL_RESULT, carry
CLI commands to a running peer; they use the same signed envelope and
are only honoured from the peer's own key on the loopback interface.

Every message sent and received is logged so swarm behaviour is
observable.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import BinaryIO

from .identity import EnvelopeError, Identity

HANDSHAKE = "HANDSHAKE"
TASK_OFFER = "TASK_OFFER"
TASK_ACCEPT = "TASK_ACCEPT"
TASK_REJECT = "TASK_REJECT"
TASK_RESULT = "TASK_RESULT"
CONTROL = "CONTROL"
CONTROL_RESULT = "CONTROL_RESULT"

_MAX_LINE_BYTES = 4 * 1024 * 1024


class ProtocolError(Exception):
    """Raised on connection loss, timeouts, or invalid messages."""


class Connection:
    """A framed, signed-envelope connection to one remote peer."""

    def __init__(self, sock: socket.socket, identity: Identity, log: logging.Logger) -> None:
        self.sock = sock
        self.identity = identity
        self.log = log
        self._reader: BinaryIO = sock.makefile("rb")
        self.remote_peer_id: str | None = None
        peername = sock.getpeername()
        self.remote_addr: str = f"{peername[0]}:{peername[1]}"

    # -- framing -----------------------------------------------------------

    def send(self, msg_type: str, body: dict) -> None:
        envelope = self.identity.seal(msg_type, body)
        line = json.dumps(envelope).encode() + b"\n"
        try:
            self.sock.sendall(line)
        except OSError as exc:
            raise ProtocolError(f"send failed: {exc}") from exc
        self.log.info(
            "SEND %s -> %s (%s) %s",
            msg_type,
            (self.remote_peer_id or "?")[:8],
            self.remote_addr,
            _summarize(body),
        )

    def recv(self, expected: tuple[str, ...] | None = None, timeout: float | None = None) -> tuple[str, dict]:
        """Read, verify, and return one message as ``(sender_id, payload)``.

        Raises :class:`ProtocolError` on EOF, timeout, verification
        failure, or (when ``expected`` is given) an unexpected type.
        """
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            line = self._reader.readline(_MAX_LINE_BYTES)
        except socket.timeout as exc:
            raise ProtocolError("timed out waiting for message") from exc
        except OSError as exc:
            raise ProtocolError(f"connection error: {exc}") from exc
        if not line:
            raise ProtocolError("connection closed by remote peer")
        try:
            sender, payload = Identity.open(json.loads(line.decode()))
        except (EnvelopeError, ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"rejected unverifiable message: {exc}") from exc
        msg_type = payload["type"]
        self.log.info(
            "RECV %s <- %s (%s) %s",
            msg_type,
            sender[:8],
            self.remote_addr,
            _summarize(payload.get("body", {})),
        )
        if self.remote_peer_id is not None and sender != self.remote_peer_id:
            raise ProtocolError("sender id changed mid-connection")
        if expected is not None and msg_type not in expected:
            raise ProtocolError(f"expected {expected}, got {msg_type}")
        return sender, payload

    def close(self) -> None:
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # -- handshake ---------------------------------------------------------

    def handshake_outbound(self, timeout: float = 10.0) -> str:
        """Client side: send HANDSHAKE, expect HANDSHAKE back."""
        self.send(HANDSHAKE, {})
        sender, _ = self.recv(expected=(HANDSHAKE,), timeout=timeout)
        self.remote_peer_id = sender
        return sender

    def handshake_inbound(self, timeout: float = 10.0) -> str:
        """Server side: expect HANDSHAKE, reply HANDSHAKE."""
        sender, _ = self.recv(expected=(HANDSHAKE,), timeout=timeout)
        self.remote_peer_id = sender
        self.send(HANDSHAKE, {})
        return sender


def connect(host: str, port: int, identity: Identity, log: logging.Logger, timeout: float = 10.0) -> Connection:
    """Open a TCP connection to a peer and complete the mutual handshake."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise ProtocolError(f"cannot connect to {host}:{port}: {exc}") from exc
    conn = Connection(sock, identity, log)
    conn.handshake_outbound(timeout=timeout)
    return conn


def _summarize(body: dict) -> str:
    """Compact one-line description of a message body for the log."""
    parts: list[str] = []
    job = body.get("job")
    if isinstance(job, dict) and "job_id" in job:
        parts.append(f"job_id={str(job.get('job_id'))[:8]}")
    for key in ("job_id", "reason", "command", "status"):
        if key in body:
            parts.append(f"{key}={str(body[key])[:40]}")
    return " ".join(parts) if parts else f"keys={sorted(body)}" if body else ""
