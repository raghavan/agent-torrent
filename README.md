![Agent Torrent — peer to peer meta harness for coding agents: submit a task, execute on a peer, verify the result](docs/agent-torrent-hero.png)

[![CI](https://github.com/raghavan/agent-torrent/actions/workflows/ci.yml/badge.svg)](https://github.com/raghavan/agent-torrent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

A peer-to-peer **meta-harness**: desktop peers advertise agent capabilities
(the `claude` and `codex` CLIs, or a [local LLM](#local-llm-no-cloud-account-needed))
and delegate coding tasks to each other, inspired by BitTorrent-style swarms.
Your subscription idles most of the day — AgentTorrent lets peers seed that
idle capacity to each other and earn credits to spend at their own peak.
Python 3.11+, standard library plus
[PyNaCl](https://pynacl.readthedocs.io/) for signatures.

> **Status: research prototype.** The protocol, sandbox, ledger, and discovery
> all work — every PR is [acceptance-tested](#acceptance-test) end-to-end in
> CI: one peer delegates a task to another that executes it on a **real
> local LLM** (llama.cpp + Qwen2.5-0.5B on the runner's CPU, zero API
> credentials — no cloud provider involved). But there is no authorization layer,
> no result verification, and no transport encryption — by design, to keep
> the interesting questions visible. Read [SECURITY.md](SECURITY.md) before
> running it outside a trusted network.

## Architecture invariants

1. **No server role.** A peer functions identically as requester and
   worker — one codebase, one process (`peer.py`).
2. **No central coordinator.** Discovery is UDP broadcast on the local
   network (beacon every 5 s, peers expire after 30 s), plus an optional
   `bootstrap_peers` list in config for peers across the internet. Peers
   also reply to first-seen beacons with a unicast beacon, so a NAT'd peer
   that bootstraps to a public peer learns about it in return
   (`discovery.py`).
3. **Every message is a signed JSON envelope.** An Ed25519 keypair is
   generated per peer on first run; a peer id is the SHA-1 hash of the
   public key, like a torrent node id. Signatures are verified before
   any message is processed (`identity.py`, `protocol.py`).
4. **Workers never execute outside their sandbox.** The execution
   subprocess gets a fresh temp workdir, a hard timeout from the job
   manifest, and a from-scratch environment — enforced in code, never
   trusted from task text (`executor.py`). The only exception is an
   explicit, operator-controlled env allowlist (see
   [Real execution](#real-execution-vs-simulation)).
5. **Swarm state is rebuildable from gossip.** The peer table is a pure
   cache of received beacons; losing local state and restarting is safe
   (`discovery.py`).

## Layout

| module | role |
|---|---|
| `identity.py` | Ed25519 keypair, peer ids, signed envelope seal/open |
| `manifest.py` | capability manifest (harness detection with version strings) |
| `discovery.py` | UDP broadcast beacons + gossip-built peer table |
| `protocol.py` | TCP JSON-line protocol: `HANDSHAKE`, `TASK_OFFER`, `TASK_ACCEPT`, `TASK_REJECT`, `TASK_RESULT` |
| `job.py` | job manifest schema — all fields required, no defaults |
| `executor.py` | sandboxed harness invocation, simulated when no harness is available |
| `api_harness.py` | the `api` harness: one OpenAI-style chat-completions call to a local LLM server, run inside the sandbox |
| `ledger.py` | plain-JSON double-entry credit ledger (both peers start at 10) |
| `peer.py` | the peer process: worker + requester + local control channel |
| `cli.py` | `mesh start`, `mesh peers`, `mesh delegate`, `mesh ledger` |

## Quick start

```sh
git clone https://github.com/raghavan/agent-torrent && cd agent-torrent
pip install -e .

mesh start                                       # run a peer (foreground)
mesh peers                                       # gossip-built peer table
mesh delegate "write a python function that reverses a string without slicing" --harness any
mesh ledger                                      # credit balance + records
```

`mesh peers` / `mesh delegate` / `mesh ledger` talk to the running peer
over the same signed TCP protocol (a `CONTROL` message honoured only
from the peer's own key on loopback).

Delegation costs the requester one credit (escrowed at offer time,
released to the worker on a good result, refunded on reject/failure/
timeout); the worker earns one. A peer with a zero balance refuses to
send a `TASK_OFFER`.

## Message flow for one successful delegation

```
Peer A (requester)                          Peer B (worker)
   |   <- both broadcast signed UDP beacons every 5s ->   |
   |--- TCP connect ---------------------------->|
   |--- HANDSHAKE ------------------------------>|  verify sig + id
   |<-- HANDSHAKE -------------------------------|  A verifies likewise
   |  A escrows 1 credit                         |
   |--- TASK_OFFER {job manifest} -------------->|  validate job, check harness
   |<-- TASK_ACCEPT {job_id} --------------------|
   |                                             |  execute in fresh sandbox,
   |                                             |  empty env, hard timeout
   |<-- TASK_RESULT {job_id, output} ------------|  B credits itself 1
   |  A settles escrow to B, prints result       |
```

If no `TASK_RESULT` arrives before the job's timeout (or the connection
drops, e.g. the worker died), A refunds its escrowed credit and reports
the failure gracefully.

## Deploying beyond one machine

**Same LAN** — nothing to configure; broadcast discovery finds peers
automatically.

**Across the internet (e.g. two VPSs)** — broadcast doesn't cross networks,
so point peers at each other's UDP discovery port:

```sh
# on host A                                # on host B
mesh start --tcp-port 9400 \              mesh start --tcp-port 9400 \
  --bootstrap-peer <B_IP>:47474             --bootstrap-peer <A_IP>:47474
```

Open UDP 47474 and the TCP port in both firewalls. A peer behind NAT can
bootstrap to a publicly reachable peer without any port forwarding: its
outbound beacons create the NAT mapping, and the public peer's unicast
beacon reply comes back through it. Delegation then works in the
NAT→public direction (the NAT'd peer can offer tasks to the public one).

**Easiest secure setup** — put all machines on a VPN
([Tailscale](https://tailscale.com)/WireGuard) and use `--bootstrap-peer`
with the VPN IPs. You get encryption and an authorization boundary for
free, which the prototype deliberately does not provide. **Do not expose
an accepting peer to the open internet** — any keypair is a valid peer,
so reachable ports mean strangers can run jobs on your harness.

## Real execution vs simulation

Three harnesses are supported: the `claude` and `codex` CLIs (detected on
PATH), and `api` — a direct call to a **local LLM server** (stdlib
`urllib`, no SDK), advertised whenever the worker's environment has an
`AGENTTORRENT_API_BASE_URL`. The `api` harness runs as a subprocess
inside the same sandbox as the CLIs and speaks OpenAI-style chat
completions — the wire format local LLM servers speak. Override the
model with `AGENTTORRENT_API_MODEL`.

### Local LLM (no cloud account needed)

Any OpenAI-compatible local server works as a worker backend — llama.cpp,
Ollama, vLLM, LM Studio. The lightest setup is llama.cpp with a ~400 MB
model that runs fine on CPU:

```sh
# terminal 1: serve a tiny model locally (llama-server fetches it once)
llama-server -hf Qwen/Qwen2.5-0.5B-Instruct-GGUF:q4_k_m --port 8080

# terminal 2: a worker peer backed by the local model
AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 \
mesh start --env-passthrough AGENTTORRENT_API_BASE_URL
```

With Ollama instead: `ollama serve` + `ollama pull qwen2.5:0.5b`, then
`AGENTTORRENT_API_BASE_URL=http://127.0.0.1:11434` and
`AGENTTORRENT_API_MODEL=qwen2.5:0.5b` (also passed through with
`--env-passthrough AGENTTORRENT_API_MODEL`).

If a worker has no harness at all (or runs with `--force-simulate`), it
returns a canned simulated response — the full protocol, ledger, and
sandbox path work with zero credentials.

For real execution, the sandbox's from-scratch environment means a
harness sees nothing by default. Allowlist exactly what it needs, on the
worker only — for the `api` harness that is `AGENTTORRENT_API_BASE_URL`
(as shown above); for a CLI harness, whatever auth variables that CLI
reads. The allowlist lives in the worker's own config and is never
influenced by the job — task text cannot widen the sandbox. Note that a
worker seeding a CLI harness executes tasks on its own account:
understand your provider's terms of service before seeding that capacity
to others.

## Acceptance test

```sh
# start a local model first (see the Local LLM section), then:
AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 python3 acceptance_test.py
```

The acceptance test **always runs against a real local model** — there
is no simulated fallback. If `AGENTTORRENT_API_BASE_URL` is missing or
the local-model path is broken, the test fails; that is the point.
Start a llama.cpp server as shown in
[Local LLM](#local-llm-no-cloud-account-needed) first (zero cost, no
account).

Think of the test as a short play between two people sharing one
machine:

1. **Two peers start up** — peer A and peer B, like two people running
   AgentTorrent — and find each other over broadcast discovery exactly
   as they would on a real network.
2. **A hires B.** A asks for "a python function that reverses a string
   without slicing" and puts 1 credit in escrow, like a deposit. B
   accepts, asks the local model from inside its sandbox, and sends the
   answer back.
3. **The answer must be real.** The test asserts the result is genuine
   LLM output — not a canned placeholder — and actually contains a
   function definition.
4. **The money must balance.** A's ledger shows 10 → 9 and B's shows
   10 → 11: exactly one credit changed hands, recorded on both sides.
5. **Then a worker dies on the job.** A hires B again and the test
   kills B's process mid-job. A must not crash, must report the failure
   cleanly, and must get its escrowed credit refunded — ending back at
   9 credits, no money lost to a dead worker. (For this step peer B is
   restarted with a fixed 3-second canned job so the kill reliably
   lands mid-execution — a live LLM call can't guarantee that timing.)

CI runs exactly this on every PR: llama.cpp serving
Qwen2.5-0.5B-Instruct on the runner's CPU, across Python 3.11 and 3.12.
No cloud API, no credentials — and if the local-model path doesn't
work, CI fails.

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The
short version: the five invariants above are law, the acceptance test
must pass, dependencies stay at stdlib + PyNaCl, and every message a peer
sends or receives gets logged. Security reports go through
[SECURITY.md](SECURITY.md), not public issues.

Deliberate non-goals: no token, no DHT, no TLS, no GUI.

## License

[MIT](LICENSE)
