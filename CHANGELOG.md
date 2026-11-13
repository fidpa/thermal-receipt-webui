# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-11-13: Text can no longer carry printer commands

A review after the first release found two ways past the preview. Control
characters in the text reached the printer as ESC/POS commands, so the paper
could differ from the preview and escape the paper budget. And within the
20,000 character cap, a text of nothing but line feeds measured 7,511 cm, and
rendering it raised the peak memory of the process from 40 to 381 MB. Both are
closed.

### Security

- **Control characters in the text no longer reach the printer.** `normalize()` in `src/receipt/markup.py` removes them before the layout, and a tab becomes a space. Passed through, they acted on the device without showing in the preview or counting against the budget.
- **A single receipt is bounded at 600 cm of paper**, `MAX_LENGTH_CM` in `src/receipt/web.py`. The renderer checks it before it allocates the image; both endpoints answer `413 too_long` beyond it.
- **Request bodies over 512 KB are refused before they are parsed** (`MAX_CONTENT_LENGTH`), with `413 too_long`.

### Fixed

- **An unreadable job ledger stops printing instead of being overwritten.** A `jobs.json` that existed but could not be read was treated as empty: a retry under a known key printed a second sheet, and the next save replaced the file. The service now answers `503 ledger_unavailable` until it is readable. A ledger of the wrong shape, from a hand edit, is treated like unparsable JSON instead of failing each request that reads it with 500.
- **`--preview` on the command line printed whenever `RECEIPT_PRINTER` was set.** It falls back to the variable only without `--preview` now.
- **The command line sent without asking the printer first.** It now reads paper, cover and faults over DLE EOT, as the web interface does, and exits with 1 on a refusal.

### Changed

- **A tag publishes nothing past red checks.** `release.yml` runs lint and tests on the tagged commit first, and checks `__version__` in `src/receipt/__init__.py` against the tag as well as `pyproject.toml`. CI installs `requirements-dev.txt`, so it runs the same pinned pytest and ruff as a local run.
- **The print lock has a test that fails without it.** Four senders at once share an allowance that fits one receipt; exactly one gets through.

### Documentation

- A full cut costs about 3.4 cm, 5.4 cm at the large size, not 3.3 cm. The cell metrics live in two places, `FONT_CELLS` and `CELL_WIDTH`. `examples/nginx.conf` notes that `http2 on;` needs nginx 1.25.1. `CODE_OF_CONDUCT.md` names a private reporting channel instead of public issues.

### Upgrade notes

- A script that relied on `--preview` to print as well now needs `--host`.
- `too_long` also covers receipts over `MAX_LENGTH_CM` and bodies over 512 KB. `ledger_unavailable` (503) is a new error key.

## [0.1.0] - 2026-11-13: Receipt preview from the real byte stream, and a print API for programs

First public release: a web interface and command line tool for 80 mm ESC/POS
receipts, whose preview parses the bytes the printer receives.

### Added

- **Preview and print share one byte stream.** `build()` in `src/receipt/web.py` feeds both, and `POST /api/print` refuses with 409 when the previewed checksum no longer matches.
- **A Markdown subset, three type sizes (64, 48, 24 columns) and a history of the last 20 receipts.**
- **A contract for automated senders:** idempotency keys, announced jobs with a paper budget in centimetres, `confirmed_seq` to resume after a fault, and one response shape across the API.
- **The printer is asked about paper, cover and faults** over DLE EOT before anything is sent.

### Known limitations

- Font cell metrics are taken from the TM-T20IV datasheet, unverified on paper.
- No authentication; run it behind a reverse proxy.
- Raster images and QR codes are not drawn.

[0.1.1]: https://github.com/fidpa/thermal-receipt-webui/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/fidpa/thermal-receipt-webui/releases/tag/v0.1.0
