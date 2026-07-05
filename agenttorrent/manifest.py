"""Capability manifest: what a peer advertises to the swarm.

Supports invariant 1 (every peer publishes the same manifest schema
regardless of role) and invariant 5 (the manifest travels inside every
discovery beacon, so any peer's view of the swarm can be rebuilt purely
from gossip).

Fields: ``peer_id``, ``harnesses`` (mapping of harness name to version
string — the ``claude`` and ``codex`` CLIs detected by probing PATH,
plus ``api`` when ``AGENTTORRENT_API_BASE_URL`` points at a local
OpenAI-compatible LLM server), ``max_runtime_seconds``, ``accepts_tasks``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

from .api_harness import DEFAULT_MODEL

log = logging.getLogger("agenttorrent.manifest")

KNOWN_HARNESSES = ("claude", "codex")
_VERSION_PROBE_TIMEOUT = 10


class ManifestError(Exception):
    """Raised when a manifest fails schema validation."""


def detect_harnesses() -> dict[str, str]:
    """Detect harness CLIs on PATH and return ``{name: version}``.

    A harness is advertised only if its CLI both exists on PATH and
    answers ``--version``; the version string is what gets gossiped.
    """
    found: dict[str, str] = {}
    for name in KNOWN_HARNESSES:
        path = shutil.which(name)
        if path is None:
            continue
        try:
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=_VERSION_PROBE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("harness %s found at %s but version probe failed: %s", name, path, exc)
            continue
        if proc.returncode == 0:
            version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "unknown"
            found[name] = version
            log.info("detected harness %s (%s)", name, version)
        else:
            log.warning("harness %s at %s exited %d on --version", name, path, proc.returncode)
    if os.environ.get("AGENTTORRENT_API_BASE_URL"):
        model = os.environ.get("AGENTTORRENT_API_MODEL", DEFAULT_MODEL)
        found["api"] = f"openai-chat-api/{model}"
        log.info("detected harness api (%s)", found["api"])
    return found


def build_manifest(
    peer_id: str,
    harnesses: dict[str, str],
    max_runtime_seconds: int,
    accepts_tasks: bool,
) -> dict:
    """Assemble a capability manifest for this peer."""
    return {
        "peer_id": peer_id,
        "harnesses": dict(harnesses),
        "max_runtime_seconds": int(max_runtime_seconds),
        "accepts_tasks": bool(accepts_tasks),
    }


def validate_manifest(manifest: object) -> dict:
    """Validate a manifest received from the network. No defaults."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest is not an object")
    problems: list[str] = []
    if not isinstance(manifest.get("peer_id"), str) or not manifest.get("peer_id"):
        problems.append("peer_id must be a non-empty string")
    harnesses = manifest.get("harnesses")
    if not isinstance(harnesses, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in harnesses.items()
    ):
        problems.append("harnesses must be a mapping of name to version string")
    mrs = manifest.get("max_runtime_seconds")
    if not isinstance(mrs, int) or isinstance(mrs, bool) or mrs <= 0:
        problems.append("max_runtime_seconds must be a positive integer")
    if not isinstance(manifest.get("accepts_tasks"), bool):
        problems.append("accepts_tasks must be a boolean")
    if problems:
        raise ManifestError("; ".join(problems))
    return manifest
