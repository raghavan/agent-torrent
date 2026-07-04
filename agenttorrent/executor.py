"""Sandboxed task execution via the local harness CLI.

Protects invariant 4: a worker never executes anything outside its
sandbox directory. Enforced in code, regardless of what the task text
says:

* the subprocess runs with ``cwd`` set to a fresh temp workdir created
  per job and deleted afterwards;
* the subprocess environment is built from scratch (``HOME``/``TMPDIR``
  point inside the sandbox; only a minimal ``PATH`` is provided) — no
  environment variables are inherited from the peer process;
* a hard timeout from the (already validated) job manifest kills the
  subprocess;
* the harness binary is resolved to an absolute path by the worker, not
  taken from the job.

One deliberate, narrow exception: the worker's operator may allowlist
specific environment variables (e.g. ``ANTHROPIC_API_KEY``) to pass
into the sandbox so a real harness CLI can authenticate. The allowlist
comes from the worker's own config — never from the job — so the
invariant that nothing in the task text can widen the sandbox still
holds.

If no harness CLI is installed, execution is simulated with a canned
response so the protocol can be exercised without credentials.
``max_tokens`` from the job manifest is advisory: it is surfaced to the
harness in the prompt preamble but current CLIs expose no hard flag.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time

from .job import validate_job

DEFAULT_SIMULATE_DELAY_SECONDS = 1.0

_SIMULATED_TEMPLATE = """[simulated by AgentTorrent: this worker executed no harness CLI]

Task: {task_text}

def reverse_string(s: str) -> str:
    \"\"\"Return s reversed, without slicing.\"\"\"
    out = []
    for ch in s:
        out.insert(0, ch)
    return "".join(out)

(If the task above is not the reverse-a-string exercise, treat this as a
placeholder result proving the delegation protocol end to end.)
"""


def _harness_command(name: str, binary: str, job: dict) -> list[str]:
    """Non-interactive invocation for each supported harness CLI."""
    prompt = (
        f"[advisory limit: at most {job['max_tokens']} output tokens]\n"
        f"Expected output: {job['expected_output']}\n\n{job['task_text']}"
    )
    if name == "claude":
        return [binary, "-p", prompt]
    if name == "codex":
        return [binary, "exec", prompt]
    raise ValueError(f"unknown harness {name!r}")


def execute_job(
    job: dict,
    available_harnesses: dict[str, str],
    force_simulate: bool = False,
    simulate_delay: float = DEFAULT_SIMULATE_DELAY_SECONDS,
    env_passthrough: tuple[str, ...] | list[str] = (),
    log: logging.Logger | None = None,
) -> dict:
    """Run a validated job in a sandbox; return a result dict.

    Result fields: ``job_id``, ``status`` (``ok`` | ``error``),
    ``output``, ``harness`` (name or ``simulated``), ``simulated``,
    ``runtime_seconds``.
    """
    log = log or logging.getLogger("agenttorrent.executor")
    validate_job(job)  # never trust the caller; limits must be typed & present

    required = job["required_harness"]
    harness: str | None = None
    if not force_simulate:
        if required == "any":
            for candidate in ("claude", "codex"):
                if candidate in available_harnesses and shutil.which(candidate):
                    harness = candidate
                    break
        elif required in available_harnesses and shutil.which(required):
            harness = required

    started = time.monotonic()
    if harness is None:
        log.info("job %s: no usable harness (required=%s) — simulating", job["job_id"][:8], required)
        time.sleep(min(simulate_delay, job["max_runtime_seconds"]))
        return {
            "job_id": job["job_id"],
            "status": "ok",
            "output": _SIMULATED_TEMPLATE.format(task_text=job["task_text"]),
            "harness": "simulated",
            "simulated": True,
            "runtime_seconds": round(time.monotonic() - started, 3),
        }

    binary = shutil.which(harness)
    assert binary is not None
    workdir = tempfile.mkdtemp(prefix="agenttorrent-job-")
    # Fresh environment: nothing inherited from the peer process, except
    # variables the worker's operator explicitly allowlisted in config.
    sandbox_env = {
        "HOME": workdir,
        "TMPDIR": workdir,
        "PATH": os.defpath,
        "NO_COLOR": "1",
    }
    for name in env_passthrough:
        if name in os.environ:
            sandbox_env[name] = os.environ[name]
            log.info("job %s: passing allowlisted env var %s into sandbox", job["job_id"][:8], name)
    cmd = _harness_command(harness, binary, job)
    log.info(
        "job %s: executing via %s in sandbox %s (timeout %ds)",
        job["job_id"][:8],
        harness,
        workdir,
        job["max_runtime_seconds"],
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=workdir,
            env=sandbox_env,
            capture_output=True,
            text=True,
            timeout=job["max_runtime_seconds"],
        )
        if proc.returncode == 0:
            status, output = "ok", proc.stdout
        else:
            status = "error"
            output = f"harness exited {proc.returncode}: {proc.stderr.strip()[:2000]}"
    except subprocess.TimeoutExpired:
        status = "error"
        output = f"harness killed after hard timeout of {job['max_runtime_seconds']}s"
    except OSError as exc:
        status = "error"
        output = f"failed to launch harness: {exc}"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    log.info("job %s: %s in %.2fs", job["job_id"][:8], status, time.monotonic() - started)
    return {
        "job_id": job["job_id"],
        "status": status,
        "output": output,
        "harness": harness,
        "simulated": False,
        "runtime_seconds": round(time.monotonic() - started, 3),
    }
