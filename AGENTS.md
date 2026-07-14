# anydocs

When the anydocs MCP server is available, use `search_docs` with the product's
source and then `read_doc` before answering questions about that product's
documentation.

MCP server: BM25 search over other tools' documentation. Ingest in CI → SQLite
FTS5 → publish as a GitHub Release asset → the client downloads it and serves
five tools locally.

## The one number that matters

**A search must stay around 500 tokens.** The obvious way to build a docs-search
tool — return the matched sections — costs 10k+ for the same question, and that
gap is the entire reason this project exists. `search_docs` returns snippets and
`read_doc` is a separate call *because* of it. If a change makes search verbose,
it has broken the point of the project.

And the budget must be *spent on something*. Search returned eight rows and only
**4.5 distinct pages**: `SEARCH_SQL` deduped by `(source, path, anchor)` and let a
page take two slots, while `src_rank` cut the candidate set at eight *chunks* — so
a second section of a page already listed ate a slot and nothing refilled it. The
ninth-ranked page, which might be the answer, was never considered. A second
section of a page the caller already has adds nothing to the only decision search
supports: **which page to read.** Deduping by `(source, path)` buys eight pages
for eight slots — recall@8 0.859 → 0.897, precision unmoved — and the snippet
pays for it (300 → 200 chars, which costs the tail of an API field list and
nothing a caller routes on).

## Failure must be loud

The recurring bug class here is a failure that looks like an answer. Each of
these shipped, and each read to the caller as "the docs don't cover this":

- an unknown `source` filtered to nothing → "no matches"
- a typo in `ANYDOCS_SOURCES` → "index is empty"
- a Korean query → its words silently dropped, answering a question nobody asked
- OR matching → always finds *something*, off the query's least interesting words

So: **never return an empty or weak result without saying why.** `search_docs`
refuses unknown sources and warns on dropped non-English terms. Hold new code to
the same rule.

And a warning is not a fix. Asked `headless -p allowedTools disallowedTools
sandbox flag`, OR matching ranked the headless and CLI pages, `sandbox` reached
none of them, and `en/sandboxing` sat in the index unmentioned. `search_docs`
*said* "no result contains sandbox" — and the caller answered anyway, reporting
that Claude Code has no sandbox. It has one: Seatbelt on macOS, bubblewrap on
Linux.

**A caller will act on a name and ignore an adjective.** So `search_docs` now
re-runs the missed word on its own and names the pages it is really on
(`query.rescue_term`), and `read_doc` ends with the page's own cross-references
(`query.outlinks`). If you add a new way to fail, make it point somewhere.

### A dead end is the loudest failure of all, and read_doc had 22

`search_docs` was healthy the whole time. What was broken sat one call later.

`read_doc` outlined an over-long section against its H2/H3 children — and an H3's
children are H4s, so `en/hooks` § `PreToolUse` came back as a table cut mid-row
above **an empty menu**. Thirteen more sections have no subheadings at *any*
level, because they are one enormous table: the settings reference, the env-var
reference, every slash command. For those an outline is not a poor answer, it is
the wrong question — and `read_doc` **could not return them at all**. A caller who
did exactly the right thing hit a wall with nothing to do next.

So: **an over-long section is now outlined if it has children and paginated if it
does not** (`_shrink`), the outline reads the level *below the parent*, and
`part=` is the escape hatch that always exists. Every anchor an outline prints
round-trips through `read_doc`; there is a corpus-wide test for it, and the count
that matters is **22 dead ends → 0**.

Two more of the same shape, both fixed, both worth not reintroducing:

- **The anchor `search_docs` hands you must be one `read_doc` accepts.** It was
  not. `extract_section` slugged with the default style while the indexer used the
  source's own, so `opencode/rules#using-opencodejson` — dropped dot and all —
  "did not exist". `slug_style` is *not in the shipped index*, so the server does
  not try to know it: it offers all three spellings and keeps whichever the caller
  used (`SLUG_STYLES`). Across 638 pages that makes exactly 4 headings ambiguous,
  all of them the same title in two cases, and `read_doc` now says so out loud
  rather than returning the first of two in silence.
