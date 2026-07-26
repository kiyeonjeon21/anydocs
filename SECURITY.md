# Security Policy

## Supported versions

anydocs is pre-1.0 (currently `0.1.0`).
Only the latest release on `main` is supported.
There is no LTS branch.

## Scope

anydocs is a local MCP server: it runs on the caller's machine, makes no outbound calls at query time beyond the one hourly index-freshness check, and requires no API key or credential.
The most relevant attack surfaces are:

- The index build/ingest path (`anydocs-build`, the `sources/*.yaml` fetchers, `scripts/`)
- The published release index (SQLite FTS5 database) and its download/verification path in `src/anydocs/artifact.py`
- The MCP server's tool handlers (`search_docs`, `read_doc`, `grep_docs`, `list_sources`, `list_pages`)

## Reporting a vulnerability

Please do not open a public issue for a security report.

Use [GitHub's private vulnerability reporting](https://github.com/kiyeonjeon21/anydocs/security/advisories/new) for this repository, or email kiyeon.jeon.21@gmail.com.
Include what you found, the impact you'd expect, and reproduction steps if you have them.

You should get a response within a few days.
This is a solo-maintained project, so please allow for that when setting expectations on turnaround.
