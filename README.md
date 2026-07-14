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
obvious way to build this — costs 10k+ for the same question. That is what makes
it cheap enough to check *every time* instead of guessing.

Everything runs locally: no API key, no network at query time, no service to keep
alive. The whole index is ~7 MB.

## Does it help? Measured.

Ten questions about config surface that has changed recently — Codex's hooks,
Cursor's model access control, Claude Code's permission modes, opencode's agent
directory — with ground truth read off the current docs. Each run is a real Claude
Code, **with WebFetch and WebSearch enabled in every arm**: the control is not a
model with its hands tied, it is what you already have. Answers graded blind
against the key, three independent passes.

| | wrong answers | accuracy | wall | cost |
| --- | --- | --- | --- | --- |
| Claude Code alone (n=80) | **26%** | 0.63 | 51s | $0.311 |
| \+ anydocs (n=80) | 20% | 0.70 | 44s | $0.319 |
| **\+ anydocs + the `AGENTS.md` line below (n=60)** | **6%** | **0.78** | **27s** | **$0.275** |

**Four times fewer wrong answers — and it is faster and cheaper at the same time.**

The middle row is the point. **Mounting the server is not enough.** With anydocs
available but nothing telling the agent to use it, it sometimes just answers from
memory — and when it does, it is wrong: asked for Claude Code's permission modes it
replied in a single turn, named four, and missed `auto` and `dontAsk`. One line of
instruction takes the skip rate to zero, and it is the difference between 20% wrong
and 6%.

So the line is not optional. It is in both install paths below.

## Install

### Codex

Codex reads MCP servers from `~/.codex/config.toml` or, for a trusted project,
`.codex/config.toml`. Add it globally with the CLI:

```bash
codex mcp add anydocs -- \
  uvx --from git+https://github.com/kiyeonjeon21/anydocs anydocs
codex mcp list
```

Or use project configuration. The longer startup timeout covers the first cold
`uvx` install and index download; `required` makes a broken server fail loudly.

```toml
[mcp_servers.anydocs]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/kiyeonjeon21/anydocs",
  "anydocs",
]
startup_timeout_sec = 120
required = true

[mcp_servers.anydocs.env]
ANYDOCS_SOURCES = "codex"
```

Restart Codex after changing configuration. **Then do step 2.**

### Clients using `.mcp.json`

For clients that support `.mcp.json`, use the following. Nothing needs to be
installed first: `uvx` fetches the server, and the server fetches the index on
first run.

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

**Then do step 2.**

### Step 2 — tell the agent to use it

Put this in the project's `AGENTS.md` (or `CLAUDE.md`):

```md
When anydocs MCP is available, use search_docs with the product's source and
then read_doc before answering questions about that product's documentation.
```

**Do not skip this.** A mounted MCP server the agent does not call is worth
nothing, and an agent that feels sure will answer from memory instead — which is
exactly when it is wrong. Measured over 140 runs, this line takes the
answer-from-memory rate to **zero**, cuts wrong answers from **20% to 6%**, and
makes the agent *faster* (27s against 44s), because one search beats three
guesses at a docs URL.

## Scoping a project to the docs it uses

`ANYDOCS_SOURCES` limits the server to the sources you name. The rest disappear —
from `list_sources`, from the `source` enum the model sees, and from every tool,
including direct `read_doc` calls.

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

In Codex config, the equivalent is:

```toml
[mcp_servers.anydocs.env]
ANYDOCS_SOURCES = "claude-code,codex"
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

Retrieval changes need evidence, and every ruler here measures exactly one thing.
`scripts/eval_search.py` runs three: 15 hand-written questions (precision), 284
auto-derived ones (each page's llms.txt description as the query — broad ranking
movement), and 1,956 built from the anchor text of the docs' own internal links
(recall@8 only, and the only text in the corpus that leaks into neither the index
nor the descriptions). `eval_rescue.py` and `eval_served.py` cost model calls and
stay out of CI.

Several plausible improvements died on these numbers, and a few shipped and had to
be reverted because the ruler was wrong rather than the code. `AGENTS.md` keeps the
list, with the numbers, so nobody spends a day re-deriving them.

### About the benchmark at the top

Ten questions, chosen by me, all on config surface — the ground a docs tool is
supposed to own. It says nothing about a question with no documented answer, and a
model that already knows React does not need this. Answers were graded by an LLM
against a hand-verified key; a single grading pass moves the accuracy figure by up
to 10 points, which is why the table reports the mean of three and the wrong-answer
ranges (25–29% / 18–21% / 5–8%) do not overlap where it matters.

## License

MIT
