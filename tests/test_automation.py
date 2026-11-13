# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the contract automated senders rely on.

Every test here counts sheets at the fake printer, because that is the only
number that costs paper. The most important ones are the two about the key: a
retry must not print, and a deliberate reprint must.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from conftest import HEALTHY, OUT_OF_PAPER, FakePrinter

from receipt.audit import FILENAME as AUDIT_FILENAME
from receipt.jobs import CONFIRM_DELAY_SECONDS, FREE_HOUR_LIMIT_CM
from receipt.printer import open_roll
from receipt.web import create_app


def test_a_person_may_print_the_same_text_twice(printing_app):
    """Without a key there is no lock; two clicks are two copies."""
    client, printer, _ = printing_app
    for _ in range(2):
        assert client.post("/api/print", json={"text": "Coffee"}).get_json()["ok"] is True
    assert len(printer.jobs) == 2


def test_a_retry_with_the_same_key_costs_no_paper(printing_app):
    """The central promise for a sender that repeats on a timeout."""
    client, printer, _ = printing_app
    first = client.post("/api/print", json={"text": "Page 1", "key": "book-1"}).get_json()
    second = client.post("/api/print", json={"text": "Page 1", "key": "book-1"}).get_json()
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["ok"] is True
    assert len(printer.jobs) == 1


def test_a_deliberate_reprint_is_possible(printing_app):
    """A second key means a second sheet, of the same text."""
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Page 1", "key": "book-1"})
    body = client.post("/api/print", json={"text": "Page 1", "key": "book-1-again"}).get_json()
    assert body["duplicate"] is False
    assert len(printer.jobs) == 2


def test_a_key_reused_for_other_text_is_a_conflict(printing_app):
    """Silently printing the new text would hide a bug in the sender."""
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Page 1", "key": "book-1"})
    response = client.post("/api/print", json={"text": "Page 2", "key": "book-1"})
    assert response.status_code == 409
    assert response.get_json()["error"] == "key_conflict"
    assert len(printer.jobs) == 1


def test_an_announced_job_reports_its_progress(printing_app):
    """Sheet count, paper used and what is left, on every answer."""
    client, _, _ = printing_app
    body = client.post(
        "/api/print",
        json={"text": "Pericope 1", "job": "eph", "seq": 1, "sheets": 38, "cm": 400, "key": "e1"},
    ).get_json()
    job = body["job"]
    assert job["id"] == "eph"
    assert job["sheets"] == 38
    assert job["declared_cm"] == 400.0
    assert job["printed_sheets"] == 1
    assert job["printed_cm"] > 0
    assert job["last_seq"] == 1


def test_a_job_does_not_flood_the_history(printing_app):
    """Twenty entries are for a person, not for a book."""
    client, _, _ = printing_app
    for number in range(1, 4):
        client.post("/api/print", json={"text": f"Pericope {number}", "job": "eph", "seq": number})
    client.post("/api/print", json={"text": "By hand"})
    labels = [entry["label"] for entry in client.get("/api/history").get_json()["entries"]]
    assert labels == ["By hand"]


def test_a_sender_can_opt_out_of_the_history(printing_app):
    """Not every automated sender announces a job."""
    client, _, _ = printing_app
    client.post("/api/print", json={"text": "Alert", "history": False})
    assert client.get("/api/history").get_json()["entries"] == []


def test_a_sender_finds_out_where_it_stopped(printing_app):
    """After an interruption: which sheet is provably on the paper."""
    client, _, _ = printing_app
    for number in (1, 2):
        client.post(
            "/api/print",
            json={"text": f"Page {number}", "job": "eph", "seq": number, "key": f"e{number}"},
        )
    time.sleep(CONFIRM_DELAY_SECONDS + 0.2)
    body = client.get("/api/job/eph").get_json()
    assert body["ok"] is True
    assert body["job"]["last_seq"] == 2
    # The printer reported healthy well after the last send, so everything up to
    # the last sheet counts as confirmed.
    assert body["job"]["confirmed_seq"] == 2
    assert [entry["key"] for entry in body["keys"]] == ["e1", "e2"]


def test_an_unknown_job_is_a_404(printing_app):
    """A sender that lost its state gets a clear answer, not an empty job."""
    client, _, _ = printing_app
    response = client.get("/api/job/never-started")
    assert response.status_code == 404
    assert response.get_json()["error"] == "job_unknown"


