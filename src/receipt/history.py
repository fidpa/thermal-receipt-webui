#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""History of recently printed texts.

One JSONL file, newest entry first. The point is reprinting without retyping,
not an archive: older entries drop out without asking.

A write error must not fail the print. By that time the paper is already out, so
the methods log the error and return instead of raising.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

FILENAME = "history.jsonl"
# StateDirectory of the systemd unit. The service creates and owns it; the
# command line runs as the same user and therefore writes the same history.
# Otherwise there would be two lists of what came out of the device.
SYSTEM_STATE_DIR = Path("/var/lib/thermal-receipt-webui")

DEFAULT_LIMIT = 20
# Generous on purpose: the visible truncation is done by the web interface with
# an ellipsis that follows the actual column width. This value only caps what
# ends up in the file.
LABEL_LENGTH = 120
DEFAULT_SIZE_KEY = "normal"

# Strip emphasis markers for the label; the underscore only with word
# boundaries, so a snake_case_name stays readable (same rule as in markup.py).
_MARKUP = re.compile(r"\*\*(.+?)\*\*|(?<!\w)_(.+?)_(?!\w)")

logger = logging.getLogger(__name__)


def default_state_dir() -> Path:
    """Where the history lives when nobody says otherwise.

    RECEIPT_STATE_DIR beats everything, which is how the unit sets the path.
    Failing that the state directory of the service, as long as it is writable;
    only then the user directory, on a machine without the service.

    Returns:
        Directory for the history file.
    """
    override = os.environ.get("RECEIPT_STATE_DIR")
    if override:
        return Path(override)
    if os.access(SYSTEM_STATE_DIR, os.W_OK):
        return SYSTEM_STATE_DIR
    return Path.home() / ".local" / "state" / "thermal-receipt-webui"


class History:
    """The last printed texts, oldest ones drop out."""

    def __init__(self, path: Path, limit: int = DEFAULT_LIMIT) -> None:
        """Set up the history without touching the file.

        Args:
            path: Target file in JSONL format.
            limit: How many entries are kept.
        """
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()

    def entries(self) -> list[dict[str, Any]]:
        """Read the history.

        Returns:
            Entries with printed_at, label, size and text, newest first. Empty
            when the file is missing or unreadable.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.error("history %s unreadable: %s", self.path, exc)
            return []

        items: list[dict[str, Any]] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("history %s: skipped an unreadable line", self.path)
                continue
            # Valid JSON is not yet an entry: a number or a list used to bring
            # down the history and with it add(), after the print, with the
            # paper already in hand.
            if not isinstance(item, dict):
                logger.warning("history %s: skipped a line holding no entry", self.path)
                continue
            # Entries from before the size switch existed carry no size
            item.setdefault("size", DEFAULT_SIZE_KEY)
            items.append(item)
        return items

    def add(self, text: str, size: str) -> None:
        """Put a printed text at the front of the history.

        The same text at the same size moves to the front rather than appearing
        twice; at a different size it is a separate entry, because otherwise a
        click on the history would silently print a different receipt.

        Args:
            text: The printed text in the Markdown subset.
            size: Key of the type size it was printed at.
        """
        entry = {
            "printed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "label": label_for(text),
            "size": size,
            "text": text,
        }
        with self._lock:
            items = [
                item
                for item in self.entries()
                if (item.get("text"), item.get("size")) != (text, size)
            ]
            items.insert(0, entry)
            self._write(items[: self.limit])

    def _write(self, items: list[dict[str, Any]]) -> None:
        """Write the file atomically, so a crash cannot halve it."""
        temporary = self.path.with_name(self.path.name + ".tmp")
        payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in items)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as exc:
            logger.error("history %s not writable: %s", self.path, exc)


def label_for(text: str) -> str:
    """Reduce a text to one line for the history list.

    Args:
        text: The printed text.

    Returns:
        The first non-empty line without markup characters, roughly capped.
    """
    for line in text.splitlines():
        stripped = _MARKUP.sub(lambda m: m.group(1) or m.group(2), line.strip().lstrip("#")).strip()
        if stripped and not set(stripped) <= {"-", "*", "_"}:
            return stripped[:LABEL_LENGTH]
    return "(no text)"
