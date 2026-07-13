# anydocs

An MCP server that gives coding agents fast BM25 search across many third-party
documentation sites — Claude Code, OpenAI Codex, Cursor, xAI, and whatever else
you add.

Docs are ingested in CI, indexed into SQLite FTS5, and published as a release
artifact. The server downloads that artifact and serves four tools:

| tool | what it does |
| --- | --- |
| `search_docs` | BM25-ranked hits as **short snippets** — never full sections |
| `read_doc` | one page, or one heading section of it |
| `grep_docs` | ripgrep regex, for exact symbols BM25 tokenizers mangle |
| `list_sources` / `list_pages` | cheap orientation |

## Adding a source

Drop a YAML file in `sources/`. Three ingest strategies cover the sites seen so far:

- `llms-txt` — llms.txt is an *index* of pages, each with a `.md` twin (Claude Code, Codex)
- `sitemap` — no llms.txt; take the page list from sitemap.xml (Cursor)
- `llms-full` — llms.txt *is* the corpus, split by a delimiter (xAI)

## Install

```jsonc
{ "mcpServers": { "anydocs": {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/kiyeonjeon21/anydocs", "anydocs"] }}}
```