def test_a_job_can_be_closed(printing_app):
    """'final' ends it, and nothing more goes on that job."""
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Last page", "job": "eph", "seq": 1, "final": True})
    response = client.post("/api/print", json={"text": "One more", "job": "eph", "seq": 2})
    assert response.status_code == 409
    assert response.get_json()["error"] == "job_done"
    assert len(printer.jobs) == 1


def test_an_unannounced_flood_is_braked(printing_app):
    """What an alert wave runs into."""
    client, printer, _ = printing_app
    long_text = "Lorem ipsum dolor sit amet. " * 400
    refusal = None
    for number in range(20):
        body = client.post("/api/print", json={"text": f"{long_text} {number}"}).get_json()
        if not body["ok"]:
            refusal = body
            break
    assert refusal is not None
    assert refusal["error"] == "budget_exceeded"
    assert refusal["budget"]["limit_cm"] == FREE_HOUR_LIMIT_CM
    assert len(printer.jobs) < 20


def test_a_budget_refusal_says_when_to_come_back(printing_app):
    """429 with a Retry-After a sender can sleep on."""
    client, _, _ = printing_app
    long_text = "Lorem ipsum dolor sit amet. " * 400
    for number in range(20):
        response = client.post("/api/print", json={"text": f"{long_text} {number}"})
        if response.status_code == 429:
            assert int(response.headers["Retry-After"]) > 0
            return
    raise AssertionError("the free contingent never ran out")


def test_an_announced_job_keeps_its_own_contingent(printing_app):
    """A book must not be braked by, or brake, the alerts."""
    client, _, _ = printing_app
    long_text = "Lorem ipsum dolor sit amet. " * 400
    for number in range(20):
        if not client.post("/api/print", json={"text": f"{long_text} {number}"}).get_json()["ok"]:
            break
    body = client.post(
        "/api/print", json={"text": long_text, "job": "book", "seq": 1, "cm": 1000}
    ).get_json()
    assert body["ok"] is True


def test_an_empty_roll_refuses_before_anything_is_sent(printing_app):
    """Bytes into a printer with no paper are bytes nobody can account for."""
    client, printer, _ = printing_app
    printer.status = list(OUT_OF_PAPER)
    response = client.post("/api/print", json={"text": "Goes nowhere"})
    assert response.status_code == 409
    body = response.get_json()
    assert body["error"] == "printer_not_ready"
    assert body["printer"]["paper_end"] is True
    assert printer.jobs == []

    printer.status = list(HEALTHY)
    assert client.post("/api/print", json={"text": "Goes now"}).get_json()["ok"] is True


def test_an_empty_roll_costs_no_budget(printing_app):
    """Nothing was printed, so nothing is charged."""
    client, printer, _ = printing_app
    printer.status = list(OUT_OF_PAPER)
    client.post("/api/print", json={"text": "x", "job": "book", "seq": 1, "cm": 100})
    printer.status = list(HEALTHY)
    body = client.post(
        "/api/print", json={"text": "x", "job": "book", "seq": 1, "cm": 100}
    ).get_json()
    assert body["job"]["printed_sheets"] == 1


def test_the_lamp_sees_the_paper(printing_app):
    """What the interface shows now comes from the device, not from a port."""
    client, printer, _ = printing_app
    body = client.get("/api/printer").get_json()
    assert body["printer"]["ready"] is True

    printer.status = list(OUT_OF_PAPER)
    body = client.get("/api/printer").get_json()
    assert body["reachable"] is True
    assert body["printer"]["ready"] is False
    assert body["printer"]["reason"]


def test_bad_fields_are_refused_before_any_paper(printing_app):
    """A sender with a bug should hear about it, not print."""
    client, printer, _ = printing_app
    for payload in (
        {"text": "x", "seq": -1},
        {"text": "x", "sheets": 0},
        {"text": "x", "cm": 0},
        {"text": "x", "key": "k" * 200},
    ):
        response = client.post("/api/print", json=payload)
        assert response.status_code == 400
        assert response.get_json()["error"] == "bad_request"
    assert printer.jobs == []


