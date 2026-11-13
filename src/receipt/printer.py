#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Send an ESC/POS byte stream to a network printer, and ask how it is doing.

Raw/JetDirect on port 9100, the standard route for Epson and Star devices. No
CUPS, no driver, no udev rule.

Next to sending there is the status query. DLE EOT is a real-time command: the
device answers it immediately, even while its buffer is still full. The query
uses a connection of its own and does not touch the print path.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .history import default_state_dir
from .layout import cut_sequence

RAW_PORT = 9100
# Who last left the paper uncut. It lives in the state directory next to the
# history and the ledger, because both routes to paper read it: the service and
# the command line. A daily weather receipt should not hang off the roll a book
# job left open when it broke off during the night.
ROLL_FILENAME = "roll.json"
SEND_TIMEOUT_SECONDS = 5.0
# Kept short: the status drives a lamp in the web interface that asks
# regularly. A printer that is switched off must not block it.
PROBE_TIMEOUT_SECONDS = 0.8
# The status query needs four rounds over the same connection; on a TM-T20IV
# that costs about 100 ms in total (measured 19.09.2026).
STATUS_TIMEOUT_SECONDS = 2.0

# DLE EOT n - n picks the level: 1 printer status, 2 offline status, 3 error
# status, 4 paper sensor.
_STATUS_QUERIES = (1, 2, 3, 4)
_DLE_EOT = (0x10, 0x04)

# Every answer has bit 1 and bit 4 set and bit 0 and bit 7 clear. Anything else
# is not a status byte but some other service on port 9100 - a Prometheus node
# exporter, for instance.
_FIXED_MASK = 0x93
_FIXED_BITS = 0x12

logger = logging.getLogger(__name__)

# Reading, sending and noting belong together; the service holds its own print
# lock, the command line does not.
_roll_lock = threading.Lock()


@dataclass(frozen=True)
class Status:
    """What the printer says about itself."""

    online: bool
    cover_open: bool
    paper_end: bool
    paper_low: bool
    error: bool

    @property
    def ready(self) -> bool:
        """True when nothing speaks against printing right now."""
        return self.online and not (self.cover_open or self.paper_end or self.error)

    @property
    def reason(self) -> str:
        """The first reason against printing; empty when there is none.

        Cause before consequence: with an empty roll or an open cover the
        printer also sets the offline bit. "Printer is offline" would be
        formally right and useless to whoever has to reload the paper.
        """
        if self.paper_end:
            return "Out of paper."
        if self.cover_open:
            return "Cover is open."
        if self.error:
            return "Printer reports an error."
        if not self.online:
            return "Printer is offline."
        return ""

    def as_dict(self) -> dict[str, Any]:
        """Return the fields as a JSON-friendly object, ready and reason included.

        Returns:
            An object for an endpoint's response.
        """
        return {
            "online": self.online,
            "cover_open": self.cover_open,
            "paper_end": self.paper_end,
            "paper_low": self.paper_low,
            "error": self.error,
            "ready": self.ready,
            "reason": self.reason,
        }


def parse_target(value: str | None) -> tuple[str | None, int]:
    """Split a printer address of the form host or host:port.

    It lives here rather than in the web module because both routes to paper
    read the same address: the interface from RECEIPT_PRINTER, the command line
    from --host or that same variable. Two readings of one setting would be two
    printers.

    The port is rarely spelled out, but on many hosts the Prometheus node
    exporter occupies port 9100 as well, so a printer can only be simulated
    locally on a different port.

    Args:
        value: Printer address from the command line or the environment; empty
            or None is allowed.

    Returns:
        The host (None when nothing is configured) and the port.
    """
    if not value:
        return None, RAW_PORT
    host, separator, port = value.rpartition(":")
    if separator and port.isdigit():
        return host, int(port)
    return value, RAW_PORT


def send_to_printer(host: str, data: bytes, port: int = RAW_PORT) -> bool:
    """Send an ESC/POS byte stream to a network printer.

    Args:
        host: IP address or hostname of the printer.
        data: Raw ESC/POS byte stream.
        port: Raw port, 9100 by default on Epson and Star devices.

    Returns:
        True on success, False when the printer could not be reached.
    """
    try:
        with socket.create_connection((host, port), timeout=SEND_TIMEOUT_SECONDS) as sock:
            sock.sendall(data)
    except OSError as exc:
        logger.error("printer %s:%d unreachable: %s", host, port, exc)
        return False
    logger.info("sent %d bytes to %s:%d", len(data), host, port)
    return True


