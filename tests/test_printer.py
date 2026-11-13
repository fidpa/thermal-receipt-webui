# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the status query.

The bit decoding is a pure function, so it is tested without a socket. The
round trip over the wire gets one test against the loopback printer, because a
query that never leaves the process proves nothing about the protocol.
"""

from __future__ import annotations

from conftest import COVER_OPEN, HEALTHY, OUT_OF_PAPER

from receipt.printer import decode_status, is_reachable, read_status

# Read off a TM-T20IV on 19.09.2026: the device answers all four levels.
MEASURED = [0x16, 0x12, 0x12, 0x12]


def test_a_healthy_printer_is_ready():
    """The bytes of the real device decode to 'nothing in the way'."""
    status = decode_status(MEASURED)
    assert status is not None
    assert status.ready is True
    assert status.reason == ""
    assert not status.paper_end and not status.cover_open and not status.error


def test_an_empty_roll_is_not_ready():
    """Paper end shows up in the offline status and the paper sensor."""
    status = decode_status(list(OUT_OF_PAPER))
    assert status is not None
    assert status.paper_end is True
    assert status.ready is False
    assert "paper" in status.reason.lower()


def test_an_open_cover_is_not_ready():
    """Bit 2 of the offline status."""
    status = decode_status([0x16, 0x16, 0x12, 0x12])
    assert status is not None
    assert status.cover_open is True
    assert status.ready is False


def test_a_paper_near_end_still_prints():
    """The roll running out is a hint, not a refusal."""
    status = decode_status([0x16, 0x12, 0x12, 0x1E])
    assert status is not None
    assert status.paper_low is True
    assert status.ready is True


def test_an_offline_device_is_not_ready():
    """Bit 3 of the printer status."""
    status = decode_status([0x1E, 0x12, 0x12, 0x12])
    assert status is not None
    assert status.online is False
    assert status.ready is False


def test_an_error_is_collected_from_both_levels():
    """The offline status flags that there is one, the error status which."""
    assert decode_status([0x16, 0x52, 0x12, 0x12]).error is True
    assert decode_status([0x16, 0x12, 0x1A, 0x12]).error is True
    assert decode_status([0x16, 0x12, 0x32, 0x12]).error is True


def test_anything_but_a_status_byte_is_rejected():
    """Port 9100 is a popular port; a node exporter is not a printer.

    Bit 1 and bit 4 are set in every status byte and bit 0 and bit 7 are clear.
    Without that check an arbitrary answer would decode into a plausible-looking
    state.
    """
    assert decode_status([0x00, 0x00, 0x00, 0x00]) is None
    assert decode_status([0x16, 0x12, 0x12, 0xFF]) is None
    assert decode_status([0x16, 0x12, 0x12]) is None
    assert decode_status([]) is None


def test_the_status_survives_the_wire(fake_printer):
    """Four queries over one connection, in order, decoded at the end."""
    status = read_status("127.0.0.1", fake_printer.port)
    assert status is not None
    assert status.ready is True

    fake_printer.status = list(OUT_OF_PAPER)
    assert read_status("127.0.0.1", fake_printer.port).paper_end is True

    fake_printer.status = list(HEALTHY)
    assert is_reachable("127.0.0.1", fake_printer.port) is True


def test_an_unreachable_printer_has_no_status():
    """Port 1 has nothing listening, so the query fails fast."""
    assert read_status("127.0.0.1", 1) is None
    assert is_reachable("127.0.0.1", 1) is False


def test_the_reason_names_the_cause_not_the_consequence():
    """A real device also goes offline when the roll is empty.

    'Printer is offline' is formally right and useless to whoever has to
    reload the paper.
    """
    assert decode_status(list(OUT_OF_PAPER)).reason == "Out of paper."
    assert decode_status(list(COVER_OPEN)).reason == "Cover is open."
    # Offline with no further cause named stays 'offline'.
    assert decode_status([0x1E, 0x12, 0x12, 0x12]).reason == "Printer is offline."
