# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the HTTP surface.

The most important test in this file is the one on the stale checksum. It backs
the central claim of the project: what gets printed is what the preview showed.
"""

from __future__ import annotations

import logging

from receipt.printer import parse_target
from receipt.web import (
    MAX_TEXT_CHARS,
    build,
    create_app,
    fingerprint,
    size_key,
    text_field,
)


def test_index_renders(client):
    """The page comes back and fetches the rest itself."""
    response = client.get("/")
    assert response.status_code == 200
    assert b"<textarea" in response.data


def test_preview_returns_image_and_checksum(client):
    """The preview reports the bytes it rendered."""
    body = client.post("/api/preview", json={"text": "# Test"}).get_json()
    assert body["empty"] is False
    assert body["png"].startswith("data:image/png;base64,")
    assert body["sha"] == fingerprint(build("# Test"))
    assert body["cols"] == 48


def test_empty_text_yields_no_image(client):
    """Whitespace is not a receipt."""
    assert client.post("/api/preview", json={"text": "   "}).get_json()["empty"] is True


def test_preview_reports_the_column_count_per_size(client):
    """The interface shows the current width, so it has to come back."""
    for size, cols in (("small", 64), ("normal", 48), ("large", 24)):
        body = client.post("/api/preview", json={"text": "x", "size": size}).get_json()
        assert body["cols"] == cols


def test_unknown_size_falls_back_to_normal(client):
    """A value from the network is validated, not trusted."""
    body = client.post("/api/preview", json={"text": "x", "size": "gigantic"}).get_json()
    assert body["size"] == "normal"


def test_overlong_word_is_flagged(client):
    """A word wider than the receipt gets broken; the user hears about it first."""
    body = client.post("/api/preview", json={"text": "a" * 60}).get_json()
    assert "60 characters" in body["warning"]


def test_ordinary_text_carries_no_warning(client):
    """The warning must not cry wolf."""
    assert client.post("/api/preview", json={"text": "a short note"}).get_json()["warning"] is None


def test_stale_checksum_is_refused(printing_client):
    """The core guarantee: a changed text does not reach the printer.

    The checksum sent with the job is compared against the one the server
    recomputes from the same text. They differ here, so nothing is sent.
    """
    response = printing_client.post(
        "/api/print", json={"text": "hello", "sha": "00000000", "size": "normal"}
    )
    assert response.status_code == 409
    assert "stale" in response.get_json()["message"]


def test_matching_checksum_reaches_the_send_path(printing_client):
    """With the right checksum the job goes out; the socket then fails.

    Port 1 has no listener, so a 502 proves the job passed the checksum gate
    and got as far as the network.
    """
    text = "hello"
    sha = fingerprint(build(text))
    response = printing_client.post("/api/print", json={"text": text, "sha": sha})
    assert response.status_code == 502


def test_printing_without_a_printer_is_refused(client):
    """Preview only mode answers cleanly instead of crashing."""
    response = client.post("/api/print", json={"text": "hello"})
    assert response.status_code == 409
    assert "No printer configured" in response.get_json()["message"]


def test_printing_empty_text_is_refused(client):
    """Nothing to print is a client error, not a blank receipt."""
    assert client.post("/api/print", json={"text": "  "}).status_code == 400


def test_printer_status_without_a_printer(client):
    """The lamp needs an answer even when nothing is configured."""
    body = client.get("/api/printer").get_json()
    assert body["ok"] is True
    assert body["configured"] is False
    assert body["host"] is None
    assert body["reachable"] is False
    assert body["printer"] is None


def test_history_starts_empty(client):
    """A fresh state directory has no entries."""
    body = client.get("/api/history").get_json()
    assert body["ok"] is True
    assert body["entries"] == []


def test_every_answer_carries_ok_and_error(client, printing_client):
    """One shape for all of them, in success as in failure.

    A program should not have to know per endpoint how a refusal looks. This
    test is the executable form of that promise.
    """
    answers = [
        client.get("/api/printer"),
        client.get("/api/history"),
        client.get("/api/job/nothing-here"),
        client.post("/api/preview", json={"text": "x"}),
        client.post("/api/preview", json={"text": "x" * (MAX_TEXT_CHARS + 1)}),
        printing_client.post("/api/print", json={"text": ""}),
        printing_client.post("/api/print", json={"text": "x"}),
    ]
    for response in answers:
        body = response.get_json()
        assert set(body) >= {"ok", "error", "message"}
        assert isinstance(body["ok"], bool)
        assert (body["error"] is None) is body["ok"]


def test_the_paper_length_comes_back_as_a_number(client):
    """A program should not have to parse a localised string."""
    body = client.post("/api/preview", json={"text": "# Test\n\nzwei Zeilen"}).get_json()
    assert isinstance(body["length_cm"], float)
    assert body["length_cm"] > 0
    assert body["length_cm_text"] == f"{body['length_cm']:.1f}"


def test_a_body_that_is_not_an_object_is_answered_not_crashed(client):
    """The endpoints face the network; foreign input gets an answer.

    A list, a bare string or a number as the body used to reach payload.get()
    and raise AttributeError, which answered a malformed request with a 500 and
    a traceback in the log.
    """
    for body in ("[1, 2, 3]", '"a string"', "42", "not json at all"):
        response = client.post("/api/preview", data=body, content_type="application/json")
        assert response.status_code == 200
        assert response.get_json()["empty"] is True


def test_a_text_field_of_the_wrong_type_counts_as_empty(client):
    """Only a string is a text; anything else is nothing to print."""
    assert client.post("/api/preview", json={"text": 42}).get_json()["empty"] is True
    assert client.post("/api/preview", json={"text": None}).get_json()["empty"] is True
    assert client.post("/api/print", json={"text": ["a"]}).status_code == 400


def test_text_field_reads_strings_only():
    """The helper behind it, on its own."""
    assert text_field({"text": "hello"}, "text") == "hello"
    assert text_field({"text": 42}, "text") == ""
    assert text_field({}, "text") == ""


def test_an_over_long_text_is_refused(client, printing_client):
    """A pasted log file must not take the service down.

    Rendering grows linearly with the text, so a few megabytes would exhaust
    both the request timeout and the memory of the single worker.
    """
    text = "x" * (MAX_TEXT_CHARS + 1)
    preview = client.post("/api/preview", json={"text": text})
    assert preview.status_code == 413
    assert str(MAX_TEXT_CHARS) in preview.get_json()["message"]
    assert printing_client.post("/api/print", json={"text": text}).status_code == 413


def test_a_text_at_the_limit_is_still_rendered(client):
    """The cap refuses what is past it, not what reaches it."""
    body = client.post("/api/preview", json={"text": "x " * (MAX_TEXT_CHARS // 2)}).get_json()
    assert body["empty"] is False


def test_parse_target_reads_host_and_port():
    """host:port is accepted, a bare host falls back to 9100."""
    assert parse_target("192.0.2.50") == ("192.0.2.50", 9100)
    assert parse_target("192.0.2.50:9101") == ("192.0.2.50", 9101)
    assert parse_target("") == (None, 9100)
    assert parse_target(None) == (None, 9100)


def test_size_key_rejects_anything_unknown():
    """Only the three documented keys pass."""
    assert size_key("large") == "large"
    assert size_key("nonsense") == "normal"
    assert size_key(None) == "normal"
    assert size_key(42) == "normal"


def test_the_app_makes_its_own_info_messages_visible(tmp_path):
    """Otherwise a successful print leaves no trace in the log.

    Importing escpos calls logging.basicConfig(), which parks a handler on the
    root logger at WARNING. The command line overrides that with force=True;
    under gunicorn nothing does, so create_app has to raise its own level.
    """
    logging.getLogger("receipt").setLevel(logging.NOTSET)
    create_app(printer=None, state_dir=tmp_path)
    assert logging.getLogger("receipt.printer").isEnabledFor(logging.INFO)


def test_build_is_deterministic():
    """The same text and size always produce the same bytes.

    Without this the checksum gate would reject valid jobs at random.
    """
    assert build("# Test\n---\ntext", "large") == build("# Test\n---\ntext", "large")
