#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""A Markdown subset for receipt text.

Four rules, all of them familiar from Markdown. There is deliberately no dialect
of its own to learn:

    # Heading          level 1: centered, bold, double height
    ## Heading         level 2: centered, bold
    ### Heading        level 3: flush left, bold
    ---                a rule across the full width
    **bold**           bold, in the middle of a sentence as well
    _underlined_       underline, in the middle of a sentence as well

Italic is missing because the device cannot do it: ESC/POS has no italic command
and the character generator has no italic face. Markdown otherwise reads _text_
as italic; here it is the emphasis that actually exists on paper. Both kinds of
emphasis nest in either order.

Typographic characters from an editor, a browser or a text file - en dashes,
ellipses, curly quotes - are mapped onto their ASCII image, because CP858 does
not know them. An emoji has no such image and stays a question mark.

Everything else is text. Consecutive lines form a paragraph and are wrapped to
the receipt width; the line breaks of the input are flattened in the process, so
a text area does not turn into ragged fragments. A blank line in the input is a
blank line on the paper.
"""

from __future__ import annotations

import re

from .layout import Emphasis, Receipt, Segment, Size

# Three levels are enough at receipt width; '####' and deeper land on level 3,
# so that '#### ' is never printed verbatim.
HEADING = re.compile(r"^\s*(#{1,6})\s+(.+)$")
MAX_HEADING_LEVEL = 3

RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")

# One expression for both kinds of emphasis, so they nest in either order:
# **_like this_** as well as _**like this**_.
# The underscore needs word boundaries, exactly as in Markdown, or a
# snake_case_name in the text would split itself into an emphasis.
EMPHASIS = re.compile(r"\*\*(.+?)\*\*|(?<!\w)_(.+?)_(?!\w)", re.DOTALL)


# Characters CP858 does not know but every text from an editor, a browser or a
# text file brings along. Without a replacement the encoder turns them into '?'.
# Only what is a typographic image of an ASCII character is translated: an emoji
# has none and stays a question mark.
_REPLACEMENTS = {
    # dashes
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    # quotes and apostrophes
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u2032": "'",
    "\u02bc": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2033": '"',
    "\u2039": "<",
    "\u203a": ">",
    # ellipsis and bullets
    "\u2026": "...",
    "\u2022": "-",
    "\u2023": "-",
    "\u25e6": "-",
    "\u2043": "-",
    # whitespace: fixed and narrow spaces become ordinary ones, zero width ones
    # and the BOM disappear (a text file brings that along).
    "\u00a0": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\u200b": "",
    "\u200c": "",
    "\u200d": "",
    "\ufeff": "",
    # arrows and comparisons, common in technical notes
    "\u2192": "->",
    "\u2190": "<-",
    "\u21d2": "=>",
    "\u21d0": "<=",
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2260": "!=",
    "\u2122": "(TM)",
}
# Control characters never reach the printer as text. ESC, GS, DLE and the rest
# are the command language of the device: passed through, they would act as
# commands the preview does not draw and the paper budget does not count. A tab
# becomes a space, because the device jumps to a tab stop the preview cannot
# know; the line feed is the one control character the layout needs.
_CONTROLS = {code: None for code in (*range(0x00, 0x20), *range(0x7F, 0xA0))}
_CONTROLS[ord("\t")] = " "
del _CONTROLS[ord("\n")]
_TRANSLATION = str.maketrans({**_REPLACEMENTS, **_CONTROLS})


def normalize(text: str) -> str:
    """Turn input text into what the printer can represent.

    Line endings are unified - browsers send breaks from a <textarea> as CRLF,
    and without normalizing them the paragraph split finds no boundary.
    Typographic characters are mapped onto their ASCII image, because CP858 does
    not know them and the encoder would otherwise print a '?'. Control
    characters other than the line feed are removed, a tab becomes a space.

    Runs before every layout call, never after: the replacements change the
    length ('...' is three characters where the ellipsis was one), and wrapping
    and truncation both count characters.

    Args:
        text: Input text, of any origin.

    Returns:
        Text with unified line endings and printable characters.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    return unified.translate(_TRANSLATION)


def inline(text: str, base: Emphasis | None = None) -> list[Segment]:
    """Split one line at **bold** and _underline_ markers.

    An unpaired ** or _ stays and gets printed. Error messages for half finished
    input would only be in the way while typing. A snake_case_name is left alone
    too, because the underscore needs word boundaries the way Markdown does.

    Args:
        text: A line or paragraph in the Markdown subset.
        base: The emphasis this text sits in, set when nesting.

    Returns:
        Runs of text with their emphasis.
    """
    style = base or Emphasis()
    segments: list[Segment] = []
    position = 0

    for match in EMPHASIS.finditer(text):
        if match.start() > position:
            segments.append((text[position : match.start()], style))
        if match.group(1) is not None:
            inner, nested = match.group(1), Emphasis(True, style.underline)
        else:
            inner, nested = match.group(2), Emphasis(style.bold, True)
        segments.extend(inline(inner, nested))
        position = match.end()

    if position < len(text):
        segments.append((text[position:], style))
    return segments


def longest_word(text: str) -> int:
    """Length of the longest unbroken word.

    A word wider than the receipt gets broken in the middle. The web interface
    warns about that instead of cutting it silently.

    Args:
        text: Input text.

    Returns:
        Character count of the longest word, 0 for empty text.
    """
    # Through inline() rather than by substitution: what is counted is what
    # gets printed.
    plain = "".join(piece for piece, _ in inline(normalize(text)))
    return max((len(word) for word in plain.split()), default=0)


def from_text(text: str, title: str = "", size: Size | None = None, cut: str = "full") -> Receipt:
    """Build a receipt from text in the Markdown subset.

    Args:
        text: The text to print.
        title: Optional heading; behaves like a leading '# ' line.
        size: Type size from layout.SIZES; the normal size when omitted.
        cut: 'full' cuts with a feed, 'none' leaves the paper standing -
            for a book as one continuous roll.

    Returns:
        A finished receipt, with or without a cut depending on `cut`.
    """
    receipt = Receipt(size)
    source = normalize(text)
    heading_line = normalize(title).strip()
    if heading_line:
        source = f"# {heading_line}\n\n{source}"

    paragraph: list[str] = []

    def flush() -> None:
        """Write out the collected paragraph and empty the buffer."""
        if paragraph:
            receipt.rich(inline(" ".join(paragraph)))
            paragraph.clear()

    for line in source.strip().split("\n"):
        heading = HEADING.match(line)
        if not line.strip():
            flush()
            receipt.feed()
        elif RULE.match(line):
            flush()
            receipt.rule()
        elif heading:
            flush()
            level = min(len(heading.group(1)), MAX_HEADING_LEVEL)
            receipt.heading(heading.group(2).strip(), level)
        else:
            paragraph.append(line.strip())

    flush()
    if cut == "full":
        receipt.cut()
    return receipt
