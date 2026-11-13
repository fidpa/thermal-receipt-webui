# Copyright (c) 2026 Marc Allgeier (fidpa)
# SPDX-License-Identifier: MIT
# https://github.com/fidpa/thermal-receipt-webui
"""Tests for the append-only record."""

from __future__ import annotations

import json

from receipt import audit


def test_a_record_is_one_line_of_json(tmp_path):
    """JSON Lines, so the file can be read with a pipe."""
    book = audit.Audit(tmp_path / "audit.jsonl")
    book.emit(
        "print",
        result="ok",
        src_ip="198.51.100.9",
        method="POST",
        path="/api/print",
        payload={"sha": "abcd1234"},
    )
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "print"
    assert record["result"] == "ok"
    assert record["src_ip"] == "198.51.100.9"
    assert record["payload"] == {"sha": "abcd1234"}
    assert record["iso"].startswith("2")


def test_records_only_ever_get_appended(tmp_path):
    """Nothing is overwritten; that is the whole point of the file."""
    book = audit.Audit(tmp_path / "audit.jsonl")
    for number in range(5):
        book.emit("print", result="ok", src_ip="198.51.100.9", payload={"seq": number})
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["payload"]["seq"] for line in lines] == [0, 1, 2, 3, 4]


def test_the_file_is_rolled_before_it_grows_without_bound(tmp_path, monkeypatch):
    """A service with no upper bound eventually fills its state directory."""
    monkeypatch.setattr(audit, "MAX_BYTES", 200)
    book = audit.Audit(tmp_path / "audit.jsonl")
    for number in range(20):
        book.emit("print", result="ok", src_ip="198.51.100.9", payload={"seq": number})
    assert (tmp_path / "audit.jsonl.1").exists()
    assert (tmp_path / "audit.jsonl").stat().st_size < 400
    # Two files and no more: the older roll is replaced, not kept.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audit.jsonl", "audit.jsonl.1"]


def test_an_unwritable_path_does_not_raise(tmp_path):
    """The paper is already out; a logging problem must not surface as a 500."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    book = audit.Audit(blocked / "audit.jsonl")
    book.emit("print", result="ok", src_ip="198.51.100.9")
