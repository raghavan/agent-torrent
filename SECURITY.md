# Security Policy

AgentTorrent is a **research prototype**. Run it only on networks and
machines where you accept the risks below.

## Threat model (what is and isn't defended)

Defended:
- **Message forgery**: every message is an Ed25519-signed envelope; peer ids
  are hashes of public keys, and both are verified before processing.
- **Malicious tasks vs the worker**: execution happens in a fresh temp
  workdir with a hard timeout and a from-scratch environment (plus only the
  operator's explicit env allowlist). Task text cannot widen the sandbox.

NOT defended — known, accepted gaps in the prototype:
- **Authorization**: any keypair is a valid peer. If your ports are
  reachable, strangers can send jobs to your harness. Firewall the UDP
  discovery port and TCP port to trusted networks, or run inside a VPN.
- **Result integrity**: a worker's result is unverified text. Nothing proves
  the worker actually ran the requested harness.
- **Privacy**: task text travels unencrypted (no TLS, by design) and is
  executed on another machine. Do not delegate confidential code.
- **Sybil resistance**: fresh identities start with fresh credits; the
  ledger is local and unaudited.

## Terms of service

AgentTorrent can drive the `claude` and `codex` CLIs. A worker seeding a
CLI harness executes **other peers' tasks on its own account** — whether
your provider's terms allow that is between you and your provider
(account-sharing and resale clauses vary). Read your provider's terms of
service before advertising a CLI harness to the swarm. The `api` harness
(a local LLM server you run yourself) raises no such question and is the
reference path used in CI.

## Reporting a vulnerability

For anything beyond the accepted gaps above (e.g. a sandbox escape, a
signature-verification bypass), please use GitHub's private vulnerability
reporting on this repository ("Security" tab → "Report a vulnerability")
rather than a public issue.
