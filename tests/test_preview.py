# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the ESC/POS renderer.

The renderer interprets the byte stream rather than a layout model, so these
tests hand it raw sequences instead of going through the layout primitives.
"""

from __future__ import annotations

import pytest

from receipt.preview import (
    FONT_A,
    FONT_B,
    FONT_CELLS,
    LINE_FEED_DOTS,
    WIDTH_DOTS,
    TooLongError,
    paper_cm,
    render,
)


def test_render_width_is_the_print_width():
    """Every image is exactly as wide as the print head."""
    assert render(b"hello\n").width == WIDTH_DOTS


def test_both_fonts_advance_by_the_same_line_feed():
    """The device feeds its line spacing, not the cell height.

    Both cells are smaller than the feed, so a font B line takes exactly as much
    paper as a font A line. Until 19.09.2026 the renderer used the cell height
    here and reported every receipt as too short - by 76 % in font B.
    """
    tall = render(b"\x1bM\x00" + b"x" * 48 + b"\n").height
    short = render(b"\x1bM\x01" + b"x" * 64 + b"\n").height
    assert short == tall == LINE_FEED_DOTS * 2  # content line plus the empty one after LF


def test_a_character_taller_than_the_feed_wins():
    """Otherwise double-height headings would print into each other."""
    doubled = FONT_CELLS[FONT_A][1] * 2
    assert doubled > LINE_FEED_DOTS
    assert render(b"\x1d!\x11x\x1d!\x00\n").height == doubled + LINE_FEED_DOTS
    # Font B doubled is 34 dots, still above the feed but below font A doubled.
    assert render(b"\x1bM\x01\x1d!\x11x\x1d!\x00\n").height == (
        FONT_CELLS[FONT_B][1] * 2 + LINE_FEED_DOTS
    )


def test_gs_scaling_multiplies_the_line_height():
    """GS ! n scales width and height 1x to 8x.

    The size is reset before the line feed, so only the line holding the
    character is scaled and the trailing empty line stays at 1x.
    """
    single = render(b"x\n").height
    double = render(b"\x1d!\x11x\x1d!\x00\n").height
    triple = render(b"\x1d!\x22x\x1d!\x00\n").height
    assert single == LINE_FEED_DOTS * 2
    # The scaled line takes the character height, the trailing empty one the feed.
    assert double == FONT_CELLS[FONT_A][1] * 2 + LINE_FEED_DOTS
    assert triple == FONT_CELLS[FONT_A][1] * 3 + LINE_FEED_DOTS


def test_a_blank_line_takes_the_line_feed_too():
    """Nothing to print still advances the paper by one line."""
    assert render(b"\x1bM\x01\n").height == LINE_FEED_DOTS * 2
    assert render(b"\x1bM\x00\n").height == LINE_FEED_DOTS * 2


def test_a_blank_line_still_follows_the_scale_when_it_exceeds_the_feed():
    """The state at the line feed decides, which matters above 30 dots.

    A blank line has no character to read the metrics off; the renderer takes
    them from the state in effect when the feed arrives.
    """
    assert render(b"\x1d!\x11\n").height == FONT_CELLS[FONT_A][1] * 2 * 2


def test_esc_bang_also_selects_the_font():
    """Bit 0 of ESC ! picks font B, which the renderer has to follow.

    Read off a doubled line, because at single size both fonts sit below the
    line feed and the heights no longer tell them apart.
    """
    assert render(b"\x1b!\x11x\x1d!\x00\n").height == (FONT_CELLS[FONT_B][1] * 2 + LINE_FEED_DOTS)


def test_esc_at_resets_the_state():
    """ESC @ returns everything to the defaults."""
    assert render(b"\x1bM\x01\x1b@\x1d!\x11x\x1d!\x00\n").height == (
        FONT_CELLS[FONT_A][1] * 2 + LINE_FEED_DOTS
    )


def test_unknown_sequences_do_not_raise():
    """An unimplemented sequence is skipped, the preview still renders."""
    assert render(b"\x1b\x99\x00text\n").width == WIDTH_DOTS


def test_truncated_sequence_at_the_end_does_not_raise():
    """A byte stream cut off mid-sequence must not take the preview down."""
    assert render(b"text\n\x1b").width == WIDTH_DOTS


@pytest.mark.parametrize(
    "stream",
    [
        b"\x1ba",  # ESC a without its alignment byte
        b"\x1bE",  # ESC E without its on/off byte
        b"\x1bM",  # ESC M without its font byte
        b"\x1bd",  # ESC d without its line count
        b"\x1b!",  # ESC ! without its mode byte
        b"\x1d!",  # GS ! without its size byte
        b"\x1dV",  # GS V without its cut mode
        b"\x1d(",  # GS ( without its payload length
        b"text\n\x1d(k\x03",  # length announced, payload missing
    ],
)
def test_a_sequence_missing_its_parameters_still_renders(stream):
    """The parameter bytes can be missing too, not just the command byte.

    A stream captured half-way ends wherever it ends. Reading past that end used
    to raise IndexError, which turned a partial capture into no image at all.
    """
    assert render(stream).width == WIDTH_DOTS


def test_a_raster_image_does_not_swallow_the_rest_of_the_stream():
    """GS v 0 is measured, so what follows it still renders.

    The image itself leaves no mark, but skipping to the end of the stream made
    everything after it vanish from the preview while the printer still printed
    it - the one divergence the checksum cannot catch.
    """
    # GS v 0, m=0, 2 bytes per line, 3 dot lines -> 6 bytes of image data
    raster = b"\x1dv0\x00\x02\x00\x03\x00" + b"\xff" * 6
    with_image = render(b"before\n" + raster + b"after\n")
    without = render(b"before\nafter\n")
    assert with_image.height == without.height


def test_a_truncated_raster_header_still_renders():
    """The length bytes can be missing as well."""
    assert render(b"text\n\x1dv0\x00").width == WIDTH_DOTS


@pytest.mark.parametrize(
    ("dots", "expected_cm"),
    [(203, 2.54), (0, 0.0), (2030, 25.4)],
)
def test_paper_cm_converts_at_203_dpi(dots, expected_cm):
    """Height in dots divided by 203 dpi, times 2.54."""
    assert paper_cm(dots) == pytest.approx(expected_cm)


def test_the_height_bound_is_checked_before_the_image_exists():
    """A stream of line feeds is refused instead of allocated."""
    with pytest.raises(TooLongError) as refusal:
        render(b"x" + b"\n" * 1000, max_height_dots=600)
    assert refusal.value.height_dots > 600
    assert render(b"x\n", max_height_dots=600).height <= 600
