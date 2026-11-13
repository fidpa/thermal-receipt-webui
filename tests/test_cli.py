# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the command line.

The printer here is the loopback FakePrinter from conftest.py. Nothing is
mocked: the CLI opens a connection and writes the same bytes it would send to
the device, only the thing at the other end is ours.
"""

from __future__ import annotations

import sys

import pytest
from conftest import OUT_OF_PAPER

from receipt.cli import main
from receipt.history import FILENAME, History


def test_the_printer_address_comes_from_the_environment(tmp_path, monkeypatch, fake_printer):
    """One address for both routes to paper, or they drift apart.

    The unit already carries RECEIPT_PRINTER for the web interface; the command
    line reads the same variable rather than asking for --host again.
    """
    monkeypatch.setenv("RECEIPT_PRINTER", f"127.0.0.1:{fake_printer.port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note"])

    assert main() == 0
    assert fake_printer.jobs and b"a note" in fake_printer.jobs[0]


def test_a_printed_text_lands_in_the_same_history(tmp_path, monkeypatch, fake_printer):
    """So one list holds everything that came out of the device."""
    monkeypatch.setenv("RECEIPT_PRINTER", f"127.0.0.1:{fake_printer.port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note", "--size", "large"])

    assert main() == 0
    entries = History(tmp_path / FILENAME).entries()
    assert [(e["text"], e["size"]) for e in entries] == [("a note", "large")]


def test_a_sample_receipt_leaves_no_history_entry(tmp_path, monkeypatch, fake_printer):
    """A sample is built from calls, not from text a reprint could repeat."""
    monkeypatch.setenv("RECEIPT_PRINTER", f"127.0.0.1:{fake_printer.port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "door-sign"])

    assert main() == 0
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


def test_a_preview_does_not_fall_back_to_the_printer(tmp_path, monkeypatch, fake_printer):
    """Looking at a layout must not cost paper, even with RECEIPT_PRINTER set."""
    monkeypatch.setenv("RECEIPT_PRINTER", f"127.0.0.1:{fake_printer.port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    target = tmp_path / "preview.png"
    monkeypatch.setattr(
        sys, "argv", ["receipt.cli", "-", "--text", "a note", "--preview", str(target)]
    )

    assert main() == 0
    assert target.exists()
    assert fake_printer.jobs == []


def test_a_printer_out_of_paper_is_refused(tmp_path, monkeypatch, fake_printer):
    """The command line asks before it sends, as the web interface does."""
    fake_printer.status = list(OUT_OF_PAPER)
    monkeypatch.setenv("RECEIPT_PRINTER", f"127.0.0.1:{fake_printer.port}")
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["receipt.cli", "-", "--text", "a note"])

    assert main() == 1
    assert fake_printer.jobs == []
    assert History(tmp_path / FILENAME).entries() == []
