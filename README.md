# AgentTorrent

![Agent Torrent — peer to peer meta harness for coding agents: submit a task, execute on a peer, verify the result](docs/agent-torrent-hero.png)

A prototype peer-to-peer **meta-harness**: desktop peers advertise agent
capabilities (the `claude` and `codex` CLIs) and delegate coding tasks to
each other, inspired by BitTorrent-style swarms. Python 3.11+, standard
library plus [PyNaCl](https://pynacl.readthedocs.io/) for signatures.

## Architecture invariants

1. **No server role.** A peer functions identically as requester and
   worker — one codebase, one process (`peer.py`).
2. **No central coordinator.** Discovery is UDP broadcast on the local
   network (beacon every 5 s, peers expire after 30 s), plus an optional
   `bootstrap_peers` list in config for peers across the internet
   (`discovery.py`).
3. **Every message is a signed JSON envelope.** An Ed25519 keypair is
   generated per peer on first run; a peer id is the SHA-1 hash of the
   public key, like a torrent node id. Signatures are verified before
   any message is processed (`identity.py`, `protocol.py`).
4. **Workers never execute outside their sandbox.** The execution
   subprocess gets a fresh temp workdir, a hard timeout from the job
   manifest, and a from-scratch environment (no inherited variables) —
   enforced in code, never trusted from task text (`executor.py`).
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

## Install & run

```sh
pip install -e .          # or: pip install pynacl && use python -m agenttorrent.cli

mesh start --data-dir ~/.agenttorrent            # run a peer (foreground)
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

## Acceptance test

```sh
python3 acceptance_test.py
```

Starts two peers on one machine (different TCP ports, shared broadcast
discovery port, simulated execution), delegates the reverse-a-string
task from A to B, checks the result and the one-credit ledger transfer
(A: 10→9, B: 10→11), then kills B mid-job and confirms A fails
gracefully and is refunded.

## Non-goals

No token, no DHT, no TLS, no GUI. Every message sent and received is
logged so the swarm's behaviour is observable.
