# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the ledger: keys, jobs and the paper budget.

Time is passed in explicitly everywhere, so none of these tests has to wait.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import pytest

from receipt.jobs import (
    CONFIRM_DELAY_SECONDS,
    FREE_HOUR_LIMIT_CM,
    HOUR_SECONDS,
    JOB_DAY_LIMIT_CM,
    Ledger,
    _seconds_to_midnight,
    confirm_delay,
)


@pytest.fixture
def ledger(tmp_path):
    """A ledger on a throwaway file."""
    return Ledger(tmp_path / "jobs.json")


def charge(ledger, *, cm, ip="198.51.100.9", job_id="", key="", seq=None, now=None, **rest):
    """Book one printed receipt with the defaults these tests do not care about."""
    job, _ = ledger.commit(
        ip=ip,
        job_id=job_id,
        seq=seq,
        sheets=rest.get("sheets"),
        declared_cm=rest.get("declared_cm"),
        key=key,
        sha=rest.get("sha", "abcd1234"),
        length_cm=cm,
        final=rest.get("final", False),
        printer_ready=rest.get("printer_ready", True),
        now=now,
    )
    return job


def test_an_unknown_key_is_unknown(ledger):
    """Nothing is remembered before the first print."""
    assert ledger.recall("nope") is None


def test_a_key_remembers_what_it_printed(ledger):
    """That memory is what makes a retry harmless."""
    charge(ledger, cm=20.0, key="book-7", job_id="book", seq=7, sha="deadbeef")
    known = ledger.recall("book-7")
    assert known["sha"] == "deadbeef"
    assert known["seq"] == 7
    assert known["job"] == "book"
    assert known["length_cm"] == 20.0


def test_a_print_without_a_key_leaves_no_key(ledger):
    """No lock without one: a person may print a second copy."""
    charge(ledger, cm=20.0)
    assert ledger.recall("") is None


def test_the_free_contingent_runs_out_within_the_hour(ledger):
    """An alert wave is capped even when nobody announced anything."""
    now = time.time()
    charge(ledger, cm=FREE_HOUR_LIMIT_CM - 10, now=now)
    assert (
        ledger.check(ip="198.51.100.9", job_id="", length_cm=9.0, declared_cm=None, now=now) is None
    )
    refusal = ledger.check(ip="198.51.100.9", job_id="", length_cm=11.0, declared_cm=None, now=now)
    assert refusal is not None
    assert refusal.code == "budget_exceeded"
    assert refusal.scope == "hour"
    assert refusal.retry_after_seconds > 0


def test_the_hourly_window_slides(ledger):
    """What was printed 61 minutes ago no longer stands in the way."""
    now = time.time()
    charge(ledger, cm=FREE_HOUR_LIMIT_CM, now=now - HOUR_SECONDS - 60)
    assert (
        ledger.check(ip="198.51.100.9", job_id="", length_cm=20.0, declared_cm=None, now=now)
        is None
    )


def test_the_free_contingent_is_per_source(ledger):
    """One noisy sender must not silence the others."""
    now = time.time()
    charge(ledger, cm=FREE_HOUR_LIMIT_CM, ip="198.51.100.9", now=now)
    assert ledger.check(ip="198.51.100.9", job_id="", length_cm=5.0, declared_cm=None, now=now)
    assert (
        ledger.check(ip="198.51.100.8", job_id="", length_cm=5.0, declared_cm=None, now=now) is None
    )


def test_the_free_contingent_has_no_daily_bound(ledger):
    """Only the hour bounds free printing; a daily one was dropped 19.09.2026.

    It caught nothing the hourly bound did not, and a level it would have
    caught - three full hours of printing - nobody reaches by accident.
    """
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    now = noon.timestamp()
    for hour in (4, 3, 2):
        charge(ledger, cm=FREE_HOUR_LIMIT_CM - 20, now=now - hour * HOUR_SECONDS)
    # The hourly window is empty by now, and nothing else stands in the way.
    assert (
        ledger.check(ip="198.51.100.9", job_id="", length_cm=20.0, declared_cm=None, now=now)
        is None
    )


def test_an_announced_job_does_not_touch_the_free_contingent(ledger):
    """The whole point of announcing: a book is not an alert wave."""
    now = time.time()
    charge(ledger, cm=FREE_HOUR_LIMIT_CM, job_id="book", declared_cm=1000.0, now=now)
    assert (
        ledger.check(ip="198.51.100.9", job_id="", length_cm=20.0, declared_cm=None, now=now)
        is None
    )


