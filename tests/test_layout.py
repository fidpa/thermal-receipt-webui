# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the layout primitives and the line wrapping.

The central claim of this file: no primitive ever writes more columns into a
line than the receipt is wide. An over-long line is where the preview and the
printer would start to disagree, because the printer wraps it and the renderer
clips it.
"""

from __future__ import annotations

from conftest import printed_lines

from receipt.layout import PLAIN, Emphasis, Receipt, Size, _wrap
from receipt.markup import from_text

LOREM = (
    "Milk, bread, coffee, two lemons, oil, onions, carrots, apples and a jar "
    "of pickles for Sunday, plus something to write the list on."
)


def test_sizes_have_the_documented_column_counts(size):
    """small 64, normal 48, large 24 columns."""
    assert size.cols in (64, 48, 24)
    assert size.cols == 576 // (12 if size.font == 0 else 9) // size.scale


def test_no_line_exceeds_the_column_count(size):
    """Holds at every size, for a paragraph that has to be wrapped."""
    lines = printed_lines(from_text(LOREM, size=size).data)
    assert lines, "the receipt produced no lines at all"
    assert max(len(line) for line in lines) <= size.cols


def test_long_word_is_broken_hard(size):
    """A word wider than the receipt is cut, not left to overflow."""
    word = "a" * (size.cols * 2 + 5)
    lines = printed_lines(from_text(word, size=size).data)
    assert max(len(line) for line in lines) <= size.cols
    assert "".join(lines).count("a") == len(word)


def test_heading_is_truncated_not_wrapped(size):
    """Headings stay on one line, so the column count never changes."""
    lines = printed_lines(from_text("# " + "x" * 200, size=size).data)
    assert max(len(line) for line in lines) <= size.cols


def test_rule_matches_the_size(size):
    """The rule is exactly as wide as the receipt, at every size."""
    assert printed_lines(from_text("---", size=size).data)[0] == "-" * size.cols


def test_separator_inherits_emphasis_only_when_both_sides_agree():
    """Otherwise an underlined gap sits between two words nobody marked up."""
    marked = Emphasis(underline=True)
    line = _wrap([("one", marked), (" ", PLAIN), ("two", PLAIN)], 48)[0]
    spaces = [style for text, style in line if text == " "]
    assert spaces == [PLAIN]


def test_separator_keeps_emphasis_inside_a_marked_phrase():
    """Two underlined words are joined by an underlined space."""
    marked = Emphasis(underline=True)
    line = _wrap([("one two", marked)], 48)[0]
    spaces = [style for text, style in line if text == " "]
    assert spaces == [marked]


def test_columns_never_overflow():
    """Left plus right longer than the width is truncated, not wrapped."""
    receipt = Receipt()
    receipt.columns("x" * 40, "y" * 40)
    assert max(len(line) for line in printed_lines(receipt.data)) <= receipt.cols


def test_an_indent_wider_than_the_receipt_is_shortened(size):
    """The indent must not push a line past the paper edge.

    The wrapping subtracts the indent from the usable width, but the indent was
    printed at full length regardless, so a wide one produced lines the printer
    would wrap and the renderer would clip - the two drifting apart.
    """
    receipt = Receipt(size)
    receipt.para(LOREM, indent=" " * (size.cols + 12))
    assert max(len(line) for line in printed_lines(receipt.data)) <= size.cols


def test_a_normal_indent_still_indents(size):
    """Shortening the outliers must not flatten the ordinary case."""
    receipt = Receipt(size)
    receipt.para("source: somewhere", indent="  ")
    assert printed_lines(receipt.data)[0].startswith("  source")


def test_empty_paragraph_yields_one_empty_line():
    """_wrap always returns at least one line, so callers need no special case."""
    assert _wrap([], 48) == [[]]


def test_scale_is_clamped_to_the_device_maximum():
    """GS ! knows 1x to 8x; big() at large size must not run past that."""
    receipt = Receipt(Size(font=0, scale=8))
    receipt.big("X")
    assert b"\x1d!\x77" in receipt.data  # 8x8, the highest legal value
