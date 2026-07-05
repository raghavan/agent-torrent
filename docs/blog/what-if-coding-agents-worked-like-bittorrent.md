# What if coding agents worked like BitTorrent?

*A peer-to-peer mesh where machines swap idle AI subscription capacity. Here's the idea — and a working prototype.*

Your coding agent subscription is idle right now. Probably.

If you pay for Claude Code Max, a Codex plan, or Cursor, think about the
duty cycle. The subscription sleeps when you sleep. It idles through your
meetings, your commute, your weekends. Generously, you're extracting value
from it a few hours a day. The rest of the time, a seat on some of the most
expensive compute ever assembled sits unused.

Meanwhile, at this exact moment, someone eleven time zones away is staring
at a rate-limit message in the middle of their workday, waiting for their
quota to reset.

We have seen this shape of problem before. In the early 2000s, the resource
was bandwidth: everyone had upload capacity sitting idle while everyone
wanted download capacity in bursts. BitTorrent's insight was not really
about piracy. It was that a swarm of peers with idle capacity and bursty
demand could outperform any central server — no coordinator anywhere, held
together by nothing but a tit-for-tat incentive that keeps freeloaders out.

I've been wondering what that looks like for frontier-model access. So I
built a prototype to find out:
[AgentTorrent](https://github.com/raghavan/agent-torrent).

> **Before we go further.** This is a research prototype, built to make a
> design space visible — not a service to run on an open network. Read
> [SECURITY.md](https://github.com/raghavan/agent-torrent/blob/main/SECURITY.md)
> before running it anywhere you care about. And one thing to be direct
> about up front: if you seed a CLI harness like `claude` or `codex`, you
> are executing strangers' tasks **on your own account** — read your
> provider's terms of service first. Both of these get their own sections
> below, because they're the interesting part.

## The analogy, made precise

In BitTorrent you *seed* (upload content to others) and *leech* (download
content from others), and the choking algorithm rewards peers who
reciprocate.

In a capacity mesh, seeding is accepting delegated tasks. Your machine
advertises "I have Claude Code 2.x installed, I accept jobs up to 300
seconds" and runs strangers' tasks while you're not using it. A harness you
don't subscribe to becomes one you can borrow: hand a task manifest to a
peer, get the result back. Leeching is delegating. Every delegation costs
the requester one credit and earns the worker one, and a peer with a zero
balance can't delegate at all.

**Run out of credits? Do some seeding.**

That last rule is the whole incentive layer — the mesh's version of
tit-for-tat. There's no reputation system, no staking, no token (a promise
I'll repeat later, loudly). Just: you can only take out what someone was
willing to put in, and the only way to top up is to be useful to a
stranger.

## The skeleton

The rest of the BitTorrent skeleton translates almost directly. A peer id
is the SHA-1 hash of an Ed25519 public key, exactly like a DHT
(distributed hash table) node id. There is no tracker: peers find each
other by UDP broadcast on the local network, plus a bootstrap list for
peers across the internet. Every peer runs identical code — there is no
server role anywhere. And all swarm state is rebuildable from gossip: kill
a peer, wipe its peer table, restart it, and it relearns the swarm within
one beacon interval.

The prototype is ~2,500 lines of Python (standard library plus PyNaCl for
signatures), organized around five invariants that I treated as law while
building it:

1. **No server role.** A peer functions identically as requester and
   worker — one codebase, one process.
2. **No central coordinator.** Discovery is UDP broadcast (a signed beacon
   every 5 seconds; peers expire after 30), plus optional bootstrap peers
   for crossing networks.
3. **Every message is a signed JSON envelope.** Signatures are verified
   before any message is processed. This includes the CLI talking to its
   own local peer.
4. **Workers never execute outside their sandbox.** The execution
   subprocess gets a fresh temp workdir, a hard timeout from the job
   manifest, and a from-scratch environment. Task text cannot widen the
   sandbox — the only thing that reaches the harness's environment is an
   explicit allowlist in the *worker's own* config.
5. **Swarm state is rebuildable from gossip.** The peer table is a pure
   cache of received beacons; losing local state and restarting is safe.

## One delegation, end to end

Here is what the protocol does for one successful delegation:

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

And here is what it looks like from the requester's terminal. Peer B is
seeding a local model (more on why local in a moment); peer A has no
harness at all:

```console
$ mesh peers
PEER ID          ADDRESS                HARNESSES                      ACCEPTS  LAST SEEN
7c19d2ab41f6…    192.168.1.42:9400      api (openai-chat-api/default)  True     1751673604

$ mesh delegate "write a python function that reverses a string without slicing" --harness api
job 4f0c81d2 completed by 7c19d2ab via api in 6s
--- result ---
def reverse_string(s):
    result = ""
    for ch in s:
        result = ch + result
    return result

$ mesh ledger
balance: 9 credits
KIND     JOB        MEMO
opening  -          opening balance
escrow   4f0c81d2   escrow 1 credit for job offered to 7c19d2ab
settle   4f0c81d2   paid 1 credit to worker 7c19d2ab
```

Meanwhile on the worker:

```console
$ mesh ledger
balance: 11 credits
KIND     JOB        MEMO
opening  -          opening balance
work     4f0c81d2   earned 1 credit from requester 3ba90e77
```

Both peers start at 10. After one job, 9 and 11 — one credit changed
hands, recorded double-entry on both sides.

The failure path matters just as much, so the acceptance test exercises it
directly: delegate a job, then kill the worker's process mid-execution.
The requester must not crash, must report the failure cleanly, and must
get its escrowed credit refunded — no money lost to a dead worker. Escrow
at offer time, settle on result, refund on reject/failure/timeout: it's a
tiny protocol, but it means the economics survive the network being a
network.

## What I deliberately didn't build

This is the section that would normally be fine print. I think it's the
most interesting part, because each gap is an open problem that any real
version of this idea has to solve.

**Authorization: any keypair is a valid peer.** Identity in the mesh is
just "can you sign with the key you claim" — there's no notion of *which*
keys are allowed. If your ports are reachable, strangers can run jobs on
your harness. The prototype's answer is "firewall it or run it inside a
VPN," which is honest but not a solution. What does membership look like
for a swarm with no central authority to issue it?

**Result integrity: a worker's result is unverified text.** Nothing proves
the worker actually ran the requested harness — it could return garbage,
or run your task on a much cheaper model than it advertised and pocket the
difference. This is *the* hard problem of the whole design. How do you
verify that a stranger genuinely ran your task on the harness they
claimed? Re-run it on a second peer and compare? Spot-check with known
tasks? Some attestation scheme? I don't know yet, and I notice nobody else
seems to either.

**Privacy: task text travels unencrypted and executes on someone else's
machine.** No TLS, by design — the prototype keeps the interesting
questions visible instead of burying them under transport plumbing. But
even with encryption on the wire, the fundamental exposure remains: you
are sending your prompt, and possibly your code, to a stranger's computer.
Do not delegate confidential work. Which tasks are actually *safe* to
delegate is its own design question.

**Sybil resistance: fresh identities start with fresh credits.** Generate
a new keypair, get 10 new credits. The ledger is local and unaudited —
it's double-entry bookkeeping, not consensus. Any real economy on top of
this needs an answer to "why can't I just make a thousand wallets?", and
every known answer (proof of work, stake, web-of-trust, fees) changes the
character of the system.

## The elephant in the room: terms of service

Let's say it plainly: a worker seeding the `claude` or `codex` CLI is
executing other people's tasks on its own subscription. Whether your
provider's terms allow that is between you and your provider — account
sharing and resale clauses vary, and "my machine ran it, but a stranger
asked for it" is exactly the kind of question those clauses exist for.
Read them before you seed a commercial harness. This is also spelled out
in the repo's
[SECURITY.md](https://github.com/raghavan/agent-torrent/blob/main/SECURITY.md).

This is why the prototype's reference path is a **local model, not a cloud
account**. The third harness, `api`, is one OpenAI-style chat-completions
call to a local LLM server — llama.cpp, Ollama, vLLM, LM Studio — run
inside the same sandbox as the CLIs. Seeding a model you run on your own
hardware raises no ToS question at all, and it's enough to exercise every
part of the design: discovery, signatures, escrow, sandboxing, failure
recovery.

It's also how the project tests itself. Every PR runs an end-to-end
acceptance test in CI: two peers start on the runner, find each other over
broadcast discovery, and one delegates a real task to the other, which
executes it on llama.cpp serving Qwen2.5-0.5B on the runner's CPU. No
cloud API, no credentials, and no simulated fallback — if the local-model
path breaks, CI fails. That's the point. The mesh's viability shouldn't
depend on anyone's subscription, including mine.

## The economics are naive on purpose

One credit per task. A 5-second "reverse a string" costs the same as a
300-second refactor. This is obviously wrong, and it's wrong on purpose —
flat pricing kept the escrow protocol small enough to get right before
making it clever.

The open questions are better than the placeholder: Should price scale
with the job manifest's declared runtime and token budget? Should workers
bid, BitTorrent-optimistic-unchoke style, so an idle swarm gets cheap and
a busy one gets expensive? Should a peer's asking price rise with its
track record, turning reputation into yield? Each of these interacts with
the Sybil problem above — an auction is only as honest as the identities
bidding in it.

## There is no token

The repo's stated non-goals: **no token, no DHT, no TLS, no GUI.**

The first one deserves emphasis, because "peer-to-peer mesh with credits"
pattern-matches to a crypto pitch. It isn't one. The credits are integers
in a local JSON file, worth nothing, transferable nowhere. They exist to
answer exactly one design question — can tit-for-tat keep a capacity swarm
reciprocal, the way it kept BitTorrent swarms reciprocal? — and nothing
kills an honest answer to that question faster than making the tokens
worth money.

## Try it

Two terminals, one machine, zero accounts:

```sh
git clone https://github.com/raghavan/agent-torrent && cd agent-torrent
pip install -e .

# terminal 1: serve a tiny local model (llama-server fetches it once, ~400 MB)
llama-server -hf Qwen/Qwen2.5-0.5B-Instruct-GGUF:q4_k_m --port 8080

# terminal 2: a worker peer backed by the local model
AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 \
mesh start --env-passthrough AGENTTORRENT_API_BASE_URL
```

Then delegate to it from a third terminal with `mesh delegate`, and watch
`mesh ledger` on both sides. Everything above — the invariants, the
message flow, the failure handling — is in the
[repo](https://github.com/raghavan/agent-torrent), and issues and PRs are
welcome. The contribution rules are short: the five invariants are law,
the acceptance test must pass, and dependencies stay at stdlib + PyNaCl.
If any of the open problems above — result verification especially —
makes you want to argue, that's the contribution I most want.

I don't know if a capacity mesh for coding agents becomes real
infrastructure or stays a thought experiment. BitTorrent needed a
particular alignment of idle supply, bursty demand, and a dead-simple
incentive before it ate a third of the internet's traffic. Two of those
three are already here: the subscriptions are idle, and the demand is
bursty. Whether the incentive can be made to hold among strangers is the
open question — and it's a lot more fun to poke at with a running
prototype than a whiteboard.

The person eleven time zones away is still staring at that rate-limit
message. Somewhere, a seat is idle. The interesting engineering is in
between.