def test_a_declared_extent_binds_nothing(ledger):
    """`cm` is information the sender gives about itself, not a contract.

    A bound per job was dropped on 19.09.2026: sized to just let the largest
    expected book through, it fired the first time an estimate was off. The
    daily cap is the bound.
    """
    now = time.time()
    charge(ledger, cm=1200.0, job_id="book", declared_cm=800.0, now=now)
    assert (
        ledger.check(ip="203.0.113.1", job_id="book", length_cm=90.0, declared_cm=None, now=now)
        is None
    )


def test_all_jobs_of_a_day_share_a_cap(ledger):
    """Otherwise a new job identifier would be a fresh roll every time."""
    now = time.time()
    for number in range(3):
        charge(ledger, cm=JOB_DAY_LIMIT_CM / 3, job_id=f"book-{number}", now=now)
    refusal = ledger.check(
        ip="203.0.113.1", job_id="book-4", length_cm=20.0, declared_cm=None, now=now
    )
    assert refusal is not None
    assert refusal.scope == "day"
    assert refusal.limit_cm == JOB_DAY_LIMIT_CM


def test_a_closed_job_takes_nothing_more(ledger):
    """'final' is a statement, and it holds."""
    now = time.time()
    charge(ledger, cm=20.0, job_id="book", final=True, now=now)
    refusal = ledger.check(
        ip="203.0.113.1", job_id="book", length_cm=20.0, declared_cm=None, now=now
    )
    assert refusal is not None
    assert refusal.code == "job_done"


def test_a_job_counts_its_sheets_and_its_paper(ledger):
    """The sender reads its own progress off this."""
    now = time.time()
    for number in (1, 2, 3):
        charge(ledger, cm=10.0, job_id="book", seq=number, now=now + number * 10)
    job = ledger.job("book", now=now + 40)
    assert job["printed_sheets"] == 3
    assert job["printed_cm"] == 30.0
    assert job["last_seq"] == 3


def test_a_sheet_is_confirmed_by_the_next_healthy_look(ledger):
    """A 200 means bytes handed over; the printer buffers.

    Only when it reports healthy again - and late enough to have worked through
    the buffer - is the sheet before that provably on the paper.
    """
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    assert ledger.job("book", now=now)["confirmed_seq"] is None

    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + CONFIRM_DELAY_SECONDS + 1)
    job = ledger.job("book", now=now + CONFIRM_DELAY_SECONDS + 1)
    assert job["confirmed_seq"] == 1
    assert job["last_seq"] == 2


def test_a_look_too_soon_confirms_nothing(ledger):
    """The buffer needs time; a query a millisecond later proves nothing."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    assert ledger.job("book", printer_ready=True, now=now + 0.1)["confirmed_seq"] is None
    later = ledger.job("book", printer_ready=True, now=now + CONFIRM_DELAY_SECONDS + 1)
    assert later["confirmed_seq"] == 1


def test_an_unhealthy_printer_confirms_nothing(ledger):
    """Out of paper is exactly when a sender must not keep counting."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    charge(
        ledger,
        cm=10.0,
        job_id="book",
        seq=2,
        printer_ready=False,
        now=now + CONFIRM_DELAY_SECONDS + 1,
    )
    assert ledger.job("book", now=now + 10)["confirmed_seq"] is None


def test_the_keys_of_a_job_come_back_in_order(ledger):
    """After an interruption the sender wants to see what got through."""
    now = time.time()
    for number in (1, 2, 3):
        charge(ledger, cm=10.0, job_id="book", seq=number, key=f"k{number}", now=now + number)
    charge(ledger, cm=10.0, job_id="other", key="x", now=now + 9)
    assert [entry["key"] for entry in ledger.keys_of("book")] == ["k1", "k2", "k3"]


def test_an_unknown_job_is_none(ledger):
    """A sender asking about a job that expired gets a clear answer."""
    assert ledger.job("never-existed") is None


def test_a_broken_file_does_not_stop_the_printing(ledger):
    """The ledger is a guard rail, not a gate; a corrupt file starts over."""
    ledger.path.write_text("{not json", encoding="utf-8")
    assert ledger.recall("anything") is None
    assert ledger.check(ip="203.0.113.1", job_id="", length_cm=10.0, declared_cm=None) is None


