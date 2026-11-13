#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Jobs, a paper budget and duplicate detection for automated senders.

A person at the browser sees the paper and hears the printer. A program sees
nothing: it retries when an answer fails to arrive, and a wave of alerts prints
for as long as events keep coming. This module holds the line, with three
things in one file:

* **Key** - whoever sends a receipt with a ``key`` gets the same answer on a
  retry and no second sheet. Without a key there is no lock, otherwise a person
  could never print a second copy.
* **Job** - whoever announces a large job gets a contingent of its own and a
  counter to resume from after an interruption.
* **Budget** - measured in centimetres of paper, because that is the resource.
  An 80 m roll holds 8,000 of them.

Nothing is charged before the print. A receipt that never reached the printer
consumes no budget.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

FILENAME = "jobs.json"
STATE_VERSION = 1

# --- Budget -----------------------------------------------------------------
# The reference is the 80 m roll with its 8,000 cm. Pick your own numbers; these
# suit a device that also prints a daily weather receipt.
# There is deliberately no bound on a single job: one that is sized to just let
# the largest expected job through is a bound that fires the first time an
# estimate is off. The daily cap below is the bound; a job's declared cm is
# information, and binds nothing.
# All announced jobs of one day together. A whole roll: this bound is not meant
# to bother anyone, only to catch the one fault nothing else here would - a
# sender that opens fresh job identifiers in a loop. Without it, announced
# printing would have no ceiling at all.
JOB_DAY_LIMIT_CM = 8000.0
# Whoever announces nothing is in the free contingent; it applies per source IP.
# A daily bound sat next to this one until 19.09.2026 and was dropped: it only
# caught a slow drip that the hourly bound already keeps to a level nobody
# reaches in practice.
FREE_HOUR_LIMIT_CM = 300.0
# How long a job stays resumable and a key counts as spent. Longer is not
# needed: a book is printed within a day.
RETENTION_SECONDS = 7 * 24 * 3600
# Consumption events for the sliding windows. 25 h covers the calendar day at
# any hour of it.
EVENT_RETENTION_SECONDS = 25 * 3600
HOUR_SECONDS = 3600.0

# A 200 means "bytes handed over", not "on the paper": the printer buffers. A
# sheet counts as confirmed only once the printer has reported healthy again,
# and no sooner than this many seconds after the send, because before that it
# has not worked through the buffer.
CONFIRM_DELAY_SECONDS = 3.0
# The wait grows with the sheet: a fixed value would confirm a long receipt that
# is still running. The TM-T20IV datasheet says 200 mm/s; half of that is taken
# here as a reserve for spin-up and the cut.
CONFIRM_SPEED_CM_PER_SECOND = 10.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Refusal:
    """Why a receipt does not fit the contingent."""

    code: str
    scope: str
    used_cm: float
    limit_cm: float
    needed_cm: float
    retry_after_seconds: int | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        """Return the refusal as a JSON-friendly object.

        Returns:
            An object for the 'budget' field of the response.
        """
        return {
            "scope": self.scope,
            "used_cm": round(self.used_cm, 1),
            "limit_cm": self.limit_cm,
            "needed_cm": round(self.needed_cm, 1),
            "retry_after_seconds": self.retry_after_seconds,
        }


def _empty_state() -> dict[str, Any]:
    """Return the initial state, before anything was ever printed."""
    return {"version": STATE_VERSION, "jobs": {}, "keys": {}, "free": {}, "job_events": []}


def _seconds_to_midnight(now: float) -> int:
    """Report how much longer the daily window runs.

    Args:
        now: Point in time as Unix time.

    Returns:
        Seconds until local midnight, at least 1.
    """
    # Count to the next midnight rather than 86,400 seconds from the last one:
    # on the changeover days the local day is 23 or 25 hours long, and replace()
    # attaches the offset of the current moment.
    moment = datetime.fromtimestamp(now).astimezone()
    tomorrow = datetime.combine(moment.date() + timedelta(days=1), dt_time.min).astimezone()
    return max(1, int(tomorrow.timestamp() - now))


def _raised(confirmed: int | None, last: int | None) -> int | None:
    """Lift the high-water mark, never lower it.

    A reprint sets last_seq back to a smaller number. Without this guard the
    next confirmed sheet would claim less had been printed than was already
    confirmed.

    Args:
        confirmed: The high-water mark so far.
        last: Number of the sheet that was sent last.

    Returns:
        The new high-water mark.
    """
    if last is None:
        return confirmed
    if confirmed is None:
        return last
    return max(confirmed, last)


