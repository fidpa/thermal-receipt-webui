# Security Policy

## Supported Versions

Only the latest minor release receives security updates. Older versions are not patched.

| Version | Supported |
|---------|-----------|
| 0.1.x (latest) | ✅ |
| < 0.1 | ❌ |

## Reporting a Vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

Report vulnerabilities privately through GitHub Security Advisories: use the "Report a vulnerability" button on the repository's Security tab.

Include in your report:

- A clear description of the vulnerability and its impact.
- Step by step reproduction instructions, the smallest example that triggers the issue.
- Affected version and configuration.
- Your suggested fix, if any.
- Whether you would like to be credited in the advisory.

## Response Timeline

| Phase | Target |
|-------|--------|
| Acknowledgement | within 48 hours |
| Initial assessment | within 7 days |
| Fix availability, depending on severity | 14 to 60 days |
| Public disclosure | coordinated with the reporter |

## Scope

### In scope

- The Flask application in `src/receipt/web.py`, in particular the checksum gate on the print path
- The ESC/POS renderer in `src/receipt/preview.py`, which parses untrusted byte streams
- The markup parser in `src/receipt/markup.py`
- The history file handling in `src/receipt/history.py`
- The idempotency keys, job ledger and paper budget in `src/receipt/jobs.py`, and the audit log in `src/receipt/audit.py`
- The connection to the printer and its status decoding in `src/receipt/printer.py`

### Out of scope

- Vulnerabilities in upstream dependencies (python-escpos, Pillow, Flask, gunicorn). Report those to their maintainers.
- Deployment misconfiguration (nginx, systemd, firewalls).
- The absence of authentication. This is documented behaviour, see the Known Limitations section of the README.
- Anything that requires access to the machine the service runs on.

## Notes for Operators

This application has **no authentication of its own**. Anyone who can reach the port can print. Deploy it accordingly:

1. Bind gunicorn to `127.0.0.1` and let a reverse proxy be the only listener on a routable address.
2. Add authentication at the proxy if the network it sits on is not trusted.
3. The printer address is configuration, not user input. The application never takes a target host from a request.
4. The state directory holds the texts of recent prints (`history.jsonl`) and, in `audit.jsonl` and `jobs.json`, the source addresses, keys and job identifiers of every attempt. Treat it like any other document store.
5. The paper budget reads the client address from the rightmost element of `X-Forwarded-For`. Only a proxy that sets or appends that header keeps a caller from picking its own contingent.

## Disclosure Policy

We follow [coordinated disclosure](https://en.wikipedia.org/wiki/Coordinated_vulnerability_disclosure). Once a fix is available we publish a GitHub Security Advisory, credit the reporter with permission, and document the fix in `CHANGELOG.md` under a `### Security` heading.
