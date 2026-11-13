# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the command line.

The printer here is a real socket that accepts one job. Nothing is mocked: the
CLI opens a connection and writes the same bytes it would send to the device,
only the thing at the other end is ours.
"""

from __future__ import annotations

import socket
import sys
import threading

import pytest

from receipt.cli import main
from receipt.history import FILENAME, History


@pytest.fixture
def fake_printer():
    """A socket that takes one print job and hands back what it received."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    received: list[bytes] = []

    def accept_one() -> None:
        connection, _ = server.accept()
        with connection:
            connection.settimeout(2)
            chunks = []
            while True:
                block = connection.recv(4096)
                if not block:
                    break
                chunks.append(block)
        received.append(b"".join(chunks))

    worker = threading.Thread(target=accept_one, daemon=True)
    worker.start()
    yield server.getsockname(), received, worker
    server.close()


def test_the_printer_address_comes_from_the_environment(tmp_path, monkeypatch, fake_printer):
    """One address for both routes to paper, or they drift apart.

    The unit already carries RECEIPT_PRINTER for the web interface; the command
    line reads the same variable rather than asking for --host again.
    """
    (host, port), received, worker = fake_printer
    monkeypatch.setenv("RECEIPT_PRINTER", f"{host}:{port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note"])

    assert main() == 0
    worker.join(timeout=2)
    assert received and b"a note" in received[0]


def test_a_printed_text_lands_in_the_same_history(tmp_path, monkeypatch, fake_printer):
    """So one list holds everything that came out of the device."""
    (host, port), _received, worker = fake_printer
    monkeypatch.setenv("RECEIPT_PRINTER", f"{host}:{port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note", "--size", "large"])

    assert main() == 0
    worker.join(timeout=2)
    entries = History(tmp_path / FILENAME).entries()
    assert [(e["text"], e["size"]) for e in entries] == [("a note", "large")]


def test_a_sample_receipt_leaves_no_history_entry(tmp_path, monkeypatch, fake_printer):
    """A sample is built from calls, not from text a reprint could repeat."""
    (host, port), _received, worker = fake_printer
    monkeypatch.setenv("RECEIPT_PRINTER", f"{host}:{port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "door-sign"])

    assert main() == 0
    worker.join(timeout=2)
    assert History(tmp_path / FILENAME).entries() == []


def test_an_unreachable_printer_is_an_error(tmp_path, monkeypatch):
    """Port 1 has no listener, and a failed job must not report success."""
    monkeypatch.setenv("RECEIPT_PRINTER", "127.0.0.1:1")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note"])

    assert main() == 1
    assert History(tmp_path / FILENAME).entries() == []


def test_without_a_printer_and_without_a_preview_it_says_so(monkeypatch):
    """argparse exits with 2, the usage error, rather than doing nothing."""
    monkeypatch.delenv("RECEIPT_PRINTER", raising=False)
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note"])

    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2


def test_a_preview_alone_needs_no_printer(tmp_path, monkeypatch):
    """Developing a layout must work with no device anywhere near."""
    monkeypatch.delenv("RECEIPT_PRINTER", raising=False)
    target = tmp_path / "preview.png"
    monkeypatch.setattr(
        sys, "argv", ["receipt.cli", "-", "--text", "a note", "--preview", str(target)]
    )

    assert main() == 0
    assert target.exists()