def confirm_delay(length_cm: float) -> float:
    """Return how long a sheet needs before it must have left the printer.

    Args:
        length_cm: Paper length of the sheet that was sent last.

    Returns:
        Seconds to wait before a healthy status report confirms it.
    """
    return max(CONFIRM_DELAY_SECONDS, length_cm / CONFIRM_SPEED_CM_PER_SECOND)


def _sum_events(events: list[list[float]], *, since: float, day: date | None) -> float:
    """Add up consumption events inside a window.

    Args:
        events: List of [Unix time, centimetres].
        since: Count only events from this point in time on.
        day: When given, additionally only events of that calendar day.

    Returns:
        The centimetres inside the window.
    """
    total = 0.0
    for stamp, centimetres in events:
        if stamp < since:
            continue
        if day is not None and datetime.fromtimestamp(stamp).date() != day:
            continue
        total += centimetres
    return total


class Ledger:
    """The state of all jobs, keys and contingents in one file.

    Every operation reads the file, changes it and writes it back atomically -
    like the history and for the same reason: there should be exactly one truth
    on disk, even when somebody edits it by hand in between. The file is small;
    a book leaves about 40 entries behind.

    Only the service writes it. The command line does not touch it: it prints
    for a person who has the paper in front of them.
    """

    def __init__(self, path: Path) -> None:
        """Set up the ledger without touching the file.

        Args:
            path: Target file in JSON format.
        """
        self.path = path
        self._lock = threading.Lock()

    # --- Reading and writing ------------------------------------------------

    def _load(self, now: float) -> dict[str, Any]:
        """Read the state and drop whatever has expired."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty_state()
        except OSError as exc:
            logger.error("ledger %s not readable: %s", self.path, exc)
            return _empty_state()

        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("ledger %s unreadable - starting over", self.path)
            return _empty_state()
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            logger.warning("ledger %s in a foreign format - starting over", self.path)
            return _empty_state()

        return self._prune(state, now)

    @staticmethod
    def _prune(state: dict[str, Any], now: float) -> dict[str, Any]:
        """Remove old jobs, keys and consumption events."""
        keep_from = now - RETENTION_SECONDS
        events_from = now - EVENT_RETENTION_SECONDS

        jobs = {
            key: value
            for key, value in state.get("jobs", {}).items()
            if value.get("updated_ts", 0) >= keep_from
        }
        keys = {
            key: value
            for key, value in state.get("keys", {}).items()
            if value.get("at_ts", 0) >= keep_from
        }
        free = {}
        for ip, events in state.get("free", {}).items():
            kept = [event for event in events if event[0] >= events_from]
            if kept:
                free[ip] = kept
        job_events = [event for event in state.get("job_events", []) if event[0] >= events_from]

        return {
            "version": STATE_VERSION,
            "jobs": jobs,
            "keys": keys,
            "free": free,
            "job_events": job_events,
        }

    def _save(self, state: dict[str, Any]) -> bool:
        """Write the file atomically, so a crash cannot halve it.

        Returns:
            True when the state reached the disk. False means the key and the
            budget of this print are lost and the next attempt would print
            again. The caller has to make that visible.
        """
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.error("ledger %s not writable: %s", self.path, exc)
            return False
        return True

    # --- Keys ---------------------------------------------------------------

    def recall(self, key: str) -> dict[str, Any] | None:
        """Look up what was already printed under this key.

        Args:
            key: The key from the request.

        Returns:
            The entry, or None when the key is new.
        """
        with self._lock:
            known: dict[str, Any] | None = self._load(time.time()).get("keys", {}).get(key)
        return known

    # --- Budget -------------------------------------------------------------

    def check(
        self,
        *,
        ip: str,
        job_id: str,
        length_cm: float,
        declared_cm: float | None,
        now: float | None = None,
    ) -> Refusal | None:
        """Check whether a receipt of this length still fits the contingent.

        Args:
            ip: Source IP of the sender.
            job_id: Job identifier; empty means the free contingent.
            length_cm: Paper length of the receipt in centimetres.
            declared_cm: Declared total extent from the job announcement.
            now: Point in time as Unix time; None takes the current one.

        Returns:
            The refusal, or None when the receipt fits.
        """
        moment = time.time() if now is None else now
        with self._lock:
            state = self._load(moment)
            if job_id:
                return self._check_job(state, job_id, length_cm, moment)
            return self._check_free(state, ip, length_cm, moment)

    def _check_job(
        self,
        state: dict[str, Any],
        job_id: str,
        length_cm: float,
        now: float,
    ) -> Refusal | None:
        """Check that the job is still open and the daily cap has room."""
        job = state["jobs"].get(job_id)
        if job and job.get("done"):
            return Refusal(
                code="job_done",
                scope="job",
                used_cm=job.get("printed_cm", 0.0),
                limit_cm=JOB_DAY_LIMIT_CM,
                needed_cm=length_cm,
                retry_after_seconds=None,
                message=f"Job {job_id} is closed.",
            )

        today = _sum_events(state["job_events"], since=0.0, day=datetime.fromtimestamp(now).date())
        if today + length_cm > JOB_DAY_LIMIT_CM:
            return Refusal(
                code="budget_exceeded",
                scope="day",
                used_cm=today,
                limit_cm=JOB_DAY_LIMIT_CM,
                needed_cm=length_cm,
                retry_after_seconds=_seconds_to_midnight(now),
                message=(
                    f"Announced jobs have used {today:.0f} of {JOB_DAY_LIMIT_CM:.0f} cm today."
                ),
            )
        return None

    def _check_free(
        self, state: dict[str, Any], ip: str, length_cm: float, now: float
    ) -> Refusal | None:
        """Check the free contingent of one source IP, per hour and per day."""
        events = state["free"].get(ip, [])
        hour = _sum_events(events, since=now - HOUR_SECONDS, day=None)
        if hour + length_cm > FREE_HOUR_LIMIT_CM:
            oldest = min((stamp for stamp, _ in events if stamp >= now - HOUR_SECONDS), default=now)
            return Refusal(
                code="budget_exceeded",
                scope="hour",
                used_cm=hour,
                limit_cm=FREE_HOUR_LIMIT_CM,
                needed_cm=length_cm,
                retry_after_seconds=max(1, int(oldest + HOUR_SECONDS - now)),
                message=(
                    f"{hour:.0f} of {FREE_HOUR_LIMIT_CM:.0f} cm of this hour are used. "
                    "An announced job has a contingent of its own."
                ),
            )

        return None

    # --- Charging -----------------------------------------------------------

    def commit(
        self,
        *,
        ip: str,
        job_id: str,
        seq: int | None,
        sheets: int | None,
        declared_cm: float | None,
        key: str,
        sha: str,
        length_cm: float,
        final: bool,
        printer_ready: bool,
        now: float | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Charge a printed receipt and carry the job forward.

        Args:
            ip: Source IP of the sender.
            job_id: Job identifier; empty means the free contingent.
            seq: Number of the receipt inside the job.
            sheets: Declared number of receipts, read on announcement only.
            declared_cm: Declared total extent, read on announcement only.
            key: Idempotency key; empty means no lock.
            sha: Checksum of the printed byte stream.
            length_cm: Paper length of the receipt in centimetres.
            final: True closes the job.
            printer_ready: Whether the printer reported healthy before the send.
            now: Point in time as Unix time; None takes the current one.

        Returns:
            The job state (None without a job) and whether the charge reached
            the disk.
        """
        moment = time.time() if now is None else now
        stamp = datetime.fromtimestamp(moment).astimezone().isoformat(timespec="seconds")
        with self._lock:
            state = self._load(moment)

            job: dict[str, Any] | None = None
            if job_id:
                job = state["jobs"].get(job_id)
                if job is None:
                    job = {
                        "id": job_id,
                        "created_at": stamp,
                        "sheets": sheets,
                        "declared_cm": declared_cm,
                        "printed_cm": 0.0,
                        "printed_sheets": 0,
                        "last_seq": None,
                        "last_ts": 0.0,
                        "last_cm": 0.0,
                        "confirmed_seq": None,
                        "interrupted": False,
                        "done": False,
                    }
                    state["jobs"][job_id] = job
                if job.get("interrupted"):
                    # After a fault the chain of evidence starts over - but only
                    # once the sender picks the doubtful sheet back up. If it
                    # prints straight past it, the note stands and confirmed_seq
                    # freezes: better a job that stays visibly unconfirmed than a
                    # high-water mark that silently covers the skipped sheet.
                    resumes = seq is None or job["last_seq"] is None or seq <= job["last_seq"]
                    if resumes:
                        job["interrupted"] = False
                elif printer_ready and moment - job["last_ts"] >= confirm_delay(
                    job.get("last_cm", 0.0)
                ):
                    # A healthy printer BEFORE this sheet confirms the one before
                    # it: its buffer was empty enough to carry on without a fault.
                    job["confirmed_seq"] = _raised(job["confirmed_seq"], job["last_seq"])
                job["printed_cm"] += length_cm
                job["printed_sheets"] += 1
                job["last_seq"] = seq
                job["last_ts"] = moment
                job["last_cm"] = length_cm
                job["updated_ts"] = moment
                job["updated_at"] = stamp
                if final:
                    job["done"] = True
                state["job_events"].append([moment, length_cm])
            else:
                state["free"].setdefault(ip, []).append([moment, length_cm])

            if key:
                state["keys"][key] = {
                    "job": job_id or None,
                    "seq": seq,
                    "sha": sha,
                    "at": stamp,
                    "at_ts": moment,
                    "length_cm": round(length_cm, 1),
                }

            recorded = self._save(state)
            return (dict(job) if job else None), recorded

    def fault(self, job_id: str, *, now: float | None = None) -> None:
        """Note that a receipt of this job did not go through cleanly.

        Out of paper, an open cover or an unreachable printer breaks the chain
        of evidence: the sheet sent last may be hanging out half printed, or not
        printed at all. While the note stands, no healthy status report confirms
        anything any more.

        When nothing is open at the time of the fault - nothing printed yet, or
        the last sheet already confirmed - no note is taken: the fault cannot
        have caught a sheet, and the receipt that failed was never charged.
        Without that exception a single 502 froze the confirmation for the rest
        of the job although nothing was in doubt.

        Args:
            job_id: Job identifier; empty does nothing.
            now: Point in time as Unix time; None takes the current one.
        """
        if not job_id:
            return
        moment = time.time() if now is None else now
        with self._lock:
            state = self._load(moment)
            job = state["jobs"].get(job_id)
            if job is None or job.get("interrupted"):
                return
            nothing_open = job.get("printed_sheets", 0) == 0 or (
                job.get("last_seq") is not None and job.get("confirmed_seq") == job.get("last_seq")
            )
            if nothing_open:
                return
            job["interrupted"] = True
            job["updated_ts"] = moment
            self._save(state)

    # --- Job state ----------------------------------------------------------

    def job(
        self, job_id: str, *, printer_ready: bool | None = None, now: float | None = None
    ) -> dict[str, Any] | None:
        """Read a job and confirm what the printer allows to be confirmed.

        Args:
            job_id: Job identifier.
            printer_ready: Freshly read printer state; True lifts confirmed_seq
                to last_seq once the buffer had time.
            now: Point in time as Unix time; None takes the current one.

        Returns:
            The job state, or None when there is no such job.
        """
        moment = time.time() if now is None else now
        with self._lock:
            state = self._load(moment)
            job = state["jobs"].get(job_id)
            if job is None:
                return None
            raised = _raised(job.get("confirmed_seq"), job.get("last_seq"))
            promotable = (
                printer_ready
                and not job.get("interrupted")
                and raised != job.get("confirmed_seq")
                and moment - job.get("last_ts", 0.0) >= confirm_delay(job.get("last_cm", 0.0))
            )
            if promotable:
                job["confirmed_seq"] = raised
                # updated_ts only on a real change: otherwise mere polling would
                # extend the retention and write the file on every request.
                job["updated_ts"] = moment
                self._save(state)
            return dict(job)

    def budget(self, ip: str, *, now: float | None = None) -> dict[str, Any]:
        """Report how much paper the contingents still allow.

        So a sender can call off a large job before starting it, rather than
        hitting the daily cap at sheet 30 with half a stack in hand.

        Args:
            ip: Source IP the free contingent applies to.
            now: Point in time as Unix time; None takes the current one.

        Returns:
            Use and limits of both contingents, all in centimetres.
        """
        moment = time.time() if now is None else now
        today = datetime.fromtimestamp(moment).date()
        with self._lock:
            state = self._load(moment)
        events = state["free"].get(ip, [])
        hour = _sum_events(events, since=moment - HOUR_SECONDS, day=None)
        oldest = min((stamp for stamp, _ in events if stamp >= moment - HOUR_SECONDS), default=None)
        return {
            "free": {
                "hour": {
                    "used_cm": round(hour, 1),
                    "limit_cm": FREE_HOUR_LIMIT_CM,
                    # When the oldest event drops out of the sliding window;
                    # 0 while there is nothing in it.
                    "frees_in_seconds": (
                        max(0, int(oldest + HOUR_SECONDS - moment)) if oldest is not None else 0
                    ),
                },
            },
            "jobs": {
                "day": {
                    "used_cm": round(_sum_events(state["job_events"], since=0.0, day=today), 1),
                    "limit_cm": JOB_DAY_LIMIT_CM,
                },
            },
            "day_resets_in_seconds": _seconds_to_midnight(moment),
        }

    def keys_of(self, job_id: str) -> list[dict[str, Any]]:
        """Return all keys of one job, oldest first.

        Args:
            job_id: Job identifier.

        Returns:
            The key entries, each extended by its own 'key' field.
        """
        with self._lock:
            state = self._load(time.time())
        entries = [
            {"key": key, **value}
            for key, value in state.get("keys", {}).items()
            if value.get("job") == job_id
        ]
        return sorted(entries, key=lambda item: item.get("at_ts", 0.0))
