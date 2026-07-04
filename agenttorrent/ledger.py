"""Plain-JSON double-entry credit ledger.

Every peer keeps its own ledger file. Each record carries balanced
double-entry lines (deltas summing to zero) across four kinds of
account: ``self``, ``genesis`` (opening balance source), ``escrow``
(credits reserved for in-flight jobs), and remote peer ids.

Flow for one job, requester side:
  escrow:  self -1, escrow +1     (at TASK_OFFER time)
  settle:  escrow -1, <worker> +1 (on TASK_RESULT ok)
  refund:  escrow -1, self +1     (on reject / timeout / failure)

Worker side:
  credit:  <requester> -1, self +1 (on returning a successful result)

Both peers start with 10 credits. ``can_offer`` refuses a TASK_OFFER
when the spendable balance is zero. This module holds *economic* state,
which is intentionally local and per-peer; swarm state proper stays
rebuildable from gossip (invariant 5).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

log = logging.getLogger("agenttorrent.ledger")

OPENING_BALANCE = 10
JOB_PRICE = 1
LEDGER_FILENAME = "ledger.json"


class LedgerError(Exception):
    """Raised on invalid ledger operations (double settle, unknown job)."""


class Ledger:
    """Append-only double-entry ledger persisted as one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        if self.path.exists():
            self._records: list[dict] = json.loads(self.path.read_text())["records"]
        else:
            self._records = []
            self._append(
                kind="opening",
                job_id=None,
                entries=[
                    {"account": "genesis", "delta": -OPENING_BALANCE},
                    {"account": "self", "delta": OPENING_BALANCE},
                ],
                memo="opening balance",
            )

    # -- primitives ----------------------------------------------------------

    def _append(self, kind: str, job_id: str | None, entries: list[dict], memo: str) -> dict:
        assert sum(e["delta"] for e in entries) == 0, "double-entry lines must balance"
        record = {
            "txn_id": uuid.uuid4().hex,
            "kind": kind,
            "job_id": job_id,
            "ts": time.time(),
            "entries": entries,
            "memo": memo,
        }
        self._records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"records": self._records}, indent=2))
        tmp.replace(self.path)
        log.info("ledger: %s job=%s %s", kind, (job_id or "-")[:8], memo)
        return record

    def _balance_locked(self, account: str) -> int:
        return sum(e["delta"] for r in self._records for e in r["entries"] if e["account"] == account)

    def _job_records(self, job_id: str) -> list[dict]:
        return [r for r in self._records if r["job_id"] == job_id]

    # -- public API ------------------------------------------------------------

    def balance(self, account: str = "self") -> int:
        with self._lock:
            return self._balance_locked(account)

    def can_offer(self) -> bool:
        """Refuse to send a TASK_OFFER when the spendable balance is zero."""
        return self.balance("self") >= JOB_PRICE

    def escrow(self, job_id: str, worker_id: str) -> None:
        with self._lock:
            if self._balance_locked("self") < JOB_PRICE:
                raise LedgerError("balance is zero: cannot offer a task")
            if self._job_records(job_id):
                raise LedgerError(f"job {job_id} already has ledger records")
            self._append(
                kind="escrow",
                job_id=job_id,
                entries=[
                    {"account": "self", "delta": -JOB_PRICE},
                    {"account": "escrow", "delta": JOB_PRICE},
                ],
                memo=f"escrow {JOB_PRICE} credit for job offered to {worker_id[:8]}",
            )

    def settle(self, job_id: str, worker_id: str) -> None:
        """Release escrowed credit to the worker after a good result."""
        with self._lock:
            self._require_open_escrow(job_id)
            self._append(
                kind="settle",
                job_id=job_id,
                entries=[
                    {"account": "escrow", "delta": -JOB_PRICE},
                    {"account": worker_id, "delta": JOB_PRICE},
                ],
                memo=f"paid {JOB_PRICE} credit to worker {worker_id[:8]}",
            )

    def refund(self, job_id: str) -> None:
        """Return escrowed credit to self after reject/timeout/failure."""
        with self._lock:
            self._require_open_escrow(job_id)
            self._append(
                kind="refund",
                job_id=job_id,
                entries=[
                    {"account": "escrow", "delta": -JOB_PRICE},
                    {"account": "self", "delta": JOB_PRICE},
                ],
                memo="refund: job was rejected, failed, or timed out",
            )

    def credit_for_work(self, job_id: str, requester_id: str) -> None:
        """Worker side: record one credit earned from the requester."""
        with self._lock:
            if any(r["kind"] == "work" for r in self._job_records(job_id)):
                raise LedgerError(f"job {job_id} already credited")
            self._append(
                kind="work",
                job_id=job_id,
                entries=[
                    {"account": requester_id, "delta": -JOB_PRICE},
                    {"account": "self", "delta": JOB_PRICE},
                ],
                memo=f"earned {JOB_PRICE} credit from requester {requester_id[:8]}",
            )

    def records(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._records]

    def _require_open_escrow(self, job_id: str) -> None:
        kinds = {r["kind"] for r in self._job_records(job_id)}
        if "escrow" not in kinds:
            raise LedgerError(f"no escrow found for job {job_id}")
        if kinds & {"settle", "refund"}:
            raise LedgerError(f"escrow for job {job_id} already closed")
