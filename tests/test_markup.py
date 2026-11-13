# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the Markdown subset."""

from __future__ import annotations

from conftest import printed_lines

from receipt.layout import Emphasis
from receipt.markup import from_text, inline, longest_word, normalize


def test_bold_and_underline_are_separate():
    """** and _ mark up different attributes, in the middle of a sentence."""
    segments = inline("plain **bold** and _underlined_ text")
    assert ("bold", Emphasis(bold=True)) in segments
    assert ("underlined", Emphasis(underline=True)) in segments


def test_emphasis_nests_in_both_orders():
    """**_x_** and _**x**_ both come out bold and underlined."""
    both = Emphasis(bold=True, underline=True)
    assert inline("**_x_**") == [("x", both)]
    assert inline("_**x**_") == [("x", both)]


def test_snake_case_survives():
    """An underscore inside a word is not an emphasis marker, as in Markdown."""
    text = "the variable snake_case_name stays intact"
    assert inline(text) == [(text, Emphasis())]


def test_unpaired_markers_are_printed():
    """Half finished input is text, not an error."""
    text = "unpaired ** and _ stay put"
    assert inline(text) == [(text, Emphasis())]


def test_heading_levels_differ():
    """Level 1 is taller than level 2, level 3 is not centered."""
    tall = from_text("# One").data
    plain = from_text("## Two").data
    flush = from_text("### Three").data
    assert tall != plain != flush
    # Only level 1 stretches the height via GS !
    assert b"\x1d!\x01" in tall
    assert b"\x1d!\x01" not in plain


def test_deeper_headings_clamp_to_level_three():
    """'#####' must never reach the paper verbatim."""
    lines = printed_lines(from_text("##### Deep").data)
    assert any(line.strip() == "Deep" for line in lines)
    assert not any("#" in line for line in lines)


def test_blank_line_stays_a_blank_line():
    """One blank line in the input is one blank line on the paper."""
    lines = printed_lines(from_text("first\n\nsecond").data)
    assert lines[0] == "first"
    assert lines[1] == ""
    assert lines[2] == "second"


def test_consecutive_lines_join_into_one_paragraph():
    """Input line breaks are flattened so a textarea does not shred the text."""
    lines = printed_lines(from_text("one\ntwo\nthree").data)
    assert lines[0] == "one two three"


def test_crlf_is_normalised():
    """Browsers send CRLF from a textarea; the paragraph split must still work."""
    assert printed_lines(from_text("a\r\n\r\nb").data)[:3] == ["a", "", "b"]


def test_rule_spans_the_width():
    """--- becomes a rule as wide as the receipt."""
    assert printed_lines(from_text("---").data)[0] == "-" * 48


def test_longest_word_ignores_markers():
    """The warning counts what gets printed, not the markup around it."""
    assert longest_word("**abcdef** _gh_") == 6


def test_longest_word_of_empty_text():
    """No text, no longest word."""
    assert longest_word("   ") == 0


def test_typographic_characters_become_their_ascii_image():
    """CP858 knows none of these; without a mapping they print as '?'.

    Text pasted from an editor, a browser or a text file is full of them, and a
    receipt peppered with question marks is not what anyone typed.
    """
    assert normalize("a \u2014 b") == "a - b"
    assert normalize("\u201egerman\u201c and \u2018english\u2019") == "\"german\" and 'english'"
    assert normalize("and so on \u2026") == "and so on ..."
    assert normalize("\u2022 first") == "- first"
    assert normalize("DSL \u2192 LTE, \u2264 5 s") == "DSL -> LTE, <= 5 s"


def test_a_byte_order_mark_disappears():
    """A text file dropped on the editor may well start with one."""
    assert normalize("\ufeffcontent") == "content"


def test_characters_cp858_has_are_left_alone():
    """The code page covers more than ASCII, and that part must survive."""
    unchanged = "\u00abquote\u00bb 20 \u00b0C \u00b1 2, 100 \u20ac, Gr\u00fc\u00dfe"
    assert normalize(unchanged) == unchanged


def test_everything_normalized_survives_the_code_page():
    """The mapping is worthless if its own output cannot be encoded."""
    from receipt.markup import _REPLACEMENTS

    for source, replacement in _REPLACEMENTS.items():
        replacement.encode("cp858")  # raises if the mapping made things worse
        assert source.encode("cp858", "replace") == b"?" or source == "\u00a0"


def test_line_endings_are_unified():
    """A browser sends CRLF, and a paragraph break has to be found in it."""
    assert normalize("a\r\nb\rc") == "a\nb\nc"


def test_the_warning_counts_what_gets_printed():
    """An ellipsis is one character in the input and three on the paper."""
    assert longest_word("\u2026" * 30) == 90


def test_control_characters_never_reach_the_printer():
    """ESC, GS, DLE and the rest are the device's command language, not text."""
    assert normalize("a\x1bJ\xfeb\x1dV\x00c\x10\x14\x01d\x7f\x85e") == "aJ\xfebVcde"


def test_a_tab_becomes_a_space_and_the_line_feed_stays():
    """The device jumps to a tab stop the preview cannot know."""
    assert normalize("a\tb\nc") == "a b\nc"
