"""The AgentTorrent peer: one process, requester and worker at once.

Protects invariant 1: there is no server role. This class runs the TCP
listener (worker side + local control), the discovery beacon, and the
delegation client (requester side) in a single process, and every peer
in the swarm runs exactly this code.

Also upholds invariant 5: apart from its keypair and ledger (economic
state), everything the peer knows about the swarm lives in the
discovery peer table, which rebuilds itself from beacons after a
restart.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import discovery as discovery_mod
from . import executor, protocol
from .identity import Identity
from .job import JobError, new_job, validate_job
from .ledger import Ledger, LedgerError
from .manifest import build_manifest, detect_harnesses

RUNTIME_FILENAME = "runtime.json"
CONFIG_FILENAME = "config.json"

_OFFER_REPLY_TIMEOUT = 15.0
_RESULT_GRACE_SECONDS = 15.0


@dataclass
class PeerConfig:
    """Static per-peer configuration, persisted in the data directory."""

    tcp_port: int = 0  # 0 = pick a free port
    discovery_port: int = 47474
    bootstrap_peers: list[str] = field(default_factory=list)
    max_runtime_seconds: int = 300
    accepts_tasks: bool = True
    force_simulate: bool = False
    simulate_delay: float = executor.DEFAULT_SIMULATE_DELAY_SECONDS

    @classmethod
    def load(cls, data_dir: Path, overrides: dict | None = None) -> "PeerConfig":
        path = data_dir / CONFIG_FILENAME
        values: dict = {}
        if path.exists():
            values.update(json.loads(path.read_text()))
        for key, value in (overrides or {}).items():
            if value is not None:
                values[key] = value
        known = {f for f in cls.__dataclass_fields__}
        config = cls(**{k: v for k, v in values.items() if k in known})
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config.__dict__, indent=2))
        return config


class Peer:
    """A single AgentTorrent peer process."""

    def __init__(self, data_dir: Path, config: PeerConfig) -> None:
        self.data_dir = data_dir
        self.config = config
        self.identity = Identity.load_or_create(data_dir)
        self.log = logging.getLogger(f"agenttorrent.peer.{self.identity.short_id}")
        self.ledger = Ledger(data_dir / "ledger.json")
        self.harnesses = detect_harnesses()
        self.manifest = build_manifest(
            peer_id=self.identity.peer_id,
            harnesses=self.harnesses,
            max_runtime_seconds=config.max_runtime_seconds,
            accepts_tasks=config.accepts_tasks,
        )
        self._server_sock: socket.socket | None = None
        self._stop = threading.Event()
        self.tcp_port: int = config.tcp_port
        self.discovery: discovery_mod.Discovery | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("", self.config.tcp_port))
        server.listen(16)
        server.settimeout(1.0)
        self._server_sock = server
        self.tcp_port = server.getsockname()[1]

        self.discovery = discovery_mod.Discovery(
            identity=self.identity,
            tcp_port=self.tcp_port,
            manifest=self.manifest,
            discovery_port=self.config.discovery_port,
            bootstrap_peers=self.config.bootstrap_peers,
        )
        self.discovery.start()
        threading.Thread(target=self._accept_loop, daemon=True, name="accept-loop").start()

        runtime = {"peer_id": self.identity.peer_id, "tcp_port": self.tcp_port, "pid": os.getpid()}
        (self.data_dir / RUNTIME_FILENAME).write_text(json.dumps(runtime, indent=2))
        self.log.info(
            "peer %s up: tcp/%d, discovery udp/%d, balance %d, harnesses %s",
            self.identity.short_id,
            self.tcp_port,
            self.config.discovery_port,
            self.ledger.balance(),
            sorted(self.harnesses) or "none (will simulate)",
        )

    def stop(self) -> None:
        self._stop.set()
        if self.discovery is not None:
            self.discovery.stop()
        if self._server_sock is not None:
            self._server_sock.close()
        try:
            (self.data_dir / RUNTIME_FILENAME).unlink(missing_ok=True)
        except OSError:
            pass

    def run_forever(self) -> None:
        self.start()
        try:
            self._stop.wait()
        except KeyboardInterrupt:
            self.log.info("interrupted; shutting down")
        finally:
            self.stop()

    # -- worker side: inbound connections --------------------------------------

    def _accept_loop(self) -> None:
        assert self._server_sock is not None
        while not self._stop.is_set():
            try:
                sock, addr = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._serve_connection,
                args=(sock, addr),
                daemon=True,
                name=f"conn-{addr[0]}:{addr[1]}",
            ).start()

    def _serve_connection(self, sock: socket.socket, addr: tuple[str, int]) -> None:
        conn = protocol.Connection(sock, self.identity, self.log)
        try:
            remote = conn.handshake_inbound()
            _, payload = conn.recv(
                expected=(protocol.TASK_OFFER, protocol.CONTROL), timeout=30.0
            )
            if payload["type"] == protocol.CONTROL:
                self._handle_control(conn, remote, addr, payload)
            else:
                self._handle_task_offer(conn, remote, payload)
        except protocol.ProtocolError as exc:
            self.log.warning("connection from %s ended: %s", addr[0], exc)
        finally:
            conn.close()

    def _handle_task_offer(self, conn: protocol.Connection, requester: str, payload: dict) -> None:
        """Worker role: validate, accept, execute in sandbox, return result."""
        job = payload.get("body", {}).get("job")
        if requester == self.identity.peer_id:
            conn.send(protocol.TASK_REJECT, {"reason": "cannot delegate to self"})
            return
        try:
            validate_job(job)
        except JobError as exc:
            conn.send(protocol.TASK_REJECT, {"reason": f"invalid job manifest: {exc}"})
            return
        if not self.config.accepts_tasks:
            conn.send(protocol.TASK_REJECT, {"job_id": job["job_id"], "reason": "peer is not accepting tasks"})
            return
        required = job["required_harness"]
        if required != "any" and required not in self.harnesses and not self.config.force_simulate:
            conn.send(
                protocol.TASK_REJECT,
                {"job_id": job["job_id"], "reason": f"harness {required!r} not available on this peer"},
            )
            return
        if job["max_runtime_seconds"] > self.config.max_runtime_seconds:
            conn.send(
                protocol.TASK_REJECT,
                {
                    "job_id": job["job_id"],
                    "reason": f"job runtime {job['max_runtime_seconds']}s exceeds this peer's limit "
                    f"of {self.config.max_runtime_seconds}s",
                },
            )
            return

        conn.send(protocol.TASK_ACCEPT, {"job_id": job["job_id"]})
        result = executor.execute_job(
            job,
            self.harnesses,
            force_simulate=self.config.force_simulate,
            simulate_delay=self.config.simulate_delay,
            log=self.log,
        )
        conn.send(protocol.TASK_RESULT, {"job_id": job["job_id"], "result": result})
        if result["status"] == "ok":
            try:
                self.ledger.credit_for_work(job["job_id"], requester)
            except LedgerError as exc:
                self.log.warning("ledger credit failed for job %s: %s", job["job_id"][:8], exc)

    # -- requester side: delegation ---------------------------------------------

    def delegate(
        self,
        task_text: str,
        required_harness: str,
        max_tokens: int,
        max_runtime_seconds: int,
        expected_output: str,
    ) -> dict:
        """Offer a job to a suitable peer and wait for the result.

        On any failure after escrow (reject, timeout, dead worker, bad
        result) the escrowed credit is refunded and a failure dict is
        returned; this method never raises for swarm-side problems.
        """
        if not self.ledger.can_offer():
            return {"status": "failed", "error": "ledger balance is zero: refusing to send TASK_OFFER"}
        try:
            job = new_job(task_text, required_harness, max_tokens, max_runtime_seconds, expected_output)
        except JobError as exc:
            return {"status": "failed", "error": f"invalid job: {exc}"}

        assert self.discovery is not None, "peer not started"
        candidates = [
            p
            for p in self.discovery.peers()
            if p.manifest["accepts_tasks"]
            and (required_harness == "any" or required_harness in p.manifest["harnesses"])
        ]
        if not candidates:
            return {
                "status": "failed",
                "error": f"no peer in the swarm accepts tasks with harness {required_harness!r}",
            }
        worker = max(candidates, key=lambda p: p.last_seen)
        self.log.info(
            "delegating job %s to peer %s at %s:%d",
            job["job_id"][:8],
            worker.peer_id[:8],
            worker.host,
            worker.tcp_port,
        )

        self.ledger.escrow(job["job_id"], worker.peer_id)
        conn: protocol.Connection | None = None
        try:
            conn = protocol.connect(worker.host, worker.tcp_port, self.identity, self.log)
            if conn.remote_peer_id != worker.peer_id:
                raise protocol.ProtocolError(
                    f"peer at {worker.host}:{worker.tcp_port} identified as "
                    f"{(conn.remote_peer_id or '?')[:8]}, expected {worker.peer_id[:8]}"
                )
            conn.send(protocol.TASK_OFFER, {"job": job})
            _, reply = conn.recv(
                expected=(protocol.TASK_ACCEPT, protocol.TASK_REJECT),
                timeout=_OFFER_REPLY_TIMEOUT,
            )
            if reply["type"] == protocol.TASK_REJECT:
                self.ledger.refund(job["job_id"])
                return {
                    "status": "rejected",
                    "job_id": job["job_id"],
                    "worker": worker.peer_id,
                    "error": reply["body"].get("reason", "rejected without reason"),
                    "refunded": True,
                }
            _, result_msg = conn.recv(
                expected=(protocol.TASK_RESULT,),
                timeout=job["max_runtime_seconds"] + _RESULT_GRACE_SECONDS,
            )
            result = result_msg["body"].get("result", {})
            if result.get("job_id") != job["job_id"]:
                raise protocol.ProtocolError("TASK_RESULT is for a different job")
            if result.get("status") != "ok":
                self.ledger.refund(job["job_id"])
                return {
                    "status": "failed",
                    "job_id": job["job_id"],
                    "worker": worker.peer_id,
                    "error": f"worker reported failure: {str(result.get('output'))[:500]}",
                    "refunded": True,
                }
            self.ledger.settle(job["job_id"], worker.peer_id)
            return {
                "status": "ok",
                "job_id": job["job_id"],
                "worker": worker.peer_id,
                "harness": result.get("harness"),
                "simulated": result.get("simulated", False),
                "runtime_seconds": result.get("runtime_seconds"),
                "output": result.get("output", ""),
            }
        except protocol.ProtocolError as exc:
            self.ledger.refund(job["job_id"])
            self.log.warning("delegation of job %s failed: %s (credit refunded)", job["job_id"][:8], exc)
            return {
                "status": "failed",
                "job_id": job["job_id"],
                "worker": worker.peer_id,
                "error": f"{exc}",
                "refunded": True,
            }
        finally:
            if conn is not None:
                conn.close()

    # -- local control channel (CLI -> running peer) ------------------------------

    def _handle_control(
        self, conn: protocol.Connection, sender: str, addr: tuple[str, int], payload: dict
    ) -> None:
        """Serve `mesh peers` / `mesh ledger` / `mesh delegate`.

        Control uses the same signed envelopes as everything else, but
        is only honoured when signed by *this peer's own key* and
        arriving over loopback — it is a local RPC, not a swarm message.
        """
        if sender != self.identity.peer_id or addr[0] not in ("127.0.0.1", "::1"):
            conn.send(protocol.CONTROL_RESULT, {"status": "error", "error": "control denied"})
            return
        body = payload.get("body", {})
        command = body.get("command")
        args = body.get("args", {})
        try:
            if command == "peers":
                assert self.discovery is not None
                data = [p.to_dict() for p in self.discovery.peers()]
            elif command == "ledger":
                data = {"balance": self.ledger.balance(), "records": self.ledger.records()}
            elif command == "delegate":
                data = self.delegate(
                    task_text=args["task_text"],
                    required_harness=args["required_harness"],
                    max_tokens=args["max_tokens"],
                    max_runtime_seconds=args["max_runtime_seconds"],
                    expected_output=args["expected_output"],
                )
            else:
                conn.send(protocol.CONTROL_RESULT, {"status": "error", "error": f"unknown command {command!r}"})
                return
        except (KeyError, LedgerError) as exc:
            conn.send(protocol.CONTROL_RESULT, {"status": "error", "error": f"{exc!r}"})
            return
        conn.send(protocol.CONTROL_RESULT, {"status": "ok", "data": data})
