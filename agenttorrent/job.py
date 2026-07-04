"""Job manifest schema and strict validation.

Supports invariant 4 indirectly: a worker only acts on a job manifest
that passed validation, so the executor's limits (timeout, harness
choice) always come from checked, typed fields — never from free text
in the task. Any job missing a field is rejected. There are no
defaults.
"""

from __future__ import annotations

import uuid

HARNESS_CHOICES = ("claude", "codex", "api", "any")

REQUIRED_FIELDS = (
    "job_id",
    "task_text",
    "required_harness",
    "max_tokens",
    "max_runtime_seconds",
    "expected_output",
)


class JobError(Exception):
    """Raised when a job manifest fails validation."""


def new_job(
    task_text: str,
    required_harness: str,
    max_tokens: int,
    max_runtime_seconds: int,
    expected_output: str,
) -> dict:
    """Build a complete job manifest with a fresh job id."""
    job = {
        "job_id": uuid.uuid4().hex,
        "task_text": task_text,
        "required_harness": required_harness,
        "max_tokens": max_tokens,
        "max_runtime_seconds": max_runtime_seconds,
        "expected_output": expected_output,
    }
    validate_job(job)
    return job


def validate_job(job: object) -> dict:
    """Validate a job manifest. Reject anything missing or mistyped."""
    if not isinstance(job, dict):
        raise JobError("job manifest is not an object")
    missing = [f for f in REQUIRED_FIELDS if f not in job]
    if missing:
        raise JobError(f"job manifest missing fields: {', '.join(missing)}")
    problems: list[str] = []
    for str_field in ("job_id", "task_text", "expected_output"):
        if not isinstance(job[str_field], str) or not job[str_field].strip():
            problems.append(f"{str_field} must be a non-empty string")
    if job["required_harness"] not in HARNESS_CHOICES:
        problems.append(f"required_harness must be one of {HARNESS_CHOICES}")
    for int_field in ("max_tokens", "max_runtime_seconds"):
        value = job[int_field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            problems.append(f"{int_field} must be a positive integer")
    if problems:
        raise JobError("; ".join(problems))
    return job
