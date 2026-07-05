#!/usr/bin/env python3
"""AgentTorrent acceptance test — always runs against a real local model.

Requires ``AGENTTORRENT_API_BASE_URL`` pointing at an OpenAI-compatible
local LLM server. There is no simulated fallback: if the local-model
path is broken or unconfigured, the test fails — that is the point, so
CI cannot go green without it. E.g. with llama.cpp serving a small
model on port 8080:

    llama-server -hf Qwen/Qwen2.5-0.5B-Instruct-GGUF:q4_k_m --port 8080
    AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 python3 acceptance_test.py

Scenario (run on one machine, real UDP broadcast discovery, real TCP):

1. Start peer A and peer B on different TCP ports sharing one UDP
   discovery port. Peer B executes for real via the ``api`` harness —
   a live call to the local LLM server.
2. Peer A delegates "write a python function that reverses a string
   without slicing". Peer B accepts, executes in its sandbox, returns
   the result, which must be genuine LLM output (not the canned
   simulation) and must contain a function definition. A's ledger
   shows a 1-credit debit (10 -> 9) and B's a 1-credit credit
   (10 -> 11).
3. Peer B is killed mid-job during a second delegation. Peer A fails
   gracefully (no crash, clear error) and the escrowed credit is
   refunded, leaving A's balance unchanged. For this step peer B is
   restarted with a fixed 3-second canned execution so the kill lands
   mid-job deterministically — a live LLM call cannot guarantee that
   timing.

``AGENTTORRENT_API_MODEL`` and ``AGENTTORRENT_API_KEY`` are forwarded
to the worker's sandbox when set.

Exits 0 on success, 1 on failure (with both peers' logs dumped).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
RUN_DIR = REPO_ROOT / "acceptance-run"
PYTHON = sys.executable

DISCOVERY_PORT = 46000 + (os.getpid() % 1000)
TCP_PORT_A = 9401
TCP_PORT_B = 9402
SIMULATE_DELAY = 3.0  # long enough to kill peer B mid-job
REAL_MAX_RUNTIME = 120  # a live LLM call needs more headroom than a canned reply

TASK_TEXT = "write a python function that reverses a string without slicing"

# Env vars the worker copies into its execution sandbox.
SANDBOX_ENV_VARS = [
    name
    for name in ("AGENTTORRENT_API_BASE_URL", "AGENTTORRENT_API_MODEL",
                 "AGENTTORRENT_API_KEY", "HTTPS_PROXY", "SSL_CERT_FILE")
    if name in os.environ
]


def mesh(data_dir: Path, *args: str) -> list[str]:
    return [PYTHON, "-m", "agenttorrent.cli", "--data-dir", str(data_dir), *args]


def start_peer(
    name: str, data_dir: Path, tcp_port: int, simulate: bool = True
) -> tuple[subprocess.Popen, Path]:
    log_path = RUN_DIR / f"{name}.log"
    log_file = open(log_path, "w")
    args = [
        "start",
        "--tcp-port", str(tcp_port),
        "--discovery-port", str(DISCOVERY_PORT),
    ]
    if simulate:
        args += ["--force-simulate", "--simulate-delay", str(SIMULATE_DELAY)]
    else:
        for var in SANDBOX_ENV_VARS:
            args += ["--env-passthrough", var]
    proc = subprocess.Popen(
        mesh(data_dir, *args),
        cwd=REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path


def cli_json(data_dir: Path, *args: str, timeout: float = 120.0) -> tuple[int, dict | list]:
    proc = subprocess.run(
        mesh(data_dir, *args, "--json"),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if not proc.stdout.strip():
        raise AssertionError(f"CLI {' '.join(args)} produced no output; stderr:\n{proc.stderr}")
    return proc.returncode, json.loads(proc.stdout)


def balance(data_dir: Path) -> int:
    _, data = cli_json(data_dir, "ledger")
    return data["balance"]


def record_kinds(data_dir: Path) -> list[str]:
    _, data = cli_json(data_dir, "ledger")
    return [r["kind"] for r in data["records"]]


def wait_for_peer(data_dir: Path, expected_peers: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _, peers = cli_json(data_dir, "peers")
            if len(peers) >= expected_peers:
                return
        except (AssertionError, subprocess.TimeoutExpired, SystemExit):
            pass
        time.sleep(1.0)
    raise AssertionError(f"peer table of {data_dir.name} never reached {expected_peers} peer(s)")


def dump_logs(logs: list[Path]) -> None:
    for path in logs:
        print(f"\n===== {path.name} (last 60 lines) =====", file=sys.stderr)
        if path.exists():
            for line in path.read_text().splitlines()[-60:]:
                print(line, file=sys.stderr)


def main() -> int:
    base_url = os.environ.get("AGENTTORRENT_API_BASE_URL")
    if not base_url:
        print(
            "acceptance test: AGENTTORRENT_API_BASE_URL is not set.\n"
            "The test always runs against a real local model — start an\n"
            "OpenAI-compatible server and point the test at it, e.g.:\n"
            "  llama-server -hf Qwen/Qwen2.5-0.5B-Instruct-GGUF:q4_k_m --port 8080\n"
            "  AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 python3 acceptance_test.py",
            file=sys.stderr,
        )
        return 1
    if RUN_DIR.exists():
        shutil.rmtree(RUN_DIR)
    RUN_DIR.mkdir()
    dir_a, dir_b = RUN_DIR / "peer-a", RUN_DIR / "peer-b"

    procs: list[subprocess.Popen] = []
    logs: list[Path] = []
    try:
        print(f"[1/6] starting peer A (tcp {TCP_PORT_A}) and peer B (tcp {TCP_PORT_B}, "
              f"real execution on the local model at {base_url}), "
              f"shared discovery udp/{DISCOVERY_PORT}")
        peer_a, log_a = start_peer("peer-a", dir_a, TCP_PORT_A)
        peer_b, log_b = start_peer("peer-b", dir_b, TCP_PORT_B, simulate=False)
        procs += [peer_a, peer_b]
        logs += [log_a, log_b]

        print("[2/6] waiting for broadcast discovery to converge...")
        wait_for_peer(dir_a, 1)
        wait_for_peer(dir_b, 1)
        assert peer_a.poll() is None and peer_b.poll() is None, "a peer process died during discovery"
        print("      both peers see each other")

        print(f"[3/6] peer A delegating (harness=api): {TASK_TEXT!r}")
        rc, result = cli_json(
            dir_a, "delegate", TASK_TEXT, "--harness", "api",
            "--max-runtime", str(REAL_MAX_RUNTIME),
            "--max-tokens", "512",  # plenty for this task; keeps small local models from rambling
            timeout=60.0 + REAL_MAX_RUNTIME,
        )
        assert rc == 0, f"delegate exited {rc}: {result}"
        assert result["status"] == "ok", f"delegation failed: {result}"
        assert result["output"].strip(), "empty result output"
        assert result["simulated"] is False, f"expected real execution, got: {result}"
        assert result["harness"] == "api", f"expected api harness, got: {result['harness']}"
        assert "[simulated by AgentTorrent" not in result["output"], "got the canned simulated reply"
        assert "def " in result["output"] or "lambda" in result["output"], (
            "LLM output does not look like a python function:\n" + result["output"]
        )
        print("      real LLM execution confirmed (harness=api, simulated=false)")
        print("      --- result from peer B ---")
        for line in result["output"].splitlines():
            print(f"      {line}")

        print("[4/6] checking ledgers for the one-credit transfer")
        bal_a, bal_b = balance(dir_a), balance(dir_b)
        assert bal_a == 9, f"peer A balance is {bal_a}, expected 9"
        assert bal_b == 11, f"peer B balance is {bal_b}, expected 11"
        assert record_kinds(dir_a) == ["opening", "escrow", "settle"], record_kinds(dir_a)
        assert record_kinds(dir_b) == ["opening", "work"], record_kinds(dir_b)
        print(f"      peer A: 10 -> {bal_a} (debited), peer B: 10 -> {bal_b} (credited)")

        # The kill must land while the job is still running, which a real
        # LLM call cannot guarantee — restart peer B (same identity and
        # ledger) with a canned 3s execution time for the crash test.
        print("[5/6] restarting peer B with a fixed 3s canned job, then killing it mid-job")
        peer_b.terminate()
        peer_b.wait(timeout=10)
        peer_b, log_b = start_peer("peer-b-sim", dir_b, TCP_PORT_B, simulate=True)
        procs.append(peer_b)
        logs.append(log_b)
        wait_for_peer(dir_b, 1)
        wait_for_peer(dir_a, 1)
        delegate2 = subprocess.Popen(
            mesh(dir_a, "delegate", "sum the first 100 primes", "--harness", "any",
                 "--max-runtime", "30", "--json"),
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.5)  # offer + accept happen fast; simulated run takes 3s
        assert delegate2.poll() is None, "second delegation finished before we could kill peer B"
        peer_b.send_signal(signal.SIGKILL)
        print(f"      SIGKILL sent to peer B (pid {peer_b.pid})")
        out, err = delegate2.communicate(timeout=60)
        result2 = json.loads(out)
        assert delegate2.returncode != 0, "delegation should fail when the worker dies"
        assert result2["status"] == "failed", f"expected failed, got: {result2}"
        assert result2.get("refunded") is True, f"no refund recorded: {result2}"
        print(f"      peer A failed gracefully: {result2['error']}")

        print("[6/6] confirming peer A was refunded")
        bal_a2 = balance(dir_a)
        assert bal_a2 == 9, f"peer A balance is {bal_a2} after refund, expected 9"
        kinds = record_kinds(dir_a)
        assert kinds == ["opening", "escrow", "settle", "escrow", "refund"], kinds
        print(f"      peer A balance back to {bal_a2}; ledger shows escrow followed by refund")

        print("\nACCEPTANCE TEST PASSED")
        return 0
    except (AssertionError, Exception) as exc:  # noqa: BLE001 - report anything
        print(f"\nACCEPTANCE TEST FAILED: {exc!r}", file=sys.stderr)
        dump_logs(logs)
        return 1
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    sys.exit(main())
