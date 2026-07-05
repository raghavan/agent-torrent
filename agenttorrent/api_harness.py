"""The ``api`` harness: one direct LLM API call to a local model, stdlib only.

Executed by the worker as a standalone script (never imported) inside the
job sandbox, so every executor invariant still holds: fresh temp workdir,
from-scratch environment, hard timeout enforced from outside. This file
must not import anything from the ``agenttorrent`` package — inside the
sandbox the package is not on ``sys.path``.

One wire format: OpenAI-style ``/v1/chat/completions``, which is what
local LLM servers speak (llama.cpp ``llama-server``, Ollama, vLLM,
LM Studio), so a peer can seed a fully local model to the swarm.

Raw HTTP instead of an SDK because dependencies stay at stdlib + PyNaCl
(see CONTRIBUTING.md).

Environment (all reach the sandbox only via the worker's env allowlist):

* ``AGENTTORRENT_API_BASE_URL`` — required; the local server's endpoint,
  e.g. ``http://127.0.0.1:8080`` for llama-server.
* ``AGENTTORRENT_API_MODEL`` — optional model override (defaults below;
  Ollama requires this to name a pulled model).
* ``AGENTTORRENT_API_KEY`` — optional bearer token; local servers don't
  check auth, so a placeholder is sent when unset.
* ``HTTPS_PROXY`` / ``SSL_CERT_FILE`` — honoured by urllib/ssl as usual.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL = "default"  # llama-server serves whatever model it loaded


def _build_request(base_url: str, api_key: str, model: str,
                   max_tokens: int, prompt: str) -> urllib.request.Request:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    return urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
        method="POST",
    )


def _extract_text(payload: dict) -> tuple[str, str | None]:
    """Return (text, error). Exactly one of the two is meaningful."""
    choices = payload.get("choices") or []
    text = (choices[0].get("message") or {}).get("content") or "" if choices else ""
    if not text.strip():
        return "", f"no text in response (finish_reason={choices[0].get('finish_reason')!r})" \
            if choices else "response has no choices"
    return text, None


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentTorrent direct-API harness")
    parser.add_argument("--max-tokens", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("prompt")
    ns = parser.parse_args()

    base_url = os.environ.get("AGENTTORRENT_API_BASE_URL")
    if not base_url:
        print(
            "api harness: AGENTTORRENT_API_BASE_URL is not set in the sandbox "
            "(start the worker with --env-passthrough AGENTTORRENT_API_BASE_URL)",
            file=sys.stderr,
        )
        return 2
    base_url = base_url.rstrip("/")
    api_key = os.environ.get("AGENTTORRENT_API_KEY", "local")
    model = os.environ.get("AGENTTORRENT_API_MODEL", DEFAULT_MODEL)
    request = _build_request(base_url, api_key, model, ns.max_tokens, ns.prompt)
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

    text, error = _extract_text(payload)
    if error:
        print(f"api harness: {error}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