def is_reachable(host: str, port: int = RAW_PORT) -> bool:
    """Check whether the printer currently accepts the raw port.

    An open port means reachable, not ready to print: out of paper or an open
    cover is something only read_status() reports back.

    Args:
        host: IP address or hostname of the printer.
        port: Raw port, 9100 by default on Epson and Star devices.

    Returns:
        True when a connection was established.
    """
    try:
        with socket.create_connection((host, port), timeout=PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def decode_status(answers: list[int]) -> Status | None:
    """Read out the four status bytes of one DLE EOT round.

    Args:
        answers: The answers to DLE EOT 1 through 4, in that order.

    Returns:
        The status, or None when the fixed bits do not match - then something
        other than an ESC/POS printer answered.
    """
    if len(answers) != len(_STATUS_QUERIES):
        return None
    if any(byte & _FIXED_MASK != _FIXED_BITS for byte in answers):
        return None

    printer, offline, error, paper = answers
    return Status(
        online=not printer & 0x08,
        cover_open=bool(offline & 0x04),
        # Bit 5 of the offline status reports the stop caused by the paper end,
        # the two paper sensor bits report the empty roll. Either way there is
        # no paper left under the print head.
        paper_end=bool(offline & 0x20) or paper & 0x60 == 0x60,
        paper_low=paper & 0x0C == 0x0C,
        # Bit 6 of the offline status collects every error; the error status
        # names the cutter (bit 3), unrecoverable (bit 5) and auto-recoverable
        # (bit 6).
        error=bool(offline & 0x40) or bool(error & 0x68),
    )


def read_status(host: str, port: int = RAW_PORT) -> Status | None:
    """Ask about paper, cover and errors over DLE EOT.

    The four levels go one after another over the same connection; sent as one
    block a TM-T20IV answers ten times slower (measured 19.09.2026: 102 ms
    against 1,038 ms).

    Args:
        host: IP address or hostname of the printer.
        port: Raw port, 9100 by default on Epson and Star devices.

    Returns:
        The status, or None when the printer was unreachable or sent something
        that is not a valid status byte.
    """
    answers: list[int] = []
    try:
        with socket.create_connection((host, port), timeout=STATUS_TIMEOUT_SECONDS) as sock:
            sock.settimeout(STATUS_TIMEOUT_SECONDS)
            for query in _STATUS_QUERIES:
                sock.sendall(bytes((*_DLE_EOT, query)))
                chunk = sock.recv(1)
                if not chunk:
                    logger.warning("printer %s:%d cut the status query short", host, port)
                    return None
                answers.append(chunk[0])
    except OSError as exc:
        logger.debug("status of %s:%d not readable: %s", host, port, exc)
        return None

    decoded = decode_status(answers)
    if decoded is None:
        logger.warning(
            "%s:%d answers DLE EOT with %s - that is not an ESC/POS status",
            host,
            port,
            " ".join(f"{byte:02x}" for byte in answers),
        )
    return decoded


def _roll_path(state_dir: Path | None) -> Path:
    """Where the note about the standing roll lives."""
    return (state_dir or default_state_dir()) / ROLL_FILENAME


def open_roll(state_dir: Path | None = None) -> str | None:
    """Return who left the paper uncut.

    Args:
        state_dir: Directory holding the note; None takes the state directory.

    Returns:
        The job identifier (the empty string without a job), or None when the
        last receipt was cut off.
    """
    try:
        raw = _roll_path(state_dir).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("roll note not readable: %s", exc)
        return None
    try:
        owner = json.loads(raw).get("owner")
    except (json.JSONDecodeError, AttributeError):
        return None
    return owner if isinstance(owner, str) else None


def remember_roll(owner: str | None, state_dir: Path | None = None) -> None:
    """Record whether the paper is left standing.

    Args:
        owner: Job identifier of the receipt that ended without a cut; None when
            it was cut and the roll is therefore closed.
        state_dir: Directory holding the note; None takes the state directory.
    """
    path = _roll_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps({"owner": owner}), encoding="utf-8")
        os.replace(temporary, path)
    except OSError as exc:
        logger.error("roll note not writable: %s", exc)


def send_receipt(
    host: str,
    data: bytes,
    port: int = RAW_PORT,
    *,
    owner: str = "",
    leaves_open: bool = False,
    state_dir: Path | None = None,
) -> bool:
    """The shared route to paper - for the service and the command line alike.

    When a roll from the previous print is still standing in the device and this
    receipt does not belong to it, a cut goes first. Otherwise the daily weather
    receipt would stick to the book that broke off during the night.

    Args:
        host: IP address or hostname of the printer.
        data: Raw ESC/POS byte stream.
        port: Raw port, 9100 by default on Epson and Star devices.
        owner: Who this receipt belongs to - a job identifier, or empty.
        leaves_open: True when this receipt ends without a cut.
        state_dir: Directory holding the roll note; None takes the state directory.

    Returns:
        True on success, False when the printer could not be reached.
    """
    with _roll_lock:
        standing = open_roll(state_dir)
        if standing is not None and standing != owner:
            logger.info("severing the roll left open by %r", standing or "(no job)")
            data = cut_sequence() + data
        if not send_to_printer(host, data, port):
            return False
        remember_roll(owner if leaves_open else None, state_dir)
    return True
