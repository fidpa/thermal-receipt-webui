#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""The Flask application.

The interface knows exactly one way in: free text in the Markdown subset from
markup.py. Predefined receipts are not reachable from here; they stay available
through the command line as a layout test bench.

Preview and print both go through build() and only through build(). The print
path additionally compares the checksum the preview displayed, which makes what
was on the screen provably the same bytes the printer receives.

Besides the person at the browser, the same API serves automated senders that
never see a preview. What they need - keys against duplicates, announced jobs
with a paper budget of their own, a counter to resume from - lives in jobs.py;
the contract is in the README under 'Contract for automated senders'.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import logging
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import audit, jobs
from .history import FILENAME, History, default_state_dir
from .jobs import Ledger, LedgerUnavailableError
from .layout import DEFAULT_SIZE, SIZES
from .markup import from_text, longest_word
from .preview import DPI, TooLongError, paper_cm, render
from .printer import is_reachable, parse_target, read_status, send_receipt

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8094

# Upper bound on the input text. The longest sample receipt runs to about 500
# characters, 20,000 are about 1.7 m of paper at the normal size - plenty for a
# receipt. The cap protects the service: rendering time and memory grow linearly
# with the text, so a log file of a few megabytes pasted in by accident would
# blow both the gunicorn timeout and the memory, and with a single worker that
# takes the whole interface with it.
MAX_TEXT_CHARS = 20_000
# Upper bound on the paper a single receipt may take. The character cap alone
# does not bound it: a text of nothing but line feeds costs one character and a
# full line of paper each. Six metres leave room for the longest text at the
# large size (20,000 characters come to about 5.6 m), and the renderer checks
# the bound before it allocates the image.
MAX_LENGTH_CM = 600.0
MAX_HEIGHT_DOTS = round(MAX_LENGTH_CM / 2.54 * DPI)
# Keys and job identifiers never become file names, but they do go into the
# state on disk. A cap keeps it small and readable.
MAX_KEY_CHARS = 128
# Which HTTP status a refusal from the ledger carries: an exhausted hourly or
# daily contingent means "try later", a closed job is a contradiction - that one
# never reopens.
REFUSAL_STATUS = {"budget_exceeded": 429, "job_done": 409}
# A job identifier appears in the path of /api/job/<id>; a slash in it would be
# unreachable from there.
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
# Error keys and messages for the refusals Flask itself produces. Werkzeug's own
# descriptions tell a sender nothing the key does not already say.
HTTP_ERRORS = {
    400: ("bad_request", "The request is malformed."),
    404: ("not_found", "No such path."),
    405: ("method_not_allowed", "That method is not provided for this path."),
    413: ("too_long", "The request is too large."),
    415: ("bad_request", "The body has to be JSON."),
}

logger = logging.getLogger(__name__)


