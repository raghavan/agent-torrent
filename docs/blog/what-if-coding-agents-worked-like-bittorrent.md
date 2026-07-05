# What if coding agents worked like BitTorrent?

*A peer-to-peer mesh where machines swap idle AI subscription capacity.
This post is about the idea and the economics; the working prototype and
all the technical detail live on
[GitHub](https://github.com/raghavan/agent-torrent).*

Your coding agent subscription is idle right now. Probably.

If you pay for Claude Code Max, a Codex plan, or Cursor, think about the
duty cycle. The subscription sleeps when you sleep. It idles through your
meetings, your commute, your weekends. Generously, you're extracting value
from it a few hours a day. Call it four hours of real use out of
twenty-four — a 15% duty cycle on a seat that costs real money, attached
to some of the most expensive compute ever assembled.

Meanwhile, at this exact moment, someone eleven time zones away is staring
at a rate-limit message in the middle of their workday, waiting for their
quota to reset.

Notice what those two facts are, together: idle supply here, unmet demand
there, and the sun guarantees the mismatch. Your midnight is someone's
2 p.m. The capacity and the need are never in the same place at the same
time — which is exactly the condition under which markets exist.

We have seen this shape of problem before. In the early 2000s, the
resource was bandwidth: everyone had upload capacity sitting idle while
everyone wanted download capacity in bursts. BitTorrent's insight was not
really about piracy. It was that a swarm of peers with idle capacity and
bursty demand could outperform any central server — no coordinator
anywhere, held together by nothing but a tit-for-tat incentive that keeps
freeloaders out.

I've been wondering what that looks like for frontier-model access. So I
built a prototype to find out:
[AgentTorrent](https://github.com/raghavan/agent-torrent).

> **Before we go further.** This is a research prototype built to explore
> a design space, not a service. If you're tempted to run it, read the
> project's
> [SECURITY.md](https://github.com/raghavan/agent-torrent/blob/main/SECURITY.md)
> first — including the part about your provider's terms of service, which
> gets its own section below because it's genuinely interesting.

## The analogy, made precise

In BitTorrent you *seed* (upload content to others) and *leech* (download
content from others), and the choking algorithm rewards peers who
reciprocate.

In a capacity mesh, seeding is accepting delegated tasks. Your machine
advertises "I have a coding agent installed, I accept jobs up to five
minutes" and runs strangers' tasks while you're not using it. Leeching is
delegating: a harness you don't subscribe to becomes one you can borrow —
hand a task to a peer, get the result back. Every delegation costs the
requester one credit and earns the worker one, and a peer with a zero
balance can't delegate at all.

**Run out of credits? Do some seeding.**

That one rule is the entire incentive layer. No reputation scores, no
staking, no token (a promise I'll repeat later, loudly). Just BitTorrent's
old bargain, restated for compute: you can only take out what someone was
willing to put in, and the only way to top up is to be useful to a
stranger.

The prototype makes the rest of the analogy literal — peer identities work
like torrent node ids, there's no tracker and no server role, and a peer
can be killed and restarted and relearn the swarm from gossip. If you want
the protocol, the sandbox model, and the message flow, it's all in the
[repo](https://github.com/raghavan/agent-torrent); this post stays on the
economics.

## Escrow, or: how to pay a stranger who might vanish

The interesting economic object in the prototype isn't the credit — it's
the escrow.

When you delegate, your credit doesn't go to the worker. It goes into
escrow at the moment of the offer, is released to the worker when a good
result comes back, and is refunded to you if the job is rejected, fails,
or times out. The prototype's acceptance test literally kills a worker
mid-job to prove the requester gets its credit back.

This matters because a mesh of strangers has no recourse. There's no
platform to appeal to, no chargeback, no support ticket. The payment
mechanism itself has to make failure boring: worker dies, you're refunded,
you try another peer. Escrow-settle-refund is the smallest mechanism I
could find that makes "pay a stranger for future work" safe when the
stranger is a process on the internet that might not exist in ten seconds.

## The pricing is naive on purpose

One credit per task. A five-second "reverse a string" costs the same as a
five-minute refactor. This is obviously wrong, and it's wrong on purpose —
flat pricing kept the mechanism small enough to get right before making it
clever.

But the placeholder points at real questions, and they're better than the
placeholder:

**Should price scale with declared cost?** Every job carries a declared
runtime and token budget. Pricing off those declarations is easy — but now
requesters have an incentive to under-declare, and workers to reject
anything suspicious. You've invented estimate haggling.

**Should workers bid?** BitTorrent's optimistic unchoking has a market
flavor — periodically give a freeloader free service, in case they turn
out to reciprocate. A capacity mesh could run the same way: an idle swarm
gets cheap, a busy swarm gets expensive, and price discovery happens
peer-to-peer with no order book anywhere. The time-zone structure of the
demand makes this genuinely interesting: the swarm's price should breathe
with the sun, cheap where it's night, dear where it's afternoon.

**Should reputation become yield?** A peer that has returned good results
for months is worth more per credit than a fresh keypair. Let its asking
price rise and reputation turns into income — which is exactly when people
start faking reputation. Every one of these refinements interacts with the
two hard problems below.

## The market for lemons

Here is the problem I now think dominates everything else, and it's a
century-old one: information asymmetry.

A worker's result is, to the requester, just text. Nothing proves the
worker actually ran the harness it advertised. A rational worker on a
frontier-model mesh has an obvious arbitrage: accept jobs claiming a
top-tier agent, run them on a cheap local model, pocket the difference.
The requester can't tell high-quality capacity from low-quality capacity
at the moment of purchase — and Akerlof told us in 1970 what happens next.
Bad capacity drives out good. Sellers of genuine frontier access leave the
market because they can't command a premium over the fakers, quality
craters, and the mesh becomes a market for lemons.

Every fix has a cost structure worth thinking about. Re-run each task on a
second peer and compare? You've doubled the price of everything to buy
verification. Spot-check workers with tasks whose answers you already
know? Cheaper, but now workers profit by detecting audits. Attestation
that a particular binary ran? Pushes trust into hardware vendors, which is
a different centralization. My honest position: I don't have a good
answer, and I notice that nobody building "decentralized AI" seems to have
one either. This is the open problem I most want people to argue with me
about.

## Printing money

The second hard problem: fresh identities start with fresh credits. An
identity is just a keypair, a keypair is free, and each one arrives with
an opening balance. So the naive version of this economy has a money
printer: generate a thousand identities, spend a thousand opening
balances, contribute nothing.

This is the Sybil problem, and it's really a question about what scarcity
backs the currency. BitTorrent dodged it elegantly — tit-for-tat was
enforced *within each connection*, per swarm, in real time, so a fresh
identity bought you nothing; you still had to upload right now to the
specific peer you wanted downloads from. A capacity mesh can't fully copy
that, because delegation is asymmetric: the peer with capacity to sell and
the peer with work to offer are usually not the same pair at the same
moment. That asymmetry is the whole point — and it's what forces the
currency to exist and store value across time, which is what makes it
counterfeitable.

The known escapes all change the system's character. Make identities
costly (proof of work — wasteful). Make them staked (now you need the
token I keep promising not to have). Make them vouched-for (web of trust —
membership politics). Or give newcomers zero credits and force everyone to
earn their way in by seeding first — the purest option, and maybe the
right one: it converts the opening balance from a gift into a wage.

## The elephant in the room: terms of service

Let's say it plainly: a worker seeding a commercial coding-agent CLI is
executing other people's tasks on its own subscription. Whether your
provider's terms allow that is between you and your provider —
account-sharing and resale clauses vary, and "my machine ran it, but a
stranger asked for it" is exactly the kind of question those clauses exist
for. Read them before you seed a commercial harness; the repo's
[SECURITY.md](https://github.com/raghavan/agent-torrent/blob/main/SECURITY.md)
says the same.

There's an economic reading of this, too. The idle capacity in your
subscription isn't accidental — flat-rate pricing *depends* on the duty
cycle. A provider sells unlimited-feeling access at a fixed price because
most seats idle most of the time; a mesh that pushes every seat toward
100% utilization is adversarial to that pricing model, and providers will
respond, contractually or technically. Any real capacity mesh has to
either make peace with providers or run on capacity the participants
actually own.

Which is why the prototype's reference path is a **local model, not a
cloud account**. Peers can seed a model running on their own hardware —
there's no ToS question when you own the weights and the machine — and the
project's CI tests the entire pipeline end to end against a real local
model with zero cloud credentials. The mesh's viability shouldn't depend
on anyone's subscription, including mine. And it suggests where the idea
is on solidest ground: not resold subscriptions, but a swarm of
GPU-owning peers seeding capacity that is genuinely theirs to sell.

## There is no token

The project's stated non-goals include: **no token.**

It deserves emphasis, because "peer-to-peer mesh with credits"
pattern-matches to a crypto pitch. It isn't one. The credits are integers
in a local file, worth nothing, transferable nowhere. They exist to answer
exactly one research question — can tit-for-tat keep a capacity swarm
reciprocal, the way it kept BitTorrent swarms reciprocal? — and nothing
kills an honest answer faster than making the tokens worth money. The
moment credits have a dollar price, every open problem above stops being a
design puzzle and becomes an attack with a payout.

## Where this goes

Everything mechanical — the protocol, the sandbox, the ledger, how to run
two peers on your own machine against a free local model — is in the
[GitHub repo](https://github.com/raghavan/agent-torrent), along with the
contribution rules. If the market-for-lemons section made you want to
argue, that's the contribution I most want.

I don't know if a capacity mesh for coding agents becomes real
infrastructure or stays a thought experiment. BitTorrent needed a
particular alignment before it ate a third of the internet's traffic: idle
supply, bursty demand, and an incentive simple enough to hold among
strangers. Two of the three are already here — the subscriptions are idle
and the demand is bursty. The third is an open question with a working
prototype attached, which is a much better kind of open question than the
whiteboard kind.

The person eleven time zones away is still staring at that rate-limit
message. Somewhere, a seat is idle. The interesting economics is in
between.
