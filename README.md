# anydocs

An MCP server that gives coding agents fast search over other tools'
documentation — Claude Code, OpenAI Codex, Cursor, opencode, xAI, and whatever
else you add.

Docs are ingested in CI, indexed into SQLite FTS5, and published as a release
artifact. The server downloads it and serves five tools:

| tool | what it does |
| --- | --- |
| `search_docs` | BM25-ranked hits as **short snippets** — never whole sections |
| `read_doc` | one page, or one heading section of it |
| `grep_docs` | regex over the raw markdown, for exact symbols BM25 splits |
| `list_sources` | which doc sets are indexed |
| `list_pages` | a source's pages and descriptions |

**A search costs ~500 tokens.** Returning whole matched sections instead — the
obvious way to build this — costs 10k+ for the same question. That gap is the
reason anydocs exists.

Everything runs locally: no API key, no network at query time, no service to keep
alive. The whole index is ~7 MB.

## Install

Add this to `.mcp.json`. Nothing to install first — `uvx` fetches the server, and
the server fetches the index on first run.

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

`ANYDOCS_SOURCES` limits the server to the sources you name. The rest disappear —
from `list_sources`, from the `source` enum the model sees, and from every search.

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

Available: `claude-code`, `codex`, `cursor`, `opencode`, `xai`. A name that is not
in the index stops the server and prints the valid ones, rather than quietly
serving an empty index.

## What it does not do

Matching is lexical, and the tools say so rather than bluffing:

- **English only.** The docs are English and matching is by word, so a Korean or
  Japanese query reaches nothing. `search_docs` names the words it had to ignore
  instead of quietly answering a question you did not ask.
- **No fuzzy matching.** A typo finds nothing. It is reported as a typo.
- **OR matching always finds *something*.** Ask Claude Code's docs about
  `cursorrules` and the hits will be pages that merely contain `tab`. The reply
  says which of your words never reached the results, so a weak match cannot pass
  as an answer.

Embeddings were measured and left out: dense retrieval alone scored *worse* than
BM25 on these corpora (hit@1 0.775 vs 0.804), and a hybrid moved recall@8 from
0.946 to 0.964 — five questions out of 276 — in exchange for a 130 MB model on
every client or a server to keep running. Not worth it yet.

## Adding a source

Drop a YAML file in `sources/`. Sites do not agree on how to publish docs, so
there are three ingest strategies:

| strategy | when | example |
| --- | --- | --- |
| `llms-txt` | llms.txt is an *index* of pages, each with a `.md` twin | Claude Code, Codex |
| `sitemap` | no llms.txt — take the page list from sitemap.xml | Cursor, opencode |
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
expect_pages: 165                        # guards against the site moving
```

Two things to get right, both of which fail silently:

- **Locales.** Every sitemap carries them, and they can multiply a source by 17.
  `expect_pages` is checked in both directions, so a filter that stops matching
  is a build failure rather than a quietly bloated index.
- **`slug_style`.** Sites slug their heading anchors differently, and a wrong
  slug still ranks fine — it just lands in the wrong place, which nothing else
  would catch. `collapse` for Mintlify (`CLAUDE.md` → `claude-md`), `github` for
  Astro Starlight (`Avante.nvim` → `avantenvim`), `verbatim` for the rest. CI
  checks every anchor against the live HTML on each sync.

CI re-ingests daily and publishes a new index only when the docs actually
changed.

## Development

```bash
uv run anydocs-build                      # ingest + index into build/
uv run pytest -q
uv run python scripts/eval_search.py      # retrieval quality against a gold set
uv run python scripts/verify_anchors.py   # anchors resolve on the live sites
uv run python scripts/sweep_chunk.py      # re-chunk from pages.body, no refetch
```

A local `build/` directory takes precedence over the published index, so
`anydocs-build` then `anydocs` serves what you just built.

Retrieval changes need evidence. `scripts/eval_search.py` scores against a
hand-written gold set plus 276 auto-derived questions (each page's llms.txt
description, which is a paraphrase and is not among the indexed columns). A
one-case swing on the hand set is noise; several plausible improvements died on
these numbers.

## License

MIT