def json_object() -> dict[str, Any]:
    """Return the request body as an object.

    Anything that is not an object - a list, a number, no JSON at all - becomes
    the empty object. The endpoints are meant to answer foreign input, not to
    break off with a traceback.

    Returns:
        The JSON body, or an empty object.
    """
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def text_field(payload: dict[str, Any], key: str) -> str:
    """Read a text field out of the body.

    Args:
        payload: The JSON body of the request.
        key: Name of the field.

    Returns:
        The value when it is a string, the empty string otherwise.
    """
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def int_field(payload: dict[str, Any], key: str) -> int | None:
    """Read an integer field out of the body.

    Args:
        payload: The JSON body of the request.
        key: Name of the field.

    Returns:
        The value when it is an integer, None otherwise. A bool is not one.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def number_field(payload: dict[str, Any], key: str) -> float | None:
    """Read a numeric field out of the body.

    Args:
        payload: The JSON body of the request.
        key: Name of the field.

    Returns:
        The value as a float, or None when it is not a number.
    """
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    # NaN compares False against everything: a NaN in the cm field would have
    # disabled every budget limit and ended up in the response as invalid JSON.
    if not math.isfinite(value):
        return None
    return float(value)


def flag_field(payload: dict[str, Any], key: str, default: bool) -> bool:
    """Read a yes/no field out of the body.

    Args:
        payload: The JSON body of the request.
        key: Name of the field.
        default: What applies when the field is missing or not a bool.

    Returns:
        The value, or the default.
    """
    value = payload.get(key)
    return value if isinstance(value, bool) else default


def type_complaint(payload: dict[str, Any]) -> str:
    """Name the first field that is present and of the wrong type.

    A silently discarded field is the worst possible answer for a program:
    "seq": "12" would leave the job without a counter, "final": "true" would
    keep it open, "key": 12345 would switch off the lock against the second
    sheet - all of it with HTTP 200.

    Args:
        payload: The JSON body of the request.

    Returns:
        The name of the field, or the empty string when all of them fit.
    """
    for name in ("text", "sha", "size", "key", "job"):
        if name in payload and not isinstance(payload[name], str):
            return name
    for name in ("seq", "sheets"):
        if name in payload and (
            isinstance(payload[name], bool) or not isinstance(payload[name], int)
        ):
            return name
    if "cm" in payload and (
        isinstance(payload["cm"], bool)
        or not isinstance(payload["cm"], int | float)
        or not math.isfinite(payload["cm"])
    ):
        return "cm"
    for name in ("final", "history"):
        if name in payload and not isinstance(payload[name], bool):
            return name
    return ""


def cut_mode(payload: dict[str, Any]) -> str | None:
    """Read the `cut` field as a cut mode.

    A person at the browser wants their receipt cut off, so 'full' is the
    default. 'none' is for a book meant to come out as one continuous roll.

    There was a third mode for the partial cut until 2026-09-20. A test print
    settled it: the cut does hold a tab, but it pulls about 1 cm of paper per
    seam that the preview never counts. For a continuous roll 'none' is cheaper
    and looks better.

    Args:
        payload: The JSON body of the request.

    Returns:
        'full' or 'none'; None for an invalid value.
    """
    if "cut" not in payload:
        return "full"
    value = payload["cut"]
    if value is True:
        return "full"
    if value is False:
        return "none"
    return None


def size_key(value: object) -> str:
    """Validate a size key coming in over the network.

    Args:
        value: Whatever the browser sent.

    Returns:
        A key from SIZES; the normal size for anything else.
    """
    return value if isinstance(value, str) and value in SIZES else DEFAULT_SIZE


def client_ip() -> str:
    """Return the source IP from behind a reverse proxy.

    A proxy that appends its own view with X-Forwarded-For leaves the address it
    saw as the rightmost element, so that one cannot be made up by the caller.
    Put the service behind such a proxy, or the budget can be sidestepped with a
    forged header.

    Returns:
        The source IP, or 'unknown'.
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip() or "unknown"
    return request.remote_addr or "unknown"


def too_long_message(length: int) -> str:
    """Word the refusal of a text beyond the upper bound.

    Args:
        length: Length of the incoming text in characters.

    Returns:
        Message for the interface.
    """
    return f"Text is {length} characters long, at most {MAX_TEXT_CHARS} are provided for."


def too_much_paper_message(error: TooLongError) -> str:
    """Explain a refusal for a receipt that would run too long.

    Args:
        error: The refusal from the renderer.

    Returns:
        Message for the interface.
    """
    return (
        f"The receipt would take {paper_cm(error.height_dots):.0f} cm of paper, "
        f"at most {MAX_LENGTH_CM:.0f} cm are provided for."
    )


def answer(
    ok: bool,
    message: str = "",
    *,
    error: str | None = None,
    status: int = 200,
    **fields: Any,
) -> tuple[Response, int]:
    """Build the answer every endpoint gives - in success as in failure.

    One shape throughout, so a program does not have to guess per endpoint:
    'ok' says whether it worked, 'error' carries a stable key when it did not,
    'message' the sentence for a person.

    Args:
        ok: Whether the operation succeeded.
        message: Sentence for the interface.
        error: Stable error key, only when ok is False.
        status: HTTP status.
        **fields: Further fields of the individual endpoint.

    Returns:
        The JSON response and the HTTP status.
    """
    body: dict[str, Any] = {"ok": ok, "error": error, "message": message}
    body.update(fields)
    return jsonify(body), status


def build(text: str, size: str = DEFAULT_SIZE, cut: str = "full") -> bytes:
    """Build the ESC/POS byte stream from text.

    The only place where text turns into bytes. Preview and print call nothing
    else; without that narrow waist the image and the paper could drift apart.

    Args:
        text: Input text in the Markdown subset.
        size: Key of the type size from SIZES.
        cut: 'full' or 'none' - see cut_mode().

    Returns:
        Raw ESC/POS byte stream, with or without a cut depending on `cut`.
    """
    return from_text(text, size=SIZES[size], cut=cut).data


