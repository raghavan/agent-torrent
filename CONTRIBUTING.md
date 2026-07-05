# Contributing to AgentTorrent

Thanks for your interest! AgentTorrent is a research prototype, so the bar
for contributions is: keep it small, keep it observable, and never violate
an architecture invariant.

## The invariants are law

Every PR must preserve these (see the README for details):

1. A peer functions identically as requester and worker — no server role.
2. No central coordinator — discovery via UDP broadcast + bootstrap peers.
3. Every inter-peer message is a signed JSON envelope, verified before use.
4. Workers never execute outside their sandbox (fresh temp workdir, hard
   timeout, no inherited environment beyond the operator's explicit allowlist).
5. All swarm state must be rebuildable from gossip — restarts are safe.

If your change needs to bend one of these, open an issue first and make the
case.

## Development setup

```sh
git clone https://github.com/raghavan/agent-torrent && cd agent-torrent
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Before you open a PR

- Run the acceptance test — it must pass. It always runs against a real
  local model (no simulated fallback), so start one first (see the
  README's Local LLM section; no credentials needed):
  `AGENTTORRENT_API_BASE_URL=http://127.0.0.1:8080 python3 acceptance_test.py`
- Match the existing style: small files, type hints, module docstrings that
  say which invariant the module protects.
- Dependencies: standard library plus PyNaCl only. No new dependencies
  without prior discussion in an issue.
- Log every message a peer sends or receives — swarm behaviour must stay
  observable.
- Out of scope by design: tokens/blockchains, DHTs, TLS, GUIs.

## Reporting bugs and proposing features

Open a GitHub issue with reproduction steps (peer logs are gold — include
them). For security issues, see [SECURITY.md](SECURITY.md) instead.