- **A `LIMIT` is a lie told quietly.** `list_pages` had no cap at all and cost
  7,600 tokens on claude-code — fifteen searches, from the tool that calls itself
  a cheap map. `grep_docs` showed 3 of a page's 52 matches and said nothing. The
  footer showed 8 of `en/settings`' 51 cross-references and said nothing. All three
  now name what they left out; `query.outlinks` counts the total exactly, the way
  `rescue_term` already did.

### Two things the rescue does not do, and cannot yet

**It fires on words that carry nothing.** Over 16 natural-language questions it
fired 5 times and 4 were noise: `stop` (105 pages), `happens` (44), `prompt`
(49) — it names three unrelated pages and tells the caller to read one before
answering. This is not free: the whole sandbox fix rests on the caller *believing*
the NOTE, and a note that cries wolf spends exactly that. Five ways to suppress
it were measured; all five are in the rejected table below. The false alarms are
the price of the fix, and nothing cheap in this index buys them down.

**It is blind to the query whose words all matched — and all missed.** Asked
`limit which model an org member can select`, Cursor's docs return org roles and
spend limits, no NOTE, because `limit`, `model`, `org`, `member` and `select` each
match *somewhere*. Ask in the docs' own words — `model access control allowed
models team admin` — and `enterprise/model-and-integration-management#model-access-control`
is the top hit. `rescue_term` only sees a word that reached nothing, so the
failure where every word reached the *wrong* thing is invisible. This one is open.

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
| A "Related pages" list on `search_docs`, from the link graph | ~200 tokens on *every* search — 40% of the budget — and on the query that actually failed it returned `common-workflows`, `agent-sdk/overview`. It did not surface `en/sandboxing`. Cut. The same graph pays for itself in `read_doc`, where the response is already kilobytes. |
| Re-ranking the link neighbourhood by BM25 | The premise of the miss was that the *query* was aimed wrong. Scoring the neighbours with the same scorer reproduces the same blindness — of course it does. |
| Ranking neighbours by IDF-lift co-citation | Over-corrects. `refs=1, inbound=2` obscurities (`deep-links`, `agent-sdk/streaming-output`) beat the right page. Popularity (`refs`) picks hubs, rarity picks noise, and nothing in between separated them. |
| Suppressing a rescue on a high-document-frequency word | **`sandbox` is in 36 of claude-code's 166 pages — 21.6%, commoner than the `stop` (16.5%) it would silence.** Any ceiling that kills the false alarms kills the case the rescue exists for. Check this one before you re-derive it; it is the obvious idea and it is backwards. |
| Suppressing a rescue by whether the word is in a page title, or a heading | Titles: `telemetry` and `worktree` hit none and both deserve the rescue. Headings: `stop` has 8, Cursor's `prompt` has 4. Neither separates a topic from a verb. |
| Suppressing a rescue by whether a page is *named* after the word (`path LIKE`) | The best of the five, and still 9/12: it silences `telemetry` and `ollama`, whose answers live on `en/env-vars` and `providers`. Losing 2 true rescues of 6 costs more than 4 false ones. |
| Warning when the top hit's BM25 margin over the runner-up is small | Looks decisive on the auto set — margin ≥ 0.2 → hit@1 0.993, < 0.1 → 0.55 — and it is an artifact of the ruler. See below. |
| Indexing `pages.description` as a fourth FTS field | It looks like a free win and it is a trap twice over. On the honest ruler it is worth **+1.8pp hit@1** — and on the auto set it reads as +10pp (0.876 → 0.976), because the auto set's *query is the description*. Indexing it **destroys the instrument**: 284 cases of eval, traded for 35 cases out of 1,956. |
| Raising `MIN_CHUNK` off the floor because it drops 369 sections | Almost all of what it drops is a JSX landing stub (`<CodexCliLanding />`). The real short sections it loses (`Vim editor mode`) are covered elsewhere in the corpus and still rank. Not a user-visible bug. Verify the claim before acting on it: a report that `PermissionBehavior` was unsearchable was simply wrong — it ranks first. |

## Retrieval changes need evidence

Do not tune by feel. `scripts/eval_search.py` scores three gold sets, and **each
one measures exactly one thing**:

- **hand** — 15 hand-written questions. Precision. Realistic, but **a one-case
  swing is noise**.
