#!/usr/bin/env python3
# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Render an ESC/POS byte stream as an image, the way a TM-T20IV prints it.

Deliberately no layout model of its own: what gets rendered is exactly the byte
stream that later goes to port 9100 unchanged. What the preview shows is what
comes out of the printer.

Device assumptions for the TM-T20IV, 80 mm roll, 203 dpi, taken from the
datasheet and not yet measured against a device:

    print width 576 dots
    Font A = 12x24 dots  ->  48 characters per line
    Font B =  9x17 dots  ->  64 characters per line
"""

from __future__ import annotations

from collections.abc import Callable

from PIL import Image, ImageDraw, ImageFont

WIDTH_DOTS = 576
DPI = 203  # TM-T20IV, horizontally as well as vertically

FONT_A = 0
FONT_B = 1
FONT_CELLS: dict[int, tuple[int, int]] = {FONT_A: (12, 24), FONT_B: (9, 17)}

CELL_H = FONT_CELLS[FONT_A][1]

# What the printer feeds per line, regardless of the font. layout.py does not
# set the line spacing (no ESC 2, no ESC 3), so the device stays on its default.
# On a TM-T20IV that is 30 dots; two real prints measured on 19.09.2026 came out
# at 29.3 and 29.4 dots per line, the same in both fonts.
#
# Until then the renderer used the cell height (17 or 24) and so reported every
# receipt as too short - by 76 % in "small" and 25 % in "normal". That hit not
# only the image but, through length_cm, the paper budget as well.
#
# When a character is taller than the feed, the device advances by the character
# height instead; otherwise double-height headings would overlap. Hence max().
LINE_FEED_DOTS = 30

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
# DejaVu Mono has an advance of 0.6023 em, so the point size follows from the
# cell width: 12 dots -> 20 (12.05 px), 9 dots -> 15 (9.03 px).
FONT_ADVANCE_RATIO = 0.6023

_GLYPH_CACHE: dict[tuple[str, bool, int], Image.Image] = {}


def _byte(data: bytes, index: int) -> int:
    """Read one byte of the stream, or 0 when it ends before that.

    A byte stream can break off mid-sequence: captured half-way, transmitted
    half-way. The preview should still produce an image then, the same way it
    skips sequences it does not know instead of failing.

    Args:
        data: Raw ESC/POS byte stream.
        index: Read position.

    Returns:
        The byte at that position, 0 past the end.
    """
    return data[index] if index < len(data) else 0


def _glyph(char: str, bold: bool, font: int) -> Image.Image:
    """Return one character as a tile the size of the font's cell.

    Args:
        char: The character to render.
        bold: True for the bold face.
        font: FONT_A or FONT_B.

    Returns:
        A greyscale tile in cell size. Cached, because receipts repeat
        themselves a lot (rules, spaces).
    """
    key = (char, bold, font)
    if key not in _GLYPH_CACHE:
        cell_w, cell_h = FONT_CELLS[font]
        face = ImageFont.truetype(
            FONT_BOLD if bold else FONT_REGULAR, round(cell_w / FONT_ADVANCE_RATIO)
        )
        tile = Image.new("L", (cell_w, cell_h), 255)
        ImageDraw.Draw(tile).text((0, 1), char, font=face, fill=0)
        _GLYPH_CACHE[key] = tile
    return _GLYPH_CACHE[key]


class PrintState:
    """The print mode at one point in the byte stream."""

    def __init__(self) -> None:
        self.bold: bool = False
        self.underline: bool = False
        self.align: int = 0  # 0 = left, 1 = center, 2 = right
        self.width_mul: int = 1
        self.height_mul: int = 1
        self.font: int = FONT_A

    def copy(self) -> PrintState:
        """Return an independent copy of the state."""
        clone = PrintState()
        clone.__dict__.update(self.__dict__)
        return clone


def _cell(state: PrintState) -> tuple[int, int]:
    """Dots one character occupies in this state (font times magnification)."""
    cell_w, cell_h = FONT_CELLS[state.font]
    return cell_w * state.width_mul, cell_h * state.height_mul


def render(data: bytes, encoding: str = "cp858") -> Image.Image:
    """Translate ESC/POS bytes into a preview image.

    Unknown control sequences are skipped rather than raising: the preview
    should still produce an image when a sequence is not implemented yet.

    Args:
        data: Raw ESC/POS byte stream, for instance from escpos.printer.Dummy.
        encoding: Code page used to decode printable characters. CP858 covers
            the German umlauts and is what layout.py forces.

    Returns:
        A greyscale image at print width (576 px), height following the content.
    """
    lines: list[list[tuple[str, PrintState]]] = [[]]
    # Height of a line that stays empty. An empty line has no character to read
    # the cell metrics from, and a fixed fallback would advance a blank line in
    # font B by font A's 24 dots while the printer advances 17.
    blank_heights: list[int] = [FONT_CELLS[FONT_A][1]]
    cuts: set[int] = set()
    state = PrintState()
    pos = 0

    def newline(count: int = 1) -> None:
        for _ in range(count):
            # The state at the line feed is the one that applies to the line
            # being closed, so a font switch before an empty line counts.
            blank_heights[-1] = _cell(state)[1]
            lines.append([])
            blank_heights.append(_cell(state)[1])

    while pos < len(data):
        byte = data[pos]

        if byte == 0x0A:  # LF
            newline()
            pos += 1
        elif byte == 0x1B:  # ESC
            pos = _handle_esc(data, pos, state, newline)
        elif byte == 0x1D:  # GS
            pos, cut_here = _handle_gs(data, pos, state)
            if cut_here:
                cuts.add(len(lines) - 1)
        elif byte in (0x0D, 0x00):
            pos += 1
        else:
            char = bytes([byte]).decode(encoding, errors="replace")
            lines[-1].append((char, state.copy()))
            pos += 1

    return _paint(lines, cuts, blank_heights)


def _handle_esc(data: bytes, pos: int, state: PrintState, newline: Callable[[int], None]) -> int:
    """Process one ESC sequence and return the new read position."""
    cmd = _byte(data, pos + 1)

    if cmd == 0x40:  # ESC @  initialize
        state.__dict__.update(PrintState().__dict__)
        return pos + 2
    if cmd == 0x61:  # ESC a  alignment
        state.align = _byte(data, pos + 2)
        return pos + 3
    if cmd == 0x45:  # ESC E  bold
        state.bold = bool(_byte(data, pos + 2))
        return pos + 3
    if cmd == 0x2D:  # ESC -  underline
        state.underline = bool(_byte(data, pos + 2))
        return pos + 3
    if cmd == 0x64:  # ESC d  feed n lines
        newline(_byte(data, pos + 2))
        return pos + 3
    if cmd == 0x21:  # ESC !  print mode (font, size AND bold in one byte)
        mode = _byte(data, pos + 2)
        state.font = FONT_B if mode & 0x01 else FONT_A
        state.height_mul = 2 if mode & 0x10 else 1
        state.width_mul = 2 if mode & 0x20 else 1
        state.bold = bool(mode & 0x08)
        return pos + 3
    if cmd == 0x4D:  # ESC M  font A or B
        state.font = FONT_B if _byte(data, pos + 2) in (1, 0x31) else FONT_A
        return pos + 3
    if cmd == 0x32:  # ESC 2  default line spacing
        return pos + 2
    if cmd in (0x74, 0x7B, 0x33):  # code page, rotation, line spacing
        return pos + 3
    return pos + 2


def _handle_gs(data: bytes, pos: int, state: PrintState) -> tuple[int, bool]:
    """Process one GS sequence.

    Returns:
        The new read position and a flag saying whether the paper is cut here.
    """
    cmd = _byte(data, pos + 1)

    if cmd == 0x21:  # GS !  character size
        size = _byte(data, pos + 2)
        state.width_mul = (size >> 4) + 1
        state.height_mul = (size & 0x0F) + 1
        return pos + 3, False
    if cmd == 0x56:  # GS V  paper cut
        has_feed = _byte(data, pos + 2) in (0x41, 0x42)
        return pos + (4 if has_feed else 3), True
    if cmd == 0x28:  # GS (  extended commands, QR codes among them
        payload = _byte(data, pos + 3) | (_byte(data, pos + 4) << 8)
        return pos + 5 + payload, False
    if cmd == 0x76 and _byte(data, pos + 2) == 0x30:  # GS v 0  raster image
        # Header GS v 0 m xL xH yL yH, then (xL + xH*256) * (yL + yH*256) bytes
        # of image data. The image is not drawn, but it is measured exactly:
        # otherwise it swallowed everything after it and the preview showed less
        # than the printer prints.
        width_bytes = _byte(data, pos + 4) | (_byte(data, pos + 5) << 8)
        height_dots = _byte(data, pos + 6) | (_byte(data, pos + 7) << 8)
        return pos + 8 + width_bytes * height_dots, False
    return pos + 3, False


def _paint(
    lines: list[list[tuple[str, PrintState]]],
    cuts: set[int],
    blank_heights: list[int],
) -> Image.Image:
    """Paint the collected lines into an image."""
    heights = [
        max(LINE_FEED_DOTS, max((_cell(s)[1] for _, s in line), default=blank_heights[index]))
        for index, line in enumerate(lines)
    ]
    image = Image.new("L", (WIDTH_DOTS, max(sum(heights), CELL_H)), 255)
    draw = ImageDraw.Draw(image)
    y = 0

    for index, line in enumerate(lines):
        row_height = heights[index]
        if line:
            used = sum(_cell(s)[0] for _, s in line)
            align = line[0][1].align
            offsets = {0: 0, 1: (WIDTH_DOTS - used) // 2, 2: WIDTH_DOTS - used}
            x = max(offsets.get(align, 0), 0)

            for char, style in line:
                width, height = _cell(style)
                if char != " " and x + width <= WIDTH_DOTS:
                    tile = _glyph(char, style.bold, style.font)
                    if (style.width_mul, style.height_mul) != (1, 1):
                        # NEAREST, because the print head doubles dots rather
                        # than smoothing them
                        tile = tile.resize((width, height), Image.NEAREST)
                    image.paste(tile, (x, y))
                if style.underline and x + width <= WIDTH_DOTS:
                    draw.line([(x, y + height - 2), (x + width - 1, y + height - 2)], fill=0)
                x += width

        if index in cuts:
            draw.line([(0, y + row_height - 1), (WIDTH_DOTS, y + row_height - 1)], fill=128)
        y += row_height

    return image


def paper_cm(height_dots: int) -> float:
    """Convert the image height into centimetres of paper.

    Counts only what the byte stream produces. What the printer feeds on top of
    that, so the cut lands behind the last character, is added by the device and
    can only be measured there.

    Args:
        height_dots: Image height in dots, one dot per print head position.

    Returns:
        Paper length in centimetres.
    """
    return height_dots / DPI * 2.54