def test_expired_jobs_and_keys_fall_out(ledger):
    """Seven days on, a book is printed and a key is free again."""
    long_ago = time.time() - 8 * 24 * 3600
    charge(ledger, cm=10.0, job_id="old", key="old-1", now=long_ago)
    assert ledger.recall("old-1") is None
    assert ledger.job("old") is None


def test_the_confirmation_wait_grows_with_the_sheet(ledger):
    """A fixed three seconds would confirm a long receipt that is still running.

    MAX_TEXT_CHARS allows about 130 cm, which takes well over ten seconds on a
    device rated at 200 mm/s.
    """
    assert confirm_delay(5.0) == CONFIRM_DELAY_SECONDS
    assert confirm_delay(20.0) == CONFIRM_DELAY_SECONDS
    assert confirm_delay(130.0) > 12.0

    now = time.time()
    charge(ledger, cm=130.0, job_id="long", seq=1, now=now)
    charge(ledger, cm=10.0, job_id="long", seq=2, now=now + 5)
    # Five seconds after a 130 cm sheet the printer is still printing it.
    assert ledger.job("long", now=now + 5)["confirmed_seq"] is None
    charge(ledger, cm=10.0, job_id="long", seq=3, now=now + 40)
    assert ledger.job("long", now=now + 40)["confirmed_seq"] == 2


def test_a_fault_stops_every_later_confirmation(ledger):
    """The case confirmed_seq exists for.

    Paper runs out during sheet 2. Somebody reloads, the printer reports healthy
    again - and prints the rest of its buffer while doing so. Sheet 2 must not
    become confirmed on the strength of that.
    """
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 10)
    ledger.fault("book", now=now + 12)

    job = ledger.job("book", printer_ready=True, now=now + 600)
    assert job["interrupted"] is True
    assert job["confirmed_seq"] == 1
    assert job["last_seq"] == 2


def test_printing_again_clears_the_fault_without_confirming_the_doubtful_sheet(ledger):
    """After the reprint the chain of evidence starts over, it does not resume."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 10)
    ledger.fault("book", now=now + 12)

    # The sender reprints the doubtful sheet 2 under a fresh key.
    job = charge(ledger, cm=10.0, job_id="book", seq=2, key="b-2-r2", now=now + 600)
    assert job["interrupted"] is False
    assert job["confirmed_seq"] == 1

    job = charge(ledger, cm=10.0, job_id="book", seq=3, now=now + 700)
    assert job["confirmed_seq"] == 2


def test_a_fault_on_a_job_that_does_not_exist_is_harmless(ledger):
    """The printer can be out of paper before a job has ever printed."""
    ledger.fault("never-started")
    ledger.fault("")
    assert ledger.job("never-started") is None


def test_an_unwritable_ledger_is_reported_not_swallowed(ledger, tmp_path):
    """The paper is out; the sender has to learn that the charge is not.

    Otherwise it answers ok, the key was never stored, and the retry prints a
    second sheet with no budget having been spent on either.
    """
    assert (
        ledger.commit(
            ip="203.0.113.1",
            job_id="",
            seq=None,
            sheets=None,
            declared_cm=None,
            key="k1",
            sha="abcd1234",
            length_cm=10.0,
            final=False,
            printer_ready=True,
        )[1]
        is True
    )

    tmp_path.chmod(0o500)
    try:
        _, recorded = ledger.commit(
            ip="203.0.113.1",
            job_id="",
            seq=None,
            sheets=None,
            declared_cm=None,
            key="k2",
            sha="abcd1234",
            length_cm=10.0,
            final=False,
            printer_ready=True,
        )
    finally:
        tmp_path.chmod(0o700)
    assert recorded is False
    assert ledger.recall("k2") is None


def test_the_daily_window_survives_the_clock_change():
    """Local days are 23 or 25 hours long twice a year.

    Counting 86,400 seconds from the past midnight told a sender to sleep an
    hour too long in spring and woke it an hour early in autumn, straight into
    another 429.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Berlin"
    time.tzset()
    try:
        spring = datetime(2026, 3, 29, 1, 30).timestamp()
        autumn = datetime(2026, 10, 25, 0, 30).timestamp()
        assert _seconds_to_midnight(spring) == pytest.approx(21.5 * 3600, abs=5)
        assert _seconds_to_midnight(autumn) == pytest.approx(24.5 * 3600, abs=5)
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_printing_past_a_doubtful_sheet_freezes_the_confirmation(ledger):
    """The high-water mark must not silently cover a sheet nobody reprinted.

    A 502 in the middle of a job leaves the sheet before it doubtful. A sender
    that ignores the resume rule and simply carries on would otherwise get a
    `confirmed_seq` that steps right over it.
    """
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    ledger.fault("book", now=now + 5)

    # The sender ignores step 1 and prints on.
    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 10)
    charge(ledger, cm=10.0, job_id="book", seq=3, now=now + 20)
    job = ledger.job("book", printer_ready=True, now=now + 60)
    assert job["interrupted"] is True
    assert job["confirmed_seq"] is None


