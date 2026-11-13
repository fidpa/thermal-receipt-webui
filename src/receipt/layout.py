#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Layout primitives for 80 mm receipts.

Every attribute lives in its own ESC/POS register: ESC M for the font, GS ! for
the size, ESC E for bold, ESC - for underline, ESC a for alignment. The older
ESC ! packs font, size and bold into a single byte and writes all three at once,
so turning bold on would have reset any global scaling back to single width.

Do not go through Escpos.set(): it has no way back out of double width or
height. double_width=False sends nothing at all, the state stays, and everything
after it prints double width too. Only normal_textsize=True resets.

There is no italic. ESC/POS has no italic command and the character generator has
no italic face. ESC 4 is ESC/P, from the dot matrix world, and does nothing here.

No primitive writes more columns into a line than the receipt is wide. The printer
wraps an over-long line by itself, the preview renderer clips it at the edge, and
the two would drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from escpos.printer import Dummy

WIDTH_DOTS = 576

FONT_A = 0
FONT_B = 1
# Cell width in dots, from the TM-T20IV datasheet: Font A 12x24, Font B 9x17.
CELL_WIDTH: dict[int, int] = {FONT_A: 12, FONT_B: 9}

ALIGN_LEFT = 0
ALIGN_CENTER = 1
ALIGN_RIGHT = 2

MAX_SCALE = 8  # GS ! scales width and height 1x to 8x
UNDERLINE_THIN = 1  # ESC - n: 1 = single line, 2 = twice as thick

_WHITESPACE = re.compile(r"(\s+)")


@dataclass(frozen=True)
class Emphasis:
    """How a run of text is marked up.

    Italic is missing on purpose: the device cannot do it.
    """

    bold: bool = False
    underline: bool = False


PLAIN = Emphasis()

# A run of text together with its emphasis.
Segment = tuple[str, Emphasis]


@dataclass(frozen=True)
class Size:
    """Type size of a receipt: a font plus a scaling factor."""

    font: int
    scale: int

    @property
    def cols(self) -> int:
        """Characters per line that fit on 80 mm paper at this size."""
        return WIDTH_DOTS // (CELL_WIDTH[self.font] * self.scale)


SIZES: dict[str, Size] = {
    "small": Size(FONT_B, 1),  # 64 columns
    "normal": Size(FONT_A, 1),  # 48 columns
    "large": Size(FONT_A, 2),  # 24 columns
}
DEFAULT_SIZE = "normal"


def cut_sequence(size: Size | None = None) -> bytes:
    """A cut with no content, to sever a roll left standing.

    Only cut() knows the bytes of a cut this way. The initialisation of an empty
    receipt rides along - it sets code page, font and alignment back to their
    defaults and does not disturb the receipt that follows, which does the same.

    Args:
        size: Type size; the normal one when omitted.

    Returns:
        A raw ESC/POS byte stream that only cuts.
    """
    return Receipt(size).cut().data


