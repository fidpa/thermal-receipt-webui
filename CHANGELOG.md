# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/fidpa/thermal-receipt-webui/releases/tag/v0.1.0
