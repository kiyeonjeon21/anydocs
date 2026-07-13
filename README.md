# anydocs

An MCP server that gives coding agents fast BM25 search across many third-party
documentation sites — Claude Code, OpenAI Codex, Cursor, xAI, and whatever else
you add.

Docs are ingested in CI, indexed into SQLite FTS5, and published as a release
artifact. The server downloads that artifact and serves five tools:

| tool | what it does |
| --- | --- |
| `search_docs` | BM25-ranked hits as **short snippets** — never full sections |
| `read_doc` | one page, or one heading section of it |
| `grep_docs` | regex over the raw markdown, for exact symbols BM25 tokenizers split |
| `list_sources` | which doc sets are indexed |
| `list_pages` | a source's pages and descriptions |

A search costs about 500 tokens. Dumping whole matched sections instead — the
usual shortcut — costs 10k+, which is the whole reason this exists.

## Install

Add this to `.mcp.json`. Nothing to install first: `uvx` fetches the server, and
the server fetches the index (~7 MB) on first run.

```json
{
  "mcpServers": {
    "anydocs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/kiyeonjeon21/anydocs",
        "anydocs"
      ]
    }
  }
}
```

## Scoping a project to the docs it uses

Set `ANYDOCS_SOURCES` to the sources this repo actually cares about. Everything
else disappears — from `list_sources`, from the `source` enum the model sees, and
from every search.

Worth doing. These doc sets describe the same ideas in different words, so on a
Claude Code repo an unfiltered search for `hook events` hands 3 of its 5 slots to
Cursor and xAI.

```json
{
  "mcpServers": {
    "anydocs": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/kiyeonjeon21/anydocs",
        "anydocs"
      ],
      "env": {
        "ANYDOCS_SOURCES": "claude-code,codex"
      }
    }
  }
}
```

Available: `claude-code`, `codex`, `cursor`, `xai`. A name that is not in the
index stops the server and prints the valid ones, rather than quietly serving an
empty index.

## Adding a source

Drop a YAML file in `sources/`. Three ingest strategies cover every site seen so
far, because sites do not agree on how to publish docs:

| strategy | when | example |
| --- | --- | --- |
| `llms-txt` | llms.txt is an *index* of pages, each with a `.md` twin | Claude Code, Codex |
| `sitemap` | no llms.txt — take the page list from sitemap.xml | Cursor |
| `llms-full` | llms.txt *is* the corpus, split by a delimiter | xAI |

```yaml
id: cursor
title: Cursor
tags: [coding-agent]
strategy: sitemap
entry: https://cursor.com/docs/sitemap.xml
base_url: https://cursor.com/docs/
page_suffix: .md
include: ["https://cursor.com/docs/*"]   # the sitemap carries 13 locales
```

CI re-ingests daily and publishes a new index only when the docs actually
changed. Anchors are checked against the live HTML on every sync — a wrong
heading slug still ranks fine and only breaks the link, so nothing else would
catch it.

## Development

```bash
uv run anydocs-build                      # ingest + index into build/
uv run pytest -q                          # tests
uv run python scripts/eval_search.py      # retrieval quality against a gold set
uv run python scripts/verify_anchors.py   # anchors resolve on the live sites
```

A local `build/` directory takes precedence over the published index, so
`anydocs-build` then `anydocs` serves what you just built.
