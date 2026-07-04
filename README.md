# AgentTorrent

![Agent Torrent — peer to peer meta harness for coding agents: submit a task, execute on a peer, verify the result](docs/agent-torrent-hero.png)

[![CI](https://github.com/raghavan/agent-torrent/actions/workflows/ci.yml/badge.svg)](https://github.com/raghavan/agent-torrent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

A peer-to-peer **meta-harness**: desktop peers advertise agent capabilities
(the `claude` and `codex` CLIs) and delegate coding tasks to each other,
inspired by BitTorrent-style swarms. Your subscription idles most of the day —
AgentTorrent lets peers seed that idle capacity to each other and earn credits
to spend at their own peak. Python 3.11+, standard library plus
[PyNaCl](https://pynacl.readthedocs.io/) for signatures.

> **Status: research prototype.** The protocol, sandbox, ledger, and discovery
> all work (see the [acceptance test](#acceptance-test)), but there is no
> authorization layer, no result verification, and no transport encryption —
> by design, to keep the interesting questions visible. Read
> [SECURITY.md](SECURITY.md) before running it outside a trusted network.

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
| `executor.py` | sandboxed harness invocation, simulated when no CLI is installed |
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

If a worker has no harness CLI installed (or runs with `--force-simulate`),
it returns a canned simulated response — the full protocol, ledger, and
sandbox path work with zero credentials.

For real execution, the sandbox's from-scratch environment means the
harness CLI has no credentials by default. Allowlist exactly what it
needs, on the worker only:

```sh
ANTHROPIC_API_KEY=sk-... mesh start --env-passthrough ANTHROPIC_API_KEY
```

The allowlist lives in the worker's own config and is never influenced by
the job — task text cannot widen the sandbox. Note that a worker executes
tasks on its own account: understand your provider's terms of service
before seeding capacity to others.

## Acceptance test

```sh
python3 acceptance_test.py
```

Starts two peers on one machine (different TCP ports, shared broadcast
discovery port, simulated execution), delegates the reverse-a-string
task from A to B, checks the result and the one-credit ledger transfer
(A: 10→9, B: 10→11), then kills B mid-job and confirms A fails
gracefully and is refunded. The same test runs in CI on every PR.

## Contributing

Issues and PRs are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The
short version: the five invariants above are law, the acceptance test
must pass, dependencies stay at stdlib + PyNaCl, and every message a peer
sends or receives gets logged. Security reports go through
[SECURITY.md](SECURITY.md), not public issues.

Deliberate non-goals: no token, no DHT, no TLS, no GUI.

## License

[MIT](LICENSE)