- **auto** — 284 cases (it moves with the corpus): each page's llms.txt
  description as the query, that page as the answer. Broad ranking movement. Fair
  because `description` lives only in `pages` and is not one of the indexed FTS
  columns, so it is a paraphrase, not the text being searched.
- **anchor** — 1,956 cases: the anchor text of the docs' own links
  (`[hook events list](/en/hooks)` → `en/hooks`). **Recall@8, and nothing else.**

The anchor set is the only text in the corpus that judges retrieval without
leaking — it is indexed neither in `chunks_fts` nor in `description` — and it is
in *referring* vocabulary, which is how a caller actually asks. `links.py` throws
it away, so the eval rebuilds it from the bodies.

**Score it on recall@8 alone.** Two things wreck its hit@1 and neither touches
recall: the anchor phrase sits verbatim in the *linking* page's body, which is
indexed, so the exact match is on the wrong page (34% of its "misses" return a
page that *cites* the answer); and the labels are noisy — `tracking costs and
usage` is graded wrong for answering `en/costs` because that one link happened to
point at `agent-sdk/cost-tracking`, and `en/costs` is the better page. Recall@8
survives both, and it is the question `search_docs` is really answering: **was the
right page among the eight rows at all?**

Gold paths are matched **exactly**. They were once matched by substring, so
`en/hooks` also "matched" `en/hooks-guide` — and a change was shipped, and had to
be reverted, on numbers that instrument produced. If a result looks too good,
suspect the ruler before the code.

**The auto set scores ranking. It cannot score confidence.** Its query is the
page's `description` — one long, distinctive sentence, which is nothing like the
three-to-six keywords a caller types. So any statistic that reads the *shape* of
the query or of the score curve will look superb on it and collapse in use. BM25
margin did exactly that: 0.993 vs 0.55 hit@1 on the auto set, and live,
`rate limit 429 retry backoff` lands the right page with a margin of 0.028 while
`output styles` lands it with 0.003. A short keyword query scores its top few
chunks nearly level *when it is right*. Measure confidence against the hand set
and real questions, never against the auto set.

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
  against its cache, throttled to once per hour (`ANYDOCS_REFRESH=1` skips it).
  Without this the daily sync reaches nobody: the release tag is fixed, so the
  cache path never changes.
- `content_hash` covers the schema, every source setting and page field,
  `cli.INDEXER_MODULES`, and every ingest module. **A new root module that shapes
  the index must be added to that list** — otherwise the docs are unchanged, the
  hash is unchanged, CI reports "documentation unchanged", and your fix is never
  published.
- The `links` table is the docs' own cross-references, resolved at index time
  (`links.py`). Sites write internal links four different ways and disagree about
  what a leading `/` means, so resolution offers every reading and keeps whichever
  lands on a page that exists. A site that links to itself under a second host
  needs `link_bases` in its YAML — without it Codex's 600 internal links all read
  as external and its graph vanished silently.
- Source names are injected into the tool schemas at startup (`enum` +
  description), so the model never has to guess `claude-code` from `claude`.
- `chunk.split_long(text, limit)` is shared by the indexer and by `read_doc`: the
  same table-header-repeating split serves 4 KB chunks to the ranker and 20 KB
  parts to a caller. The `limit` is defaulted so the index is byte-identical.

## Known, and not fixed

- **Two headings that slug alike get the same anchor, and the second one's is
  wrong on the live site.** Real sluggers append `-1` to a repeat; ours does not,
  so `cursor/rules#project-rules` from the *second* section lands on the first.
  4 pages in 638. `read_doc` says so out loud, but the emitted link is still
  wrong. Fixing it properly means numbering in `chunk.py` — which is an index
  change, hence a republish, hence a `SCHEMA_VERSION` question.
- **The silent weak result.** Asked `limit which model an org member can select`,
  Cursor's docs return org roles and spend limits, no NOTE — every word matched
  *something* — and the answer is not in the eight. Ask in the docs' own words
  (`model access control`) and it is first. `rescue_term` only sees a word that
  reached nothing, so the failure where every word reached the *wrong* thing is
  invisible. No candidate fix. The anchor recall@8 ruler is what will measure one.

## Verify before committing

```bash
uv run pytest -q
uv run anydocs-build                      # real ingest; ~1 min
uv run python scripts/eval_search.py      # no regression
uv run python scripts/verify_anchors.py   # anchors still resolve live
```
