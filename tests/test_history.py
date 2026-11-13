# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the print history."""

from __future__ import annotations

from pathlib import Path

from receipt.history import History, default_state_dir, label_for


def test_add_and_read_back(tmp_path):
    """The newest entry comes first and carries its size."""
    history = History(tmp_path / "history.jsonl")
    history.add("first", "normal")
    history.add("second", "large")
    entries = history.entries()
    assert [e["text"] for e in entries] == ["second", "first"]
    assert entries[0]["size"] == "large"


def test_same_text_at_another_size_is_its_own_entry(tmp_path):
    """Otherwise a click on the history would print a different receipt."""
    history = History(tmp_path / "history.jsonl")
    history.add("note", "normal")
    history.add("note", "large")
    assert len(history.entries()) == 2


def test_same_text_at_the_same_size_moves_to_the_front(tmp_path):
    """A reprint reorders the list instead of duplicating the entry."""
    history = History(tmp_path / "history.jsonl")
    history.add("a", "normal")
    history.add("b", "normal")
    history.add("a", "normal")
    assert [e["text"] for e in history.entries()] == ["a", "b"]


def test_limit_drops_the_oldest(tmp_path):
    """The history is a convenience, not an archive."""
    history = History(tmp_path / "history.jsonl", limit=3)
    for index in range(5):
        history.add(f"entry {index}", "normal")
    assert [e["text"] for e in history.entries()] == ["entry 4", "entry 3", "entry 2"]


def test_missing_file_reads_as_empty(tmp_path):
    """A first run has no file yet and must not fail."""
    assert History(tmp_path / "nothing.jsonl").entries() == []


def test_a_broken_line_does_not_topple_the_history(tmp_path):
    """One unreadable line is skipped, the rest survives."""
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"text": "good", "label": "good", "size": "normal", "printed_at": "x"}\n'
        "{ this is not json\n",
        encoding="utf-8",
    )
    assert [e["text"] for e in History(path).entries()] == ["good"]


def test_a_line_holding_no_entry_is_skipped(tmp_path):
    """Valid JSON that is not an object must not topple the history either.

    add() reads the file before it writes, and it runs after the paper is out.
    A number on a line used to raise there, turning a successful print into an
    error the user could do nothing about.
    """
    path = tmp_path / "history.jsonl"
    path.write_text(
        '{"text": "good", "label": "good", "size": "normal", "printed_at": "x"}\n'
        "42\n"
        '["not an entry"]\n'
        "null\n",
        encoding="utf-8",
    )
    history = History(path)
    assert [e["text"] for e in history.entries()] == ["good"]
    history.add("new", "normal")
    assert [e["text"] for e in history.entries()] == ["new", "good"]


def test_entry_without_size_defaults_to_normal(tmp_path):
    """Entries written before the size switch existed stay readable."""
    path = tmp_path / "history.jsonl"
    path.write_text('{"text": "old", "label": "old", "printed_at": "x"}\n', encoding="utf-8")
    assert History(path).entries()[0]["size"] == "normal"


def test_label_strips_paired_markup():
    """The list shows the text, not the markers around it."""
    assert label_for("# **Important** _note_") == "Important note"


def test_label_keeps_snake_case():
    """An underscore inside a word is not markup, here either."""
    assert label_for("the RECEIPT_PRINTER variable") == "the RECEIPT_PRINTER variable"


def test_label_skips_a_leading_rule():
    """A receipt that opens with --- still gets a usable label."""
    assert label_for("---\nthe actual text") == "the actual text"


def test_label_of_an_empty_receipt():
    """Something has to go in the list even then."""
    assert label_for("   \n\n  ") == "(no text)"


def test_state_dir_follows_the_environment(tmp_path, monkeypatch):
    """The unit sets the variable; both routes to paper have to obey it.

    Without one answer here the interface and the command line would keep two
    histories of the same printer.
    """
    monkeypatch.setenv("RECEIPT_STATE_DIR", str(tmp_path))
    assert default_state_dir() == tmp_path


def test_state_dir_falls_back_to_the_user_directory(monkeypatch):
    """On a machine without the service there is still somewhere to write."""
    monkeypatch.delenv("RECEIPT_STATE_DIR", raising=False)
    monkeypatch.setattr("receipt.history.SYSTEM_STATE_DIR", Path("/nonexistent-state-dir"))
    assert default_state_dir().parts[-3:] == (".local", "state", "thermal-receipt-webui")
