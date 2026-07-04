"""The ``api`` harness: one direct Anthropic Messages API call, stdlib only.

Executed by the worker as a standalone script (never imported) inside the
job sandbox, so every executor invariant still holds: fresh temp workdir,
from-scratch environment, hard timeout enforced from outside. This file
must not import anything from the ``agenttorrent`` package — inside the
sandbox the package is not on ``sys.path``.

Raw HTTP instead of the ``anthropic`` SDK because dependencies stay at
stdlib + PyNaCl (see CONTRIBUTING.md).

Environment (all reach the sandbox only via the worker's env allowlist):

* ``ANTHROPIC_API_KEY``  — required.
* ``AGENTTORRENT_API_MODEL`` — optional model override (default below).
* ``ANTHROPIC_BASE_URL`` — optional API endpoint override (useful for
  tests and proxies), same variable the official SDKs honour.
* ``HTTPS_PROXY`` / ``SSL_CERT_FILE`` — honoured by urllib/ssl as usual.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_BASE_URL = "https://api.anthropic.com"


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentTorrent direct-API harness")
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("prompt")
    ns = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "api harness: ANTHROPIC_API_KEY is not set in the sandbox "
            "(start the worker with --env-passthrough ANTHROPIC_API_KEY)",
            file=sys.stderr,
        )
        return 2

    model = os.environ.get("AGENTTORRENT_API_MODEL", DEFAULT_MODEL)
    base_url = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=json.dumps(
            {
                "model": model,
                "max_tokens": ns.max_tokens,
                "thinking": {"type": "adaptive"},
                "messages": [{"role": "user", "content": ns.prompt}],
            }
        ).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=ns.timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        print(f"api harness: HTTP {exc.code} from {base_url}: {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"api harness: request to {base_url} failed: {exc}", file=sys.stderr)
        return 1

    if payload.get("stop_reason") == "refusal":
        print("api harness: the model refused this request", file=sys.stderr)
        return 1
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    )
    if not text.strip():
        print(
            f"api harness: no text in response (stop_reason={payload.get('stop_reason')!r})",
            file=sys.stderr,
        )
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
