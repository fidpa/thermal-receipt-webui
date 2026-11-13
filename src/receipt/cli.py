#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Render a receipt as a PNG or send it to an ESC/POS printer.

Usage:
    python -m receipt.cli flashcard --preview /tmp/card.png
    python -m receipt.cli door-sign --host 192.0.2.50
    RECEIPT_PRINTER=192.0.2.50 python -m receipt.cli door-sign
    echo "Milk, bread, coffee" | python -m receipt.cli - --title Shopping --host 192.0.2.50
    python -m receipt.cli - --text "A short note" --preview /tmp/note.png
    python -m receipt.cli - --text "Reminder" --size large --preview /tmp/large.png
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .history import FILENAME, History, default_state_dir
from .layout import DEFAULT_SIZE, SIZES
from .markup import from_text
from .preview import paper_cm, render
from .printer import parse_target, send_receipt
from .samples import SAMPLES

logger = logging.getLogger(__name__)


def write_preview(data: bytes, target: Path) -> bool:
    """Render the byte stream as a PNG.

    Args:
        data: Raw ESC/POS byte stream.
        target: Destination file.

    Returns:
        True on success, False when the image could not be written.
    """
    try:
        image = render(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
    except OSError as exc:
        logger.error("preview %s not writable: %s", target, exc)
        return False
    logger.info(
        "%s  %dx%d px  %.1f cm of paper  (%d ESC/POS bytes)",
        target,
        image.width,
        image.height,
        paper_cm(image.height),
        len(data),
    )
    return True


def main() -> int:
    """Parse the command line, then render and/or print.

    Returns:
        0 on success, 1 on error.
    """
    parser = argparse.ArgumentParser(
        # Without prog, argparse takes the basename of sys.argv[0] and the help
        # text says "cli.py", which is not how anybody starts this.
        prog="python -m receipt.cli",
        description="Render a receipt as a PNG or send it to an ESC/POS printer.",
    )
    parser.add_argument(
        "sample",
        choices=[*sorted(SAMPLES), "-"],
        help="which sample receipt, or '-' for free text from --text or stdin",
    )
    parser.add_argument("--text", help="free text (only with '-'); without it stdin is read")
    parser.add_argument("--title", default="", help="heading for the free text receipt")
    parser.add_argument(
        "--size",
        choices=sorted(SIZES),
        default=DEFAULT_SIZE,
        help=f"type size for the free text receipt (default {DEFAULT_SIZE})",
    )
    parser.add_argument("--preview", type=Path, help="write a PNG preview to this file")
    parser.add_argument(
        "--host",
        help="printer address, optionally with :port; without it RECEIPT_PRINTER is used",
    )
    parser.add_argument("--verbose", action="store_true", help="debug output")
    args = parser.parse_args()

    # force=True, because importing escpos.printer already installs a handler on
    # the root logger and sets level WARNING. Without force, basicConfig has no
    # effect and INFO messages disappear.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        force=True,
    )

    # The same variable the systemd unit carries: the printer address lives in
    # one place, not in two that can drift apart.
    host, port = parse_target(args.host or os.environ.get("RECEIPT_PRINTER"))
    if not args.preview and not host:
        parser.error("give at least --preview or --host (or set RECEIPT_PRINTER)")

    text = ""
    if args.sample == "-":
        text = args.text if args.text is not None else sys.stdin.read()
        if not text.strip():
            logger.error("no text received (neither --text nor stdin)")
            return 1
        data = from_text(text, args.title, SIZES[args.size]).data
    else:
        data = SAMPLES[args.sample]().data

    ok = True
    if args.preview:
        ok = write_preview(data, args.preview) and ok
    if host and send_receipt(host, data, port):
        # Into the same history as the interface, so there is one list of what
        # came out of the device. Free text only: a sample receipt is built from
        # calls, not from text a reprint could repeat.
        if text.strip():
            History(default_state_dir() / FILENAME).add(text, args.size)
    elif host:
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