def test_resuming_at_the_doubtful_sheet_clears_the_note(ledger):
    """Picking the sheet back up is what restarts the chain of evidence."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    ledger.fault("book", now=now + 5)

    job = charge(ledger, cm=10.0, job_id="book", seq=1, key="b-1-r2", now=now + 10)
    assert job["interrupted"] is False
    job = charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 20)
    assert job["confirmed_seq"] == 1


def test_the_confirmation_never_moves_backwards(ledger):
    """A reprint sets last_seq back; the mark must not follow it down."""
    now = time.time()
    for number in (1, 2, 3):
        charge(ledger, cm=10.0, job_id="book", seq=number, now=now + number * 10)
    assert ledger.job("book", printer_ready=True, now=now + 60)["confirmed_seq"] == 3

    charge(ledger, cm=10.0, job_id="book", seq=2, key="b-2-r2", now=now + 70)
    job = ledger.job("book", printer_ready=True, now=now + 120)
    assert job["last_seq"] == 2
    assert job["confirmed_seq"] == 3


def test_the_budget_can_be_read_before_a_job_starts(ledger):
    """A book of 11 m should be called off before sheet 1, not at sheet 30."""
    now = time.time()
    charge(ledger, cm=100.0, ip="198.51.100.9", now=now)
    charge(ledger, cm=250.0, job_id="book", now=now)

    budget = ledger.budget("198.51.100.9", now=now)
    assert budget["free"]["hour"]["used_cm"] == 100.0
    assert budget["free"]["hour"]["limit_cm"] == FREE_HOUR_LIMIT_CM
    assert 0 < budget["free"]["hour"]["frees_in_seconds"] <= HOUR_SECONDS
    assert "day" not in budget["free"]
    assert budget["jobs"]["day"]["used_cm"] == 250.0
    assert budget["jobs"]["day"]["limit_cm"] == JOB_DAY_LIMIT_CM
    assert "limit_cm_per_job" not in budget["jobs"]
    assert budget["day_resets_in_seconds"] > 0

    # The free contingent is per source IP, so another sender sees its own.
    assert ledger.budget("198.51.100.8", now=now)["free"]["hour"]["used_cm"] == 0.0
    assert ledger.budget("198.51.100.8", now=now)["jobs"]["day"]["used_cm"] == 250.0


def test_a_fault_with_nothing_open_leaves_no_note(ledger):
    """A fault can only catch a sheet that is not confirmed yet.

    Without this, one 502 after a confirmed sheet froze the confirmation for the
    rest of the job - and the receipt that failed was never charged either, so
    nothing at all was in doubt.
    """
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 10)
    # A status query confirms sheet 2: from here everything sent is accounted for.
    assert ledger.job("book", printer_ready=True, now=now + 20)["confirmed_seq"] == 2

    ledger.fault("book", now=now + 25)
    job = ledger.job("book", now=now + 25)
    assert job["interrupted"] is False
    assert job["confirmed_seq"] == 2


def test_a_fault_before_the_first_sheet_leaves_no_note(ledger):
    """Out of paper at sheet 1 has nothing to cast doubt on."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    ledger.fault("other", now=now)
    assert ledger.job("other") is None


def test_a_fault_with_an_unconfirmed_sheet_still_takes_the_note(ledger):
    """The case the note exists for must keep working."""
    now = time.time()
    charge(ledger, cm=10.0, job_id="book", seq=1, now=now)
    charge(ledger, cm=10.0, job_id="book", seq=2, now=now + 10)
    ledger.fault("book", now=now + 12)
    job = ledger.job("book", printer_ready=True, now=now + 60)
    assert job["interrupted"] is True
    assert job["confirmed_seq"] == 1
