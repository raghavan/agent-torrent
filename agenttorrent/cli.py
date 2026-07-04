"""AgentTorrent command line: mesh start | peers | delegate | ledger.

``mesh start`` runs a peer in the foreground. The other commands talk
to that running peer over the same signed TCP protocol, using the
CONTROL message type — signed with the peer's own key (loaded from the
shared data directory) and accepted only on loopback, so even local RPC
honours invariant 3 (every message is a signed envelope).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import protocol
from .identity import Identity
from .peer import RUNTIME_FILENAME, Peer, PeerConfig

DEFAULT_DATA_DIR = Path.home() / ".agenttorrent"

_CONTROL_TIMEOUT_BASE = 30.0


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _control_request(data_dir: Path, command: str, args: dict, timeout: float) -> dict:
    """Send one CONTROL command to the running peer and return its data."""
    runtime_path = data_dir / RUNTIME_FILENAME
    if not runtime_path.exists():
        raise SystemExit(f"no running peer found for {data_dir} (start one with: mesh start --data-dir {data_dir})")
    runtime = json.loads(runtime_path.read_text())
    identity = Identity.load_or_create(data_dir)
    log = logging.getLogger(f"agenttorrent.cli.{identity.short_id}")
    conn = protocol.connect("127.0.0.1", runtime["tcp_port"], identity, log, timeout=10.0)
    try:
        conn.send(protocol.CONTROL, {"command": command, "args": args})
        _, payload = conn.recv(expected=(protocol.CONTROL_RESULT,), timeout=timeout)
    finally:
        conn.close()
    body = payload["body"]
    if body.get("status") != "ok":
        raise SystemExit(f"peer refused control command: {body.get('error')}")
    return body["data"]


# -- subcommands -------------------------------------------------------------


def cmd_start(ns: argparse.Namespace) -> int:
    data_dir = Path(ns.data_dir).expanduser()
    config = PeerConfig.load(
        data_dir,
        overrides={
            "tcp_port": ns.tcp_port,
            "discovery_port": ns.discovery_port,
            "bootstrap_peers": ns.bootstrap_peer or None,
            "accepts_tasks": None if ns.accepts_tasks is None else ns.accepts_tasks,
            "force_simulate": True if ns.force_simulate else None,
            "simulate_delay": ns.simulate_delay,
            "max_runtime_seconds": ns.max_runtime,
        },
    )
    peer = Peer(data_dir, config)
    peer.run_forever()
    return 0


def cmd_peers(ns: argparse.Namespace) -> int:
    peers = _control_request(Path(ns.data_dir).expanduser(), "peers", {}, _CONTROL_TIMEOUT_BASE)
    if ns.json:
        print(json.dumps(peers, indent=2))
        return 0
    if not peers:
        print("no live peers in the swarm")
        return 0
    print(f"{'PEER ID':<16} {'ADDRESS':<22} {'HARNESSES':<30} {'ACCEPTS':<8} LAST SEEN")
    for p in peers:
        harnesses = ", ".join(f"{k} ({v})" for k, v in p["manifest"]["harnesses"].items()) or "none (simulated)"
        print(
            f"{p['peer_id'][:12] + '…':<16} {p['host'] + ':' + str(p['tcp_port']):<22} "
            f"{harnesses[:29]:<30} {str(p['manifest']['accepts_tasks']):<8} {p['last_seen']:.0f}"
        )
    return 0


def cmd_delegate(ns: argparse.Namespace) -> int:
    args = {
        "task_text": ns.task,
        "required_harness": ns.harness,
        "max_tokens": ns.max_tokens,
        "max_runtime_seconds": ns.max_runtime,
        "expected_output": ns.expect,
    }
    timeout = _CONTROL_TIMEOUT_BASE + ns.max_runtime
    result = _control_request(Path(ns.data_dir).expanduser(), "delegate", args, timeout)
    if ns.json:
        print(json.dumps(result, indent=2))
    elif result["status"] == "ok":
        print(f"job {result['job_id'][:8]} completed by {result['worker'][:8]} "
              f"via {result['harness']} in {result['runtime_seconds']}s")
        print("--- result ---")
        print(result["output"])
    else:
        print(f"delegation {result['status']}: {result.get('error')}", file=sys.stderr)
        if result.get("refunded"):
            print("escrowed credit was refunded", file=sys.stderr)
    return 0 if result["status"] == "ok" else 2


def cmd_ledger(ns: argparse.Namespace) -> int:
    data = _control_request(Path(ns.data_dir).expanduser(), "ledger", {}, _CONTROL_TIMEOUT_BASE)
    if ns.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"balance: {data['balance']} credits")
    print(f"{'KIND':<8} {'JOB':<10} MEMO")
    for r in data["records"]:
        print(f"{r['kind']:<8} {(r['job_id'] or '-')[:8]:<10} {r['memo']}")
    return 0


# -- argument parsing ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mesh", description="AgentTorrent peer-to-peer meta-harness")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="peer data directory (keys, ledger, config)")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="run a peer in the foreground")
    p_start.add_argument("--tcp-port", type=int, default=None, help="TCP listen port (default: pick a free port)")
    p_start.add_argument("--discovery-port", type=int, default=None, help="UDP broadcast port (default 47474)")
    p_start.add_argument("--bootstrap-peer", action="append", metavar="HOST:UDP_PORT",
                         help="unicast beacon target for peers beyond the local network (repeatable)")
    p_start.add_argument("--no-accept-tasks", dest="accepts_tasks", action="store_const", const=False,
                         default=None, help="advertise accepts_tasks=false")
    p_start.add_argument("--force-simulate", action="store_true",
                         help="always simulate execution even if a harness CLI is installed")
    p_start.add_argument("--simulate-delay", type=float, default=None,
                         help="seconds a simulated execution takes (default 1.0)")
    p_start.add_argument("--max-runtime", type=int, default=None,
                         help="largest job runtime this peer will accept (default 300)")
    p_start.set_defaults(func=cmd_start)

    p_peers = sub.add_parser("peers", help="show the gossip-built peer table")
    p_peers.add_argument("--json", action="store_true")
    p_peers.set_defaults(func=cmd_peers)

    p_delegate = sub.add_parser("delegate", help="delegate a task to the swarm")
    p_delegate.add_argument("task", help="task text for the remote harness")
    p_delegate.add_argument("--harness", choices=("claude", "codex", "any"), default="any")
    p_delegate.add_argument("--max-tokens", type=int, default=4096)
    p_delegate.add_argument("--max-runtime", type=int, default=60, help="hard timeout for the job in seconds")
    p_delegate.add_argument("--expect", default="plain text answer", help="expected_output description")
    p_delegate.add_argument("--json", action="store_true")
    p_delegate.set_defaults(func=cmd_delegate)

    p_ledger = sub.add_parser("ledger", help="show this peer's credit ledger")
    p_ledger.add_argument("--json", action="store_true")
    p_ledger.set_defaults(func=cmd_ledger)

    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    _setup_logging(ns.verbose)
    return ns.func(ns)


if __name__ == "__main__":
    sys.exit(main())
