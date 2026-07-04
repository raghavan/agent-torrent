"""UDP broadcast discovery and the gossip-built peer table.

Protects invariant 2 (no central coordinator: peers find each other via
UDP broadcast on the local network, plus an optional ``bootstrap_peers``
list for peers across the internet) and invariant 5 (the peer table is a
pure cache of received beacons — throw it away, restart, and it rebuilds
itself within one beacon interval).

Every beacon is a signed envelope (invariant 3); unverifiable beacons
are dropped before their contents are looked at.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass, field

from .identity import EnvelopeError, Identity
from .manifest import ManifestError, validate_manifest

BEACON_INTERVAL_SECONDS = 5.0
PEER_TTL_SECONDS = 30.0
_RECV_BUFSIZE = 65536

# 255.255.255.255 reaches the local network; 127.255.255.255 is the
# loopback broadcast so multiple peers on one machine hear each other
# even when the LAN broadcast doesn't loop back.
_BROADCAST_TARGETS = ("255.255.255.255", "127.255.255.255")

MSG_BEACON = "BEACON"


@dataclass
class PeerInfo:
    """One row of the gossip-derived peer table."""

    peer_id: str
    host: str
    tcp_port: int
    manifest: dict
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "peer_id": self.peer_id,
            "host": self.host,
            "tcp_port": self.tcp_port,
            "manifest": self.manifest,
            "last_seen": self.last_seen,
        }


class Discovery:
    """Beacon sender + listener maintaining the peer table."""

    def __init__(
        self,
        identity: Identity,
        tcp_port: int,
        manifest: dict,
        discovery_port: int,
        bootstrap_peers: list[str] | None = None,
    ) -> None:
        self.identity = identity
        self.tcp_port = tcp_port
        self.manifest = manifest
        self.discovery_port = discovery_port
        self.bootstrap_peers = list(bootstrap_peers or [])
        self.log = logging.getLogger(f"agenttorrent.discovery.{identity.short_id}")
        self._table: dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._recv_sock: socket.socket | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        recv.bind(("", self.discovery_port))
        recv.settimeout(1.0)
        self._recv_sock = recv
        for target in (self._send_loop, self._recv_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)
        self.log.info(
            "discovery started on udp/%d (bootstrap: %s)",
            self.discovery_port,
            self.bootstrap_peers or "none",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._recv_sock is not None:
            self._recv_sock.close()

    # -- peer table --------------------------------------------------------

    def peers(self) -> list[PeerInfo]:
        """Live peers, with entries older than ``PEER_TTL_SECONDS`` expired."""
        cutoff = time.time() - PEER_TTL_SECONDS
        with self._lock:
            expired = [pid for pid, p in self._table.items() if p.last_seen < cutoff]
            for pid in expired:
                gone = self._table.pop(pid)
                self.log.info("peer %s expired (last seen %.1fs ago)", gone.peer_id[:8], time.time() - gone.last_seen)
            return sorted(self._table.values(), key=lambda p: p.peer_id)

    def get_peer(self, peer_id: str) -> PeerInfo | None:
        for p in self.peers():
            if p.peer_id == peer_id:
                return p
        return None

    # -- beacon sending ----------------------------------------------------

    def _beacon_bytes(self) -> bytes:
        envelope = self.identity.seal(
            MSG_BEACON,
            {"tcp_port": self.tcp_port, "manifest": self.manifest},
        )
        return json.dumps(envelope).encode()

    def _send_loop(self) -> None:
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while not self._stop.is_set():
            data = self._beacon_bytes()
            targets: list[tuple[str, int]] = [(h, self.discovery_port) for h in _BROADCAST_TARGETS]
            for entry in self.bootstrap_peers:
                host, _, port = entry.rpartition(":")
                if host:
                    targets.append((host, int(port)))
            for addr in targets:
                try:
                    send.sendto(data, addr)
                    self.log.info("SEND %s -> udp://%s:%d", MSG_BEACON, addr[0], addr[1])
                except OSError as exc:
                    self.log.debug("beacon to %s failed: %s", addr, exc)
            self._stop.wait(BEACON_INTERVAL_SECONDS)
        send.close()

    # -- beacon receiving ----------------------------------------------------

    def _recv_loop(self) -> None:
        assert self._recv_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._recv_sock.recvfrom(_RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                break  # socket closed during stop()
            self._handle_beacon(data, addr)

    def _handle_beacon(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            sender, payload = Identity.open(json.loads(data.decode()))
        except (EnvelopeError, ValueError, UnicodeDecodeError) as exc:
            self.log.warning("dropped unverifiable beacon from %s: %s", addr[0], exc)
            return
        if sender == self.identity.peer_id:
            return  # our own broadcast echoed back
        if payload["type"] != MSG_BEACON:
            self.log.warning("dropped non-beacon UDP message type=%s from %s", payload["type"], sender[:8])
            return
        body = payload.get("body", {})
        tcp_port = body.get("tcp_port")
        if not isinstance(tcp_port, int) or not (0 < tcp_port < 65536):
            self.log.warning("dropped beacon from %s: bad tcp_port", sender[:8])
            return
        try:
            manifest = validate_manifest(body.get("manifest"))
        except ManifestError as exc:
            self.log.warning("dropped beacon from %s: bad manifest: %s", sender[:8], exc)
            return
        if manifest["peer_id"] != sender:
            self.log.warning("dropped beacon from %s: manifest peer_id mismatch", sender[:8])
            return
        self.log.info("RECV %s <- %s (udp://%s:%d)", MSG_BEACON, sender[:8], addr[0], addr[1])
        with self._lock:
            is_new = sender not in self._table
            self._table[sender] = PeerInfo(
                peer_id=sender,
                host=addr[0],
                tcp_port=tcp_port,
                manifest=manifest,
                last_seen=time.time(),
            )
        if is_new:
            self.log.info(
                "new peer %s at %s:%d harnesses=%s accepts_tasks=%s",
                sender[:8],
                addr[0],
                tcp_port,
                sorted(manifest["harnesses"]) or "none (simulated)",
                manifest["accepts_tasks"],
            )
