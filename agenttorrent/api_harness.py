"""The ``api`` harness: one direct LLM API call, stdlib only.

Executed by the worker as a standalone script (never imported) inside the
job sandbox, so every executor invariant still holds: fresh temp workdir,
from-scratch environment, hard timeout enforced from outside. This file
must not import anything from the ``agenttorrent`` package — inside the
sandbox the package is not on ``sys.path``.

Two wire formats ("flavors"):

* ``anthropic`` (default) — the Anthropic Messages API.
* ``openai`` — OpenAI-style ``/v1/chat/completions``, which is what
  local LLM servers speak (llama.cpp ``llama-server``, Ollama, vLLM,
  LM Studio), so a peer can seed a fully local model to the swarm.

Raw HTTP instead of an SDK because dependencies stay at stdlib + PyNaCl
(see CONTRIBUTING.md).

Environment (all reach the sandbox only via the worker's env allowlist):

* ``ANTHROPIC_API_KEY``  — required; any placeholder works for local
  servers that don't check auth (e.g. ``local``).
* ``AGENTTORRENT_API_FLAVOR`` — ``anthropic`` (default) or ``openai``.
* ``AGENTTORRENT_API_MODEL`` — optional model override (defaults below;
  Ollama requires this to name a pulled model).
* ``ANTHROPIC_BASE_URL`` — optional API endpoint override, e.g.
  ``http://127.0.0.1:8080`` for a local llama-server. Same variable the
  official SDKs honour.
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
DEFAULT_OPENAI_MODEL = "default"  # llama-server serves whatever model it loaded
DEFAULT_BASE_URL = "https://api.anthropic.com"
FLAVORS = ("anthropic", "openai")


def _build_request(flavor: str, base_url: str, api_key: str, model: str,
                   max_tokens: int, prompt: str) -> urllib.request.Request:
    if flavor == "anthropic":
        path = "/v1/messages"
        auth = {"x-api-key": api_key, "anthropic-version": API_VERSION}
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        path = "/v1/chat/completions"
        auth = {"authorization": f"Bearer {api_key}"}
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    return urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **auth},
        method="POST",
    )


def _extract_text(flavor: str, payload: dict) -> tuple[str, str | None]:
    """Return (text, error). Exactly one of the two is meaningful."""
    if flavor == "anthropic":
        if payload.get("stop_reason") == "refusal":
            return "", "the model refused this request"
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        if not text.strip():
            return "", f"no text in response (stop_reason={payload.get('stop_reason')!r})"
        return text, None
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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "api harness: ANTHROPIC_API_KEY is not set in the sandbox "
            "(start the worker with --env-passthrough ANTHROPIC_API_KEY)",
            file=sys.stderr,
        )
        return 2
    flavor = os.environ.get("AGENTTORRENT_API_FLAVOR", "anthropic")
    if flavor not in FLAVORS:
        print(f"api harness: unknown AGENTTORRENT_API_FLAVOR {flavor!r} (use one of {FLAVORS})",
              file=sys.stderr)
        return 2

    default_model = DEFAULT_MODEL if flavor == "anthropic" else DEFAULT_OPENAI_MODEL
    model = os.environ.get("AGENTTORRENT_API_MODEL", default_model)
    base_url = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    request = _build_request(flavor, base_url, api_key, model, ns.max_tokens, ns.prompt)
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

    text, error = _extract_text(flavor, payload)
    if error:
        print(f"api harness: {error}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
