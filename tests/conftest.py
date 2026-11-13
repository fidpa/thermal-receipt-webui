# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Shared fixtures.

Only pure functions and an in-process Flask client are tested: no mocks, no
running services. Every file a test needs is created inside that test's own
tmp_path.

The one exception is ``fake_printer``: a socket on the loopback interface that
answers DLE EOT and swallows print data. It is not a mock - the code under test
opens a real connection and speaks the real protocol - but a print path without
something listening cannot be tested at all.
"""

from __future__ import annotations

import re
import select
import socket
import threading

import pytest

from receipt.layout import SIZES
from receipt.web import create_app

# The four answers of a healthy printer to DLE EOT 1 through 4.
HEALTHY = (0x16, 0x12, 0x12, 0x12)
# The roll is empty: bit 5 of the offline status and both paper sensor bits -
# and bit 3 of the printer status, because a real device also goes offline over
# it. Keeping 0x16 here would let every test pass on a state no printer ever
# reports.
OUT_OF_PAPER = (0x1E, 0x32, 0x12, 0x72)
# The cover is open: bit 2 of the offline status, again with the offline bit.
COVER_OPEN = (0x1E, 0x16, 0x12, 0x12)


class FakePrinter:
    """A loopback socket that speaks just enough ESC/POS to be a printer."""

    def __init__(self, status=HEALTHY):
        self.status = list(status)
        self._jobs: list[bytes] = []
        # Connections taken from the backlog whose handler has not finished.
        # Accepting and counting happen under one lock, so a connection is
        # always visible either in the backlog or in this counter.
        self._pending = 0
        self._cond = threading.Condition()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def jobs(self) -> list[bytes]:
        """The byte streams received so far, once every connection is handled.

        The service answers as soon as it has closed its socket, while the
        handler here may still be reading the last bytes. Without waiting, a
        test reading this list right after a print races that handler, and on a
        slow runner loses.
        """
        with self._cond:
            self._cond.wait_for(self._idle, timeout=5)
            return self._jobs

    def _idle(self) -> bool:
        if self._pending:
            return False
        try:
            waiting, _, _ = select.select([self._sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return not waiting

    def _serve(self):
        while not self._stop:
            try:
                ready, _, _ = select.select([self._sock], [], [], 0.05)
            except (OSError, ValueError):
                return
            if not ready:
                continue
            with self._cond:
                try:
                    conn, _ = self._sock.accept()
                except OSError:
                    return
                self._pending += 1
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        payload = b""
        try:
            with conn:
                conn.settimeout(3)
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    if chunk[:2] == b"\x10\x04" and len(chunk) == 3:
                        conn.sendall(bytes([self.status[chunk[2] - 1]]))
                        continue
                    payload += chunk
        finally:
            with self._cond:
                if payload:
                    self._jobs.append(payload)
                self._pending -= 1
                self._cond.notify_all()

    def close(self):
        self._stop = True
        self._sock.close()


# Strip every ESC/POS control sequence so a test can read the printable text
# back out of the byte stream.
CONTROL = re.compile(
    rb"\x1b[!aEM\-d]." rb"|\x1b[@2]" rb"|\x1d![\x00-\xff]" rb"|\x1dV[\x00-\xff]{1,2}" rb"|\x1bt."
)


def printed_lines(data: bytes) -> list[str]:
    """Return the printable lines of a byte stream, control codes removed.

    Args:
        data: Raw ESC/POS byte stream.

    Returns:
        One decoded string per line, trailing blank lines included.
    """
    return [line.decode("cp858", "replace") for line in CONTROL.sub(b"", data).split(b"\n")]


@pytest.fixture
def client(tmp_path):
    """A Flask test client with no printer and a throwaway state directory."""
    app = create_app(printer=None, state_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def printing_client(tmp_path):
    """A client that believes in a printer, without one being reachable.

    Port 1 is chosen because nothing listens there, so send_to_printer fails
    fast and the 502 path can be exercised without a socket of our own.
    """
    app = create_app(printer="192.0.2.50:1", state_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def fake_printer():
    """A printer on the loopback interface, healthy to begin with."""
    printer = FakePrinter()
    yield printer
    printer.close()


@pytest.fixture
def printing_app(tmp_path, fake_printer):
    """A client wired to the fake printer, plus that printer and its state dir."""
    app = create_app(printer=f"127.0.0.1:{fake_printer.port}", state_dir=tmp_path)
    app.config["TESTING"] = True
    return app.test_client(), fake_printer, tmp_path


@pytest.fixture(params=sorted(SIZES))
def size(request):
    """Runs a test once per type size."""
    return SIZES[request.param]