class Receipt:
    """Collects layout calls and hands them out as an ESC/POS byte stream."""

    def __init__(self, size: Size | None = None) -> None:
        """Start a receipt at the requested type size.

        Args:
            size: One of SIZES; the normal size when omitted.
        """
        self.size = size or SIZES[DEFAULT_SIZE]
        self.cols = self.size.cols
        self.printer = Dummy()
        self.printer.magic.force_encoding("CP858")  # deterministic umlauts
        self._font(self.size.font)
        self._scale(self.size.scale)
        self._bold(False)
        self._underline(False)
        self._align(ALIGN_LEFT)

    # -- State -------------------------------------------------------------
    def _font(self, font: int) -> None:
        """Select font A or B via ESC M."""
        self.printer._raw(b"\x1bM" + bytes([font]))  # noqa: SLF001

    def _scale(self, factor: int) -> None:
        """Set width and height together via GS ! (1 to 8)."""
        value = min(max(factor, 1), MAX_SCALE) - 1
        self.printer._raw(b"\x1d!" + bytes([(value << 4) | value]))  # noqa: SLF001

    def _stretch(self, height: int) -> None:
        """Set the height independently of the width via GS !.

        Headings grow taller but not wider, so the column count of the receipt
        stays the same at every heading level.
        """
        width = min(max(self.size.scale, 1), MAX_SCALE) - 1
        tall = min(max(self.size.scale * height, 1), MAX_SCALE) - 1
        self.printer._raw(b"\x1d!" + bytes([(width << 4) | tall]))  # noqa: SLF001

    def _bold(self, enabled: bool) -> None:
        """Switch bold via ESC E."""
        self.printer._raw(b"\x1bE" + bytes([1 if enabled else 0]))  # noqa: SLF001

    def _underline(self, enabled: bool) -> None:
        """Switch underline via ESC -.

        The printer underlines the spaces between words as well; the preview
        renders it the same way.
        """
        self.printer._raw(  # noqa: SLF001
            b"\x1b-" + bytes([UNDERLINE_THIN if enabled else 0])
        )

    def _align(self, align: int) -> None:
        """Set alignment via ESC a."""
        self.printer._raw(b"\x1ba" + bytes([align]))  # noqa: SLF001

    # -- Primitives --------------------------------------------------------
    def heading(self, text: str, level: int = 1) -> Receipt:
        """Write a heading at one of three levels.

        Level 1 is centered and double height, level 2 centered, level 3 flush
        left. All three are bold and stay on a single line.

        Args:
            text: The heading, truncated to the receipt width.
            level: 1 to 3; larger values are clamped to 3.

        Returns:
            The receipt itself, so calls can be chained.
        """
        step = min(max(level, 1), 3)
        if step < 3:
            self._align(ALIGN_CENTER)
        if step == 1:
            self._stretch(2)
        self._bold(True)
        self.printer.text(text[: self.cols] + "\n")
        self._bold(False)
        if step == 1:
            self._scale(self.size.scale)
        if step < 3:
            self._align(ALIGN_LEFT)
        return self

    def title(self, text: str) -> Receipt:
        """Centered heading, bold and double height (level 1)."""
        return self.heading(text, 1)

    def big(self, text: str) -> Receipt:
        """Centered heading at double width and height.

        Double width halves the line. Usable for single words, not for
        sentences or rules.
        """
        self._align(ALIGN_CENTER)
        self._bold(True)
        self._scale(self.size.scale * 2)
        self.printer.text(text[: self.cols // 2] + "\n")
        self._scale(self.size.scale)
        self._bold(False)
        self._align(ALIGN_LEFT)
        return self

    def bold(self, text: str) -> Receipt:
        """Bold paragraph, wrapped to the column width."""
        return self.rich([(text, Emphasis(bold=True))])

    def center(self, text: str) -> Receipt:
        """A single centered line."""
        self._align(ALIGN_CENTER)
        self.printer.text(text[: self.cols] + "\n")
        self._align(ALIGN_LEFT)
        return self

    def columns(self, left: str, right: str = "") -> Receipt:
        """A line with a left and a right aligned part, padded with spaces."""
        padding = self.cols - len(left) - len(right)
        self.printer.text((left + " " * max(padding, 1) + right)[: self.cols] + "\n")
        return self

    def rule(self, char: str = "-") -> Receipt:
        """A rule across the full width."""
        self.printer.text(char * self.cols + "\n")
        return self

    def para(self, text: str, indent: str = "") -> Receipt:
        """Paragraph, word wrapped to the receipt width minus the indent."""
        return self.rich([(text, PLAIN)], indent)

    def rich(self, segments: Iterable[Segment], indent: str = "") -> Receipt:
        """Paragraph with changing emphasis, wrapped to the receipt width.

        Wrapping runs across segment boundaries: emphasis in the middle of a
        sentence does not force a line break.

        Args:
            segments: Runs of text with their emphasis.
            indent: Indent; reduces the usable width accordingly. An indent
                wider than the receipt is shortened.

        Returns:
            The receipt itself, so calls can be chained.
        """
        # An indent wider than the receipt would overflow every line even though
        # the wrapping itself counts correctly: the printer would wrap it, the
        # preview would cut it off.
        padding = indent[: max(self.cols - 1, 0)]
        width = max(self.cols - len(padding), 1)
        for line in _wrap(segments, width):
            if padding:
                self.printer.text(padding)
            current = PLAIN
            for text, wanted in line:
                if wanted.bold != current.bold:
                    self._bold(wanted.bold)
                if wanted.underline != current.underline:
                    self._underline(wanted.underline)
                current = wanted
                self.printer.text(text)
            if current.bold:
                self._bold(False)
            if current.underline:
                self._underline(False)
            self.printer.text("\n")
        return self

    def feed(self, lines: int = 1) -> Receipt:
        """Blank lines."""
        self.printer.ln(lines)
        return self

    def cut(self) -> Receipt:
        """Cut at the end of the receipt.

        The cut feeds generously: three blank lines plus the six python-escpos
        adds itself, so the last text sits above the tear bar - about 3.3 cm in
        all. That is what a receipt someone picks up should look like.

        A book coming out as one continuous roll needs no cut at all. A partial
        cut (GS V 66) was measured on a TM-T20IV on 2026-09-20 and removed
        again: it does leave the receipts joined, but it pulls about 1 cm of
        paper per seam and tears a hole into the page. 'none' wins on every
        count.

        Returns:
            The receipt itself, so calls can be chained.
        """
        self.printer.ln(3)
        self.printer.cut()
        return self

    @property
    def data(self) -> bytes:
        """The finished ESC/POS byte stream."""
        result: bytes = self.printer.output
        return result


def _words(segments: Iterable[Segment]) -> list[list[Segment]]:
    """Split segments into words that carry their emphasis along.

    Args:
        segments: Runs of text with their emphasis.

    Returns:
        One list of pieces per word. A word has several pieces when the
        emphasis changes in the middle of it.
    """
    words: list[list[Segment]] = []
    current: list[Segment] = []

    for text, emphasis in segments:
        for part in _WHITESPACE.split(text):
            if not part:
                continue
            if part.isspace():
                if current:
                    words.append(current)
                    current = []
            else:
                current.append((part, emphasis))

    if current:
        words.append(current)
    return words


def _length(word: list[Segment]) -> int:
    """Character count of a word across all of its pieces."""
    return sum(len(text) for text, _ in word)


def _split_long(word: list[Segment], width: int) -> list[list[Segment]]:
    """Break a word that is wider than one line.

    Without this cut the line would be too long: the printer wraps it by itself
    while the preview renderer clips it.

    Args:
        word: The pieces of one word.
        width: Usable column count.

    Returns:
        The parts of the word, each at most width characters long.
    """
    pieces: list[list[Segment]] = [[]]
    room = width

    for text, emphasis in word:
        rest = text
        while rest:
            taken, rest = rest[:room], rest[room:]
            pieces[-1].append((taken, emphasis))
            room -= len(taken)
            if room == 0:
                pieces.append([])
                room = width

    return [piece for piece in pieces if piece]


def _wrap(segments: Iterable[Segment], width: int) -> list[list[Segment]]:
    """Wrap segments to the column width, greedily, the way textwrap does.

    The separating space inherits the emphasis only when both neighbours share
    it. Otherwise an underlined gap would sit between two words that nobody
    marked up.

    Args:
        segments: Runs of text with their emphasis.
        width: Usable column count.

    Returns:
        One list of pieces per line, separating spaces included.
    """
    lines: list[list[Segment]] = []
    line: list[Segment] = []
    filled = 0

    for word in _words(segments):
        parts = _split_long(word, width) if _length(word) > width else [word]
        for part in parts:
            needed = _length(part) + (1 if line else 0)
            if line and filled + needed > width:
                lines.append(line)
                line, filled, needed = [], 0, _length(part)
            if line:
                previous = line[-1][1]
                line.append((" ", previous if previous == part[0][1] else PLAIN))
            line.extend(part)
            filled += needed

    if line:
        lines.append(line)
    return lines or [[]]
