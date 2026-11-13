#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""An append-only record of every print attempt over HTTP.

The history in the interface holds 20 entries and exists for reprinting without
retyping. A book of 38 receipts would flush it, which is why an automated sender
stays out of it - and why there has to be a second list that forgets nothing:
what was accepted or refused, when, and from where.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

FILENAME = "audit.jsonl"
# A record runs to about 300 bytes, so 5 MB are over 15,000 print attempts. Two
# files, nothing older is kept - a service without an upper bound eventually
# fills the state directory.
MAX_BYTES = 5_000_000

logger = logging.getLogger(__name__)


class Audit:
    """A JSON Lines file that is only ever appended to."""

    def __init__(self, path: Path) -> None:
        """Set up the record without touching the file.

        Args:
            path: Target file in JSONL format.
        """
        self.path = path
        self._lock = threading.Lock()

    def emit(
        self,
        event: str,
        *,
        result: str,
        src_ip: str,
        method: str = "",
        path: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one record.

        A write error must not fail a print; by that time the paper is already
        out. So an error goes to the log and the method returns instead of
        raising.

        Args:
            event: What happened, such as 'print' or 'duplicate'.
            result: 'ok' or the error key of the response.
            src_ip: Source IP of the sender.
            method: HTTP method of the request.
            path: Path of the request.
            payload: Further fields of the operation.
        """
        now = time.time()
        record: dict[str, Any] = {
            "ts": int(now),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "src_ip": src_ip,
            "method": method,
            "path": path,
            "event": event,
            "result": result,
        }
        if payload is not None:
            record["payload"] = payload
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._roll()
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError as exc:
                logger.error("record %s not writable: %s", self.path, exc)

    def _roll(self) -> None:
        """Move the file aside as soon as it reaches the upper bound."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size < MAX_BYTES:
            return
        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
        logger.info("record %s rotated (%d bytes)", self.path, size)
