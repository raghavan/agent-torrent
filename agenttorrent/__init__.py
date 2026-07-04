"""AgentTorrent: a peer-to-peer meta-harness.

Desktop peers advertise agent capabilities (claude / codex CLIs) and
delegate coding tasks to each other, BitTorrent-swarm style.

Architecture invariants (see individual modules for enforcement):

1. A peer functions identically as requester and worker. One codebase,
   one process, no server role (``peer.py``).
2. No central coordinator. Discovery is UDP broadcast plus optional
   ``bootstrap_peers`` (``discovery.py``).
3. Every inter-peer message is a signed JSON envelope; a peer id is the
   hash of its Ed25519 public key (``identity.py``).
4. A worker never executes anything outside its sandbox directory
   (``executor.py``).
5. All swarm state is rebuildable from gossip; losing local state and
   restarting is safe (``discovery.py``).
"""

__version__ = "0.1.0"