def test_every_attempt_lands_in_the_audit_record(printing_app):
    """The history forgets; this does not."""
    client, printer, state_dir = printing_app
    client.post("/api/print", json={"text": "Page 1", "job": "eph", "seq": 1, "key": "e1"})
    client.post("/api/print", json={"text": "Page 1", "job": "eph", "seq": 1, "key": "e1"})
    printer.status = list(OUT_OF_PAPER)
    client.post("/api/print", json={"text": "Page 2", "job": "eph", "seq": 2})

    records = [
        json.loads(line)
        for line in (state_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert [record["result"] for record in records] == ["ok", "duplicate", "printer_not_ready"]
    assert records[0]["payload"]["job"] == "eph"
    assert records[0]["payload"]["sha"]
    assert all(record["src_ip"] for record in records)


def test_the_resume_after_an_empty_roll_leaves_no_gap(printing_app):
    """The scenario confirmed_seq was built for, end to end.

    Sheet 2 goes out, the roll ends, somebody reloads. The old key for sheet 2
    is spent - answering `duplicate` there would leave a hole in the book, so
    the service refuses it and names the way out.
    """
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Sheet 1", "job": "b", "seq": 1, "key": "b-1"})
    time.sleep(CONFIRM_DELAY_SECONDS + 0.2)
    client.post("/api/print", json={"text": "Sheet 2", "job": "b", "seq": 2, "key": "b-2"})

    printer.status = list(OUT_OF_PAPER)
    assert (
        client.post(
            "/api/print", json={"text": "Sheet 3", "job": "b", "seq": 3, "key": "b-3"}
        ).status_code
        == 409
    )
    printer.status = list(HEALTHY)

    job = client.get("/api/job/b").get_json()["job"]
    assert job["interrupted"] is True
    assert job["confirmed_seq"] == 1
    assert job["last_seq"] == 2

    spent = client.post("/api/print", json={"text": "Sheet 2", "job": "b", "seq": 2, "key": "b-2"})
    assert spent.status_code == 409
    assert spent.get_json()["error"] == "key_spent"

    again = client.post(
        "/api/print", json={"text": "Sheet 2", "job": "b", "seq": 2, "key": "b-2-r2"}
    ).get_json()
    assert again["ok"] is True
    assert again["duplicate"] is False
    assert again["job"]["interrupted"] is False
    assert len(printer.jobs) == 3


def test_a_plain_timeout_retry_is_still_a_duplicate(printing_app):
    """Without a fault in between, the key keeps doing its job.

    The distinction matters: a retry after a lost answer must not print, a
    retry after a fault must.
    """
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Sheet 1", "job": "b", "seq": 1, "key": "b-1"})
    body = client.post(
        "/api/print", json={"text": "Sheet 1", "job": "b", "seq": 1, "key": "b-1"}
    ).get_json()
    assert body["duplicate"] is True
    assert len(printer.jobs) == 1


def test_a_field_of_the_wrong_type_is_refused_not_ignored(printing_app):
    """Each of these used to answer 200 and quietly do something else."""
    client, printer, _ = printing_app
    for payload in (
        {"text": "x", "seq": "1"},
        {"text": "x", "final": "true"},
        {"text": "x", "cm": "800"},
        {"text": "x", "key": 12345},
        {"text": "x", "sheets": 3.5},
        {"text": "x", "history": "no"},
    ):
        response = client.post("/api/print", json=payload)
        assert response.status_code == 400, payload
        assert response.get_json()["error"] == "bad_request", payload
    assert printer.jobs == []


def test_a_non_finite_length_is_refused(printing_app):
    """NaN compares False against every limit, which switched the budget off."""
    client, printer, _ = printing_app
    for body in ('{"text":"x","cm":NaN}', '{"text":"x","cm":Infinity}'):
        response = client.post("/api/print", data=body, content_type="application/json")
        assert response.status_code == 400
        assert response.get_json()["error"] == "bad_request"
    assert printer.jobs == []


def test_a_job_may_exceed_what_it_declared(printing_app):
    """`cm` is information, not a bound; only the daily cap stops a job."""
    client, _, _ = printing_app
    long_text = "Lorem ipsum dolor sit amet. " * 400
    client.post("/api/print", json={"text": long_text, "job": "small", "seq": 1, "cm": 10})
    body = client.post(
        "/api/print", json={"text": long_text + " 2", "job": "small", "seq": 2}
    ).get_json()
    assert body["ok"] is True


def test_a_job_identifier_stays_reachable_in_a_url(printing_app):
    """A slash in the identifier could never be read back from /api/job/<id>."""
    client, printer, _ = printing_app
    response = client.post("/api/print", json={"text": "x", "job": "book/1", "seq": 1})
    assert response.status_code == 400
    assert response.get_json()["error"] == "bad_request"
    assert printer.jobs == []


def test_even_flasks_own_refusals_are_json(printing_app):
    """A sender that reads ok and error must not meet an HTML page."""
    client, _, _ = printing_app
    for response in (
        client.get("/api/job/"),
        client.post("/api/job/x"),
        client.get("/api/print"),
        client.get("/api/nothing-here"),
    ):
        assert response.content_type.startswith("application/json")
        body = response.get_json()
        assert body["ok"] is False
        assert body["error"] in ("not_found", "method_not_allowed")


def test_an_unexpected_fault_is_json_too(tmp_path, fake_printer):
    """A 500 with a traceback page would break every sender at once."""
    app = create_app(printer=f"127.0.0.1:{fake_printer.port}", state_dir=tmp_path)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("something went wrong")

    response = app.test_client().get("/api/boom")
    assert response.status_code == 500
    assert response.content_type.startswith("application/json")
    assert response.get_json()["error"] == "internal"


def test_a_print_that_was_not_charged_says_so(printing_app):
    """Paper out, charge not written: the key no longer protects anything."""
    client, printer, state_dir = printing_app
    assert client.post("/api/print", json={"text": "x", "key": "k1"}).get_json()["recorded"] is True

    state_dir.chmod(0o500)
    try:
        body = client.post("/api/print", json={"text": "y", "key": "k2"}).get_json()
    finally:
        state_dir.chmod(0o700)
    assert body["ok"] is True
    assert body["recorded"] is False
    assert len(printer.jobs) == 2


def test_two_senders_at_once_get_one_receipt_each(printing_app):
    """One worker, four threads: the print path has to serialise itself.

    Without the lock two requests could pass the same gap in a contingent, and
    their byte streams could interleave on the way to the device.
    """
    client, printer, _ = printing_app
    answers: list[dict] = []
    barrier = threading.Barrier(4)

    def send(number: int) -> None:
        barrier.wait()
        answers.append(client.post("/api/print", json={"text": f"Sheet {number}"}).get_json())

    threads = [threading.Thread(target=send, args=(number,)) for number in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert [body["ok"] for body in answers] == [True] * 4
    assert len(printer.jobs) == 4
    # Every receipt arrived whole: four distinct checksums, four distinct byte
    # streams, none of them spliced into another.
    assert len({body["sha"] for body in answers}) == 4
    assert len({bytes(job) for job in printer.jobs}) == 4


def test_a_502_leaves_the_key_free_but_marks_the_job(printing_app, tmp_path):
    """Nothing was charged, so the same key prints - but the job is doubtful.

    The sheet *before* the failed one was in the printer when the connection
    broke. Whether it came out whole is exactly what nobody knows.
    """
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Sheet 1", "job": "b", "seq": 1, "key": "b-1"})
    printer.close()

    response = client.post(
        "/api/print", json={"text": "Sheet 2", "job": "b", "seq": 2, "key": "b-2"}
    )
    assert response.status_code == 502
    assert response.get_json()["error"] == "printer_unreachable"

    job = client.get("/api/job/b").get_json()
    assert job["job"]["interrupted"] is True
    assert job["job"]["last_seq"] == 1
    # The failed sheet was never charged, so its key is still free.
    assert [entry["key"] for entry in job["keys"]] == ["b-1"]


def test_the_printer_endpoint_reports_the_budget(printing_app):
    """A sender asks once before it starts, not after thirty sheets."""
    client, _, _ = printing_app
    client.post("/api/print", json={"text": "x" * 500})
    budget = client.get("/api/printer").get_json()["budget"]
    assert budget["free"]["hour"]["used_cm"] > 0
    assert budget["free"]["hour"]["limit_cm"] == FREE_HOUR_LIMIT_CM
    assert budget["jobs"]["day"]["limit_cm"] > 0
    assert budget["day_resets_in_seconds"] > 0


def test_a_502_after_a_confirmed_sheet_does_not_freeze_the_job(printing_app):
    """A fault that caught nothing must not cost the rest of the job.

    The sender polls the job (its own step 4), which confirms the last sheet.
    The next receipt then fails with a 502 - but everything sent so far is
    accounted for and the failed receipt was never charged, so nothing is in
    doubt and the job has to carry on confirming.
    """
    client, printer, _ = printing_app
    client.post("/api/print", json={"text": "Sheet 1", "job": "b", "seq": 1, "key": "b-1"})
    time.sleep(CONFIRM_DELAY_SECONDS + 0.2)
    client.post("/api/print", json={"text": "Sheet 2", "job": "b", "seq": 2, "key": "b-2"})
    time.sleep(CONFIRM_DELAY_SECONDS + 0.2)
    assert client.get("/api/job/b").get_json()["job"]["confirmed_seq"] == 2

    printer.close()
    assert (
        client.post(
            "/api/print", json={"text": "Sheet 3", "job": "b", "seq": 3, "key": "b-3"}
        ).status_code
        == 502
    )

    job = client.get("/api/job/b").get_json()["job"]
    assert job["interrupted"] is False
    assert job["confirmed_seq"] == 2

    # The sender carries on at 3, exactly as the flow says, and the mark climbs.
    revived = FakePrinter()
    try:
        client.application.config["PRINTER_PORT"] = revived.port
        for number in (3, 4):
            client.post(
                "/api/print",
                json={"text": f"Sheet {number}", "job": "b", "seq": number, "key": f"b-{number}"},
            )
            time.sleep(CONFIRM_DELAY_SECONDS + 0.2)
        job = client.get("/api/job/b").get_json()["job"]
        assert job["interrupted"] is False
        assert job["confirmed_seq"] == 4
    finally:
        revived.close()
    # Every sheet exactly once: the 502 cost no paper and no reprint.
    assert len(printer.jobs) + len(revived.jobs) == 4


def test_a_roll_job_leaves_no_gap_between_receipts(printing_app):
    """A book should come out as one roll, not as a stack of 40 receipts.

    A full cut feeds nine lines before cutting - 3.3 cm of blank paper each
    time, over a metre across a book.
    """
    client, printer, state_dir = printing_app
    text = "# Pericope 4\n\nA paragraph long enough to have a length."
    full = client.post("/api/preview", json={"text": text}).get_json()
    plain = client.post("/api/preview", json={"text": text, "cut": False}).get_json()
    assert full["cut"] == "full"
    assert plain["cut"] == "none"
    assert full["length_cm"] - plain["length_cm"] == pytest.approx(3.3, abs=0.2)

    for number in (1, 2, 3):
        body = client.post(
            "/api/print",
            json={"text": f"Pericope {number}", "job": "book", "seq": number, "cut": False},
        ).get_json()
        assert body["ok"] is True
        assert body["cut"] == "none"
    # Nothing in the stream cuts, and the service knows the roll is standing.
    assert not any(b"\x1dV" in job for job in printer.jobs)
    assert open_roll(state_dir) == "book"


def test_a_foreign_receipt_is_severed_from_a_standing_roll(printing_app):
    """Otherwise the weather receipt hangs off last night's aborted book."""
    client, printer, state_dir = printing_app
    client.post("/api/print", json={"text": "Pericope 1", "job": "book", "cut": False})
    assert open_roll(state_dir) == "book"

    client.post("/api/print", json={"text": "Weather"})
    # Two cuts in that stream: the separator in front and the receipt's own.
    assert printer.jobs[-1].count(b"\x1dV\x00") == 2
    assert open_roll(state_dir) is None


def test_the_same_job_is_not_severed_from_its_own_roll(printing_app):
    """The separator must not fire between two receipts of one book."""
    client, printer, _ = printing_app
    for number in (1, 2):
        client.post(
            "/api/print",
            json={"text": f"Pericope {number}", "job": "book", "seq": number, "cut": False},
        )
    assert not any(b"\x1dV" in job for job in printer.jobs)


def test_an_invalid_cut_value_is_refused(printing_app):
    """Two documented values, nothing else.

    'partial' is in the list on purpose: it was a third mode until 2026-09-20,
    and a sender still using it must hear about it rather than print a full cut.
    """
    client, printer, _ = printing_app
    for bad in ("full", "partial", "voll", 1, None, 0):
        response = client.post("/api/print", json={"text": "x", "cut": bad})
        assert response.status_code == 400, bad
        assert response.get_json()["error"] == "bad_request"
    assert printer.jobs == []
    assert client.post("/api/preview", json={"text": "x", "cut": "nope"}).status_code == 400