def fingerprint(data: bytes) -> str:
    """Short hash of a byte stream, shown in the interface.

    Args:
        data: Raw ESC/POS byte stream.

    Returns:
        The first eight hex digits of the SHA-256.
    """
    return hashlib.sha256(data).hexdigest()[:8]


def paper_length_cm(data: bytes) -> float:
    """Measure how much paper a byte stream consumes.

    Args:
        data: Raw ESC/POS byte stream.

    Returns:
        Paper length in centimetres.
    """
    return paper_cm(render(data, max_height_dots=MAX_HEIGHT_DOTS).height)


def preview_data_uri(data: bytes) -> tuple[str, float]:
    """Render ESC/POS bytes as a data: URI, so the page needs no file.

    Args:
        data: Raw ESC/POS byte stream.

    Returns:
        A data: URI holding a PNG, and the paper length in centimetres.
    """
    image = render(data, max_height_dots=MAX_HEIGHT_DOTS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    uri = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
    return uri, paper_cm(image.height)


def job_view(job: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reduce a job to what the sender needs.

    Args:
        job: The stored job, or None.

    Returns:
        An object for the response, or None without a job.
    """
    if job is None:
        return None
    return {
        "id": job["id"],
        "sheets": job["sheets"],
        "declared_cm": job["declared_cm"],
        "printed_cm": round(job["printed_cm"], 1),
        "printed_sheets": job["printed_sheets"],
        "last_seq": job["last_seq"],
        "confirmed_seq": job["confirmed_seq"],
        "interrupted": job.get("interrupted", False),
        "done": job["done"],
        "created_at": job["created_at"],
        "updated_at": job.get("updated_at", job["created_at"]),
    }


def create_app(printer: str | None = None, state_dir: Path | None = None) -> Flask:
    """Build the Flask application.

    Without arguments both values come from the environment, which is how
    gunicorn starts the app as 'receipt.web:create_app()' without the unit file
    having to know any code.

    Args:
        printer: IP address or hostname of the printer, optionally with a port;
            None leaves the preview as the only function.
        state_dir: Directory for history, ledger and audit record.

    Returns:
        The configured application.
    """
    # Importing escpos calls logging.basicConfig(), which leaves a handler on the
    # root logger whose level stays at WARNING. Under gunicorn nobody calls
    # basicConfig with force=True the way the command line does - without a level
    # of its own, every successful print would go unlogged and only failures
    # would show.
    logging.getLogger(__package__).setLevel(logging.INFO)

    app = Flask(__name__)
    host, port = parse_target(printer or os.environ.get("RECEIPT_PRINTER"))
    directory = state_dir or default_state_dir()
    app.config["PRINTER_HOST"] = host
    app.config["PRINTER_PORT"] = port
    app.config["HISTORY"] = History(directory / FILENAME)
    app.config["LEDGER"] = Ledger(directory / jobs.FILENAME)
    app.config["AUDIT"] = audit.Audit(directory / audit.FILENAME)
    # One receipt at a time to the paper: the lock covers the budget check, the
    # status query, the send and the charge together. Without it two senders
    # could slip through the same gap in a contingent, and two byte streams
    # could run into each other.
    app.config["PRINT_LOCK"] = threading.Lock()
    # Bound on the request body. MAX_TEXT_CHARS is only checked once the JSON is
    # parsed, which for a body of hundreds of megabytes is too late. 512 KB hold
    # 20,000 characters even when every one of them arrives as a \uXXXX escape.
    app.config["MAX_CONTENT_LENGTH"] = 512 * 1024

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException) -> Any:
        """Flask's own refusals carry the same response shape as everything else.

        A wrong path or a wrong method used to come back as HTML, and a sender
        reading ok and error got a parse failure instead of an answer.
        """
        status = error.code or 500
        key, message = HTTP_ERRORS.get(status, ("http_error", error.name))
        return answer(False, message, error=key, status=status)

    @app.errorhandler(LedgerUnavailableError)
    def handle_ledger_unavailable(error: LedgerUnavailableError) -> Any:
        """An unreadable ledger stops printing instead of being overwritten."""
        return answer(
            False,
            "The job ledger cannot be read; nothing is printed until it can.",
            error="ledger_unavailable",
            status=503,
        )

    @app.errorhandler(Exception)
    def handle_unexpected(error: Exception) -> Any:
        """A fault in the service stays a JSON object, not a traceback."""
        logger.exception("unhandled error in %s", request.path)
        return answer(False, "Unexpected error in the service.", error="internal", status=500)

    @app.route("/")
    def index() -> str:
        """Serve the interface; it fetches everything else itself."""
        return render_template(
            "index.html",
            sizes={key: value.cols for key, value in SIZES.items()},
            default_size=DEFAULT_SIZE,
        )

    @app.post("/api/preview")
    def api_preview() -> Any:
        """Render the entered text as an image of the real byte stream."""
        payload = json_object()
        text = text_field(payload, "text")
        size = size_key(payload.get("size"))
        cols = SIZES[size].cols
        cut = cut_mode(payload)
        if cut is None:
            return answer(
                False, "Field 'cut' has an invalid value.", error="bad_request", status=400
            )

        if not text.strip():
            return answer(True, empty=True, size=size, cols=cols)
        if len(text) > MAX_TEXT_CHARS:
            return answer(
                False, too_long_message(len(text)), error="too_long", status=413, empty=True
            )

        data = build(text, size, cut)
        try:
            uri, length_cm = preview_data_uri(data)
        except TooLongError as error:
            return answer(
                False, too_much_paper_message(error), error="too_long", status=413, empty=True
            )
        overlong = longest_word(text)
        return answer(
            True,
            empty=False,
            png=uri,
            size=size,
            cols=cols,
            cut=cut,
            length_cm=round(length_cm, 1),
            length_cm_text=f"{length_cm:.1f}",
            bytes=len(data),
            sha=fingerprint(data),
            warning=(
                f"One word is {overlong} characters long, more than this receipt's {cols} columns."
                if overlong > cols
                else None
            ),
        )

    @app.post("/api/print")
    def api_print() -> Any:
        """Print exactly the byte stream the preview showed."""
        payload = json_object()
        text = text_field(payload, "text")
        expected = text_field(payload, "sha")
        size = size_key(payload.get("size"))
        key = text_field(payload, "key")
        job_id = text_field(payload, "job")
        seq = int_field(payload, "seq")
        sheets = int_field(payload, "sheets")
        declared_cm = number_field(payload, "cm")
        final = flag_field(payload, "final", False)
        cut = cut_mode(payload)
        # An automated sender does not belong in the 20 entries where a person
        # looks for their last receipt. Announcing a job makes you one; every
        # sender can also say so itself.
        record = flag_field(payload, "history", not job_id)
        source = client_ip()
        ledger: Ledger = app.config["LEDGER"]
        book: audit.Audit = app.config["AUDIT"]

        def refuse(message: str, error: str, status: int, **fields: Any) -> tuple[Response, int]:
            """Answer with a refusal and write the operation to the record."""
            book.emit(
                "print",
                result=error,
                src_ip=source,
                method=request.method,
                path=request.path,
                payload={"job": job_id or None, "seq": seq, "key": key or None, **fields},
            )
            return answer(False, message, error=error, status=status, **fields)

        if cut is None:
            return refuse("Field 'cut' has an invalid value.", "bad_request", 400)
        offender = type_complaint(payload)
        if offender:
            return refuse(f"Field '{offender}' has the wrong type.", "bad_request", 400)
        for name, value in (("key", key), ("job", job_id)):
            if len(value) > MAX_KEY_CHARS:
                return refuse(
                    f"Field '{name}' is longer than {MAX_KEY_CHARS} characters.",
                    "bad_request",
                    400,
                )
        if job_id and not JOB_ID_PATTERN.match(job_id):
            return refuse(
                "A job identifier may hold only letters, digits, dot, hyphen and underscore.",
                "bad_request",
                400,
            )
        if seq is not None and seq < 0:
            return refuse("Field 'seq' must not be negative.", "bad_request", 400)
        if sheets is not None and sheets <= 0:
            return refuse("Field 'sheets' must be greater than zero.", "bad_request", 400)
        if declared_cm is not None and declared_cm <= 0:
            return refuse("Field 'cm' must be greater than zero.", "bad_request", 400)

        if not text.strip():
            return refuse("No text entered.", "empty_text", 400)
        if len(text) > MAX_TEXT_CHARS:
            return refuse(too_long_message(len(text)), "too_long", 413)

        printer_host = app.config["PRINTER_HOST"]
        if not printer_host:
            return refuse("No printer configured.", "no_printer", 409)

        data = build(text, size, cut)
        actual = fingerprint(data)
        if expected and expected != actual:
            # The text changed between the preview and the click.
            logger.warning("preview %s does not match print job %s", expected, actual)
            return refuse("Preview is stale, look again.", "stale_preview", 409)

        try:
            length_cm = paper_length_cm(data)
        except TooLongError as error:
            return refuse(too_much_paper_message(error), "too_long", 413)
        printer_port = app.config["PRINTER_PORT"]

        with app.config["PRINT_LOCK"]:
            if key:
                known = ledger.recall(key)
                if known and known["sha"] != actual:
                    return refuse(
                        f"Key {key} already stands for a different receipt "
                        f"(checksum {known['sha']}, {known['at']}).",
                        "key_conflict",
                        409,
                        sha=actual,
                    )
                if known:
                    state_of_job = ledger.job(known["job"]) if known["job"] else None
                    doubtful = (
                        state_of_job is not None
                        and state_of_job.get("interrupted")
                        and known["seq"] is not None
                        and (
                            state_of_job.get("confirmed_seq") is None
                            or known["seq"] > state_of_job["confirmed_seq"]
                        )
                    )
                    if doubtful:
                        # The sheet went out but was never confirmed, and a fault
                        # happened in between. A silent duplicate would leave
                        # exactly the gap confirmed_seq exists to prevent.
                        return refuse(
                            f"Key {key} belongs to sheet {known['seq']}, which is unconfirmed "
                            "after a fault. Pick a new key to print it again.",
                            "key_spent",
                            409,
                            job=job_view(state_of_job),
                        )
                    logger.info("key %s is known - no second sheet", key)
                    book.emit(
                        "print",
                        result="duplicate",
                        src_ip=source,
                        method=request.method,
                        path=request.path,
                        payload={"job": known["job"], "seq": known["seq"], "key": key},
                    )
                    return answer(
                        True,
                        f"Already printed at {known['at']} - no second sheet.",
                        sha=actual,
                        bytes=len(data),
                        length_cm=known["length_cm"],
                        length_cm_text=f"{known['length_cm']:.1f}",
                        duplicate=True,
                        job=job_view(ledger.job(job_id)) if job_id else None,
                    )

            refusal = ledger.check(
                ip=source, job_id=job_id, length_cm=length_cm, declared_cm=declared_cm
            )
            if refusal is not None:
                response, status = refuse(
                    refusal.message,
                    refusal.code,
                    REFUSAL_STATUS[refusal.code],
                    budget=refusal.as_dict(),
                    length_cm=round(length_cm, 1),
                )
                if refusal.retry_after_seconds:
                    response.headers["Retry-After"] = str(refusal.retry_after_seconds)
                return response, status

            # The printer says before the send whether there is paper at all. It
            # does not say whether the previous receipt came out whole - that
            # answer comes with the next receipt or at /api/job.
            state = read_status(printer_host, printer_port)
            if state is not None and not state.ready:
                # The sheet this job sent last may be hanging out half printed;
                # from now on no healthy report confirms anything until the
                # sender has printed again.
                ledger.fault(job_id)
                return refuse(state.reason, "printer_not_ready", 409, printer=state.as_dict())

            sent = send_receipt(
                printer_host,
                data,
                printer_port,
                owner=job_id,
                leaves_open=cut == "none",
                state_dir=directory,
            )
            if not sent:
                ledger.fault(job_id)
                return refuse(f"Printer {printer_host} unreachable.", "printer_unreachable", 502)

            job, recorded = ledger.commit(
                ip=source,
                job_id=job_id,
                seq=seq,
                sheets=sheets,
                declared_cm=declared_cm,
                key=key,
                sha=actual,
                length_cm=length_cm,
                final=final,
                printer_ready=state is not None and state.ready,
            )
            if record:
                app.config["HISTORY"].add(text, size)

        book.emit(
            "print",
            result="ok",
            src_ip=source,
            method=request.method,
            path=request.path,
            payload={
                "job": job_id or None,
                "seq": seq,
                "key": key or None,
                "sha": actual,
                "bytes": len(data),
                "length_cm": round(length_cm, 1),
                "size": size,
                "cut": cut,
                "history": record,
                "recorded": recorded,
            },
        )
        note = ""
        if not recorded:
            # The paper is out, the charge is not. The sender has to hear about
            # it: its key no longer protects against a second sheet, and the
            # budget never saw this receipt.
            note = " WARNING: not written to the ledger - a retry will print again."
            logger.error("print succeeded, charge failed (key %s)", key or "-")
        return answer(
            True,
            f"Printed {len(data)} bytes to {printer_host}:{printer_port}, "
            f"checksum {actual}, {length_cm:.1f} cm{note}",
            recorded=recorded,
            cut=cut,
            sha=actual,
            bytes=len(data),
            length_cm=round(length_cm, 1),
            length_cm_text=f"{length_cm:.1f}",
            duplicate=False,
            job=job_view(job),
            printer=state.as_dict() if state else None,
        )

    @app.get("/api/printer")
    def api_printer() -> Any:
        """Report reachability and, where possible, paper, cover and errors."""
        printer_host = app.config["PRINTER_HOST"]
        printer_port = app.config["PRINTER_PORT"]
        if not printer_host:
            return answer(
                True,
                "No printer configured.",
                configured=False,
                host=None,
                port=printer_port,
                reachable=False,
                printer=None,
                budget=app.config["LEDGER"].budget(client_ip()),
            )

        state = read_status(printer_host, printer_port)
        reachable = state is not None or is_reachable(printer_host, printer_port)
        return answer(
            True,
            state.reason if state else "",
            configured=True,
            host=printer_host,
            port=printer_port,
            reachable=reachable,
            printer=state.as_dict() if state else None,
            # So a sender can call off a large job before starting it. The free
            # contingent is per source IP, so this is the caller's.
            budget=app.config["LEDGER"].budget(client_ip()),
        )

    @app.get("/api/job/<job_id>")
    def api_job(job_id: str) -> Any:
        """Tell a sender how far its job has come.

        The printer state is read freshly for this: when it reports healthy and
        the last send is far enough back, the last sheet counts as confirmed too.
        """
        printer_host = app.config["PRINTER_HOST"]
        # The moment BEFORE the query counts: it can take up to two seconds, and
        # a later stamp would confirm a sheet on the strength of a state that is
        # older than the sheet itself.
        asked_at = time.time()
        state = read_status(printer_host, app.config["PRINTER_PORT"]) if printer_host else None
        ledger: Ledger = app.config["LEDGER"]
        job = ledger.job(job_id, printer_ready=state is not None and state.ready, now=asked_at)
        if job is None:
            return answer(False, f"Job {job_id} is unknown.", error="job_unknown", status=404)
        return answer(
            True,
            job=job_view(job),
            keys=ledger.keys_of(job_id),
            printer=state.as_dict() if state else None,
        )

    @app.get("/api/history")
    def api_history() -> Any:
        """Return the recently printed texts, newest first."""
        return answer(True, entries=app.config["HISTORY"].entries())

    return app


def main() -> int:
    """Start the development server.

    In production the same app runs under gunicorn; see systemd/ for a unit.

    Returns:
        0 on a clean exit, 1 when the server could not start.
    """
    parser = argparse.ArgumentParser(
        prog="python -m receipt.web",
        description="Web interface for typing and printing receipts (development server).",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})"
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port (default {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--printer", help="printer address, optionally with :port; without it, preview only"
    )
    parser.add_argument("--state-dir", type=Path, help="directory for the print history")
    args = parser.parse_args()

    # force=True, because importing escpos.printer already installs a handler on
    # the root logger and sets level WARNING. Without force, basicConfig has no
    # effect and INFO messages disappear.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", force=True)

    if not args.printer:
        logger.warning("no --printer given: preview only, the print button stays off")

    app = create_app(printer=args.printer, state_dir=args.state_dir)

    try:
        app.run(host=args.host, port=args.port)
    except OSError as exc:
        logger.error("could not start on %s:%d: %s", args.host, args.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
