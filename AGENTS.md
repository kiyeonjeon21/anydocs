# anydocs

MCP server: BM25 search over other tools' documentation. Ingest in CI → SQLite
FTS5 → publish as a GitHub Release asset → the client downloads it and serves
five tools locally.

## The one number that matters

**A search must stay around 500 tokens.** The obvious way to build a docs-search
tool — return the matched sections — costs 10k+ for the same question, and that
gap is the entire reason this project exists. `search_docs` returns snippets and
`read_doc` is a separate call *because* of it. If a change makes search verbose,
it has broken the point of the project.

## Failure must be loud

The recurring bug class here is a failure that looks like an answer. Each of
these shipped, and each read to the caller as "the docs don't cover this":

- an unknown `source` filtered to nothing → "no matches"
- a typo in `ANYDOCS_SOURCES` → "index is empty"
- a Korean query → its words silently dropped, answering a question nobody asked
- OR matching → always finds *something*, off the query's least interesting words

So: **never return an empty or weak result without saying why.** `search_docs`
now names the words that never reached the results, refuses unknown sources, and
warns on dropped non-English terms. Hold new code to the same rule.

## Do not retry these — they were measured and rejected

Re-deriving them costs a day each.

| Idea | What the numbers said |
| --- | --- |
| Dense / hybrid embeddings | Dense alone is *worse* than BM25 (hit@1 0.775 vs 0.804). Hybrid moves recall@8 0.946 → 0.964: five questions out of 276, for a 130 MB client model or a server to run. |
| AND-first query matching | `list`, `file`, `order` sit in 10-18% of the corpus. They select nothing but can still *veto* the right answer. OR beat AND on both hit@1 and hit@3. |
| Document-frequency stopwords | Every threshold lost to plain OR. |
| BM25 field-weight tuning | 276-question hit@1 moves only 0.775–0.804 across the whole grid. Not a lever. |
| Demoting link-heavy "pointer" chunks | The *correct* chunk had 39% link density; the pointer had 35%. The signal does not separate them. |
| Smaller `MAX_CHUNK` | Fixes "settings file precedence order" and breaks "hook events list" and "config.toml model provider". A trade, not a win. |
| Stripping link URLs from the *indexed* body | Ranking flat, slightly worse at 4000. It is stripped at *display* time only. |

## Retrieval changes need evidence

Do not tune by feel. `scripts/eval_search.py` scores two gold sets:

- 15 hand-written questions — realistic, but **a one-case swing is noise**
- 276 auto-derived — each page's llms.txt description as the query, that page as
  the answer. Fair because `description` lives only in `pages` and is not one of
  the indexed FTS columns, so it is a paraphrase, not the text being searched.

Gold paths are matched **exactly**. They were once matched by substring, so
`en/hooks` also "matched" `en/hooks-guide` — and a change was shipped, and had to
be reverted, on numbers that instrument produced. If a result looks too good,
suspect the ruler before the code.

`scripts/sweep_chunk.py` re-chunks from `pages.body`, so sweeping chunk size or
weights needs **no refetch** — a full sweep is seconds.

## Adding a source

One YAML in `sources/`. Three strategies cover every site so far: `llms-txt`,
`sitemap`, `llms-full`. Two things fail silently:

- **Locales.** `fnmatch` has **no brace expansion** — a `{ja,ko}` glob matches
  nothing and lets every translation in. opencode went to 564 pages instead of
  36 with no error. List one glob per locale, and set `expect_pages`, which is
  checked in *both* directions.
- **`slug_style`.** Sites disagree, and a wrong anchor still ranks fine — it just
  lands in the wrong place, which nothing else would catch. `collapse` (Mintlify:
  `CLAUDE.md` → `claude-md`, and the **slash survives**), `github` (Astro
  Starlight: `Avante.nvim` → `avantenvim`, dot **dropped**), `verbatim` (no
  collapsing, trailing dash kept). Always run `scripts/verify_anchors.py`; it
  diffs against the live HTML. Cursor is client-rendered and reports as
  unverifiable rather than passing.

Also: a 200 response is not a page. `docs.cursor.com` serves its SPA shell with
HTTP 200 for every unknown path, so bodies are content-checked, not trusted.

## Architecture notes

- `pages.body` is the single source of truth. `read_doc` and `grep_docs` both
  read it; there is no markdown tree on disk. grep is Python `re` over the bodies
  (~25–90 ms for the whole corpus) — ripgrep was dropped because it is not
  reliably installed.
- The client compares the published `content_hash` (manifest.json, ~400 bytes)
  against its cache, throttled to once per 6 h. Without this the daily sync
  reaches nobody: the release tag is fixed, so the cache path never changes.
- Source names are injected into the tool schemas at startup (`enum` +
  description), so the model never has to guess `claude-code` from `claude`.

## Verify before committing

```bash
uv run pytest -q
uv run anydocs-build                      # real ingest; ~1 min
uv run python scripts/eval_search.py      # no regression
uv run python scripts/verify_anchors.py   # anchors still resolve live
```
