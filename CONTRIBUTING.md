# Contributing

Issues and pull requests are welcome.

## Before you open a pull request

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
PYTHONPATH=src .venv/bin/python -m pytest tests/
```

CI runs exactly these three commands. `ruff format --check` is a separate gate from `ruff check`; passing one does not mean passing the other.

## The rule that matters most

**The preview renders the byte stream, not a reconstruction of it.**

`src/receipt/preview.py` interprets the same bytes that `src/receipt/printer.py` sends. Any change that lets the two drift apart is a regression, even when the resulting picture looks better. That includes:

- Drawing something in the preview that no ESC/POS sequence produced.
- Letting a layout primitive write more columns into a line than the receipt is wide. The printer would wrap it, the renderer clips it, and the two disagree from that point on. `tests/test_layout.py::test_no_line_exceeds_the_column_count` pins this down at all three sizes.
- Changing what `web.build()` returns for one caller but not the other. Preview and print go through that one function on purpose.

Additions to the renderer for sequences it does not understand yet, QR codes and raster images among them, are welcome. The principle stays.

## Device assumptions

The cell metrics come from the TM-T20IV datasheet and have not been verified against paper. They live in two places that have to agree: `FONT_CELLS` in `src/receipt/preview.py` draws the preview, `CELL_WIDTH` in `src/receipt/layout.py` sets the column count. If you own the device and measure something different, that is a valuable bug report: the whole preview rests on these numbers.

The renderer draws with DejaVu Sans Mono from the Debian path `/usr/share/fonts/truetype/dejavu/` (`fonts-dejavu-core`); the tests need it too. Elsewhere, adjust `FONT_REGULAR` and `FONT_BOLD` in `src/receipt/preview.py`.

## Style

- Python 3.10 or newer, type hints, Google style docstrings.
- English throughout: code, comments, docstrings, log messages, documentation.
- Comments explain why, not what.
- New behaviour comes with a test. The tests use no mocks and no running services; every file a test needs is created in its own `tmp_path`.
