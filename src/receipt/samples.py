#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Sample receipts for checking the layout before a printer is connected.

Between them the three samples exercise every primitive in layout.py and all
three type sizes, which is what makes them useful as a test bench. They are not
reachable from the web interface, only through the command line.
"""

from __future__ import annotations

from collections.abc import Callable

from .layout import SIZES, Receipt


def shopping_list() -> Receipt:
    """Everyday note: heading, rule, bold inside a sentence, flowing text."""
    receipt = Receipt(SIZES["normal"])
    receipt.title("Shopping list")
    receipt.center("Saturday morning")
    receipt.rule("=")
    receipt.feed()
    receipt.bold("Do not forget the yeast.")
    receipt.feed()
    receipt.para(
        "Milk, bread, coffee, two lemons, oil, onions, carrots, apples and a "
        "jar of pickles for Sunday."
    )
    receipt.feed()
    receipt.rule()
    receipt.columns("Budget", "25.00")
    receipt.columns("Spent last week", "23.40")
    receipt.feed()
    receipt.para("Market closes at 14:00, the bakery an hour earlier.", indent="  ")
    receipt.cut()
    return receipt


def flashcard() -> Receipt:
    """Study card: question on top, fold line in the middle, answer below."""
    receipt = Receipt(SIZES["normal"])
    receipt.columns("Package management", "Card 12")
    receipt.rule("=")
    receipt.feed()
    receipt.big("QUESTION")
    receipt.feed()
    receipt.para(
        "Which command removes an installed Debian package but leaves its "
        "configuration files on the system?"
    )
    receipt.feed(4)
    receipt.rule(".")  # fold line
    receipt.feed(4)
    receipt.big("ANSWER")
    receipt.feed()
    receipt.bold("dpkg --remove <package>")
    receipt.feed()
    receipt.para(
        "Short form: dpkg -r. Only dpkg --purge (-P) deletes the conffiles as "
        "well. apt remove behaves like dpkg -r, apt purge like dpkg -P."
    )
    receipt.feed()
    receipt.para("See also: dpkg, apt-get, apt, dpkg-reconfigure", indent="  ")
    receipt.cut()
    return receipt


def door_sign() -> Receipt:
    """A few words at the largest size, to be taped somewhere."""
    receipt = Receipt(SIZES["large"])
    receipt.title("Wet paint")
    receipt.rule()
    receipt.feed()
    receipt.para("Please use the side door until Friday.")
    receipt.cut()
    return receipt


SAMPLES: dict[str, Callable[[], Receipt]] = {
    "shopping-list": shopping_list,
    "flashcard": flashcard,
    "door-sign": door_sign,
}
