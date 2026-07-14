# anydocs

When the anydocs MCP server is available, use `search_docs` with the product's
source and then `read_doc` before answering questions about that product's
documentation.

MCP server: BM25 search over other tools' documentation. Ingest in CI → SQLite
FTS5 → publish as a GitHub Release asset → the client downloads it and serves
five tools locally.

**The rest of this file is for agents working *on* anydocs, and none of it
ships.** What reaches someone *using* the server is `SERVER_INSTRUCTIONS` and the
five tool docstrings in `server.py` — about 1,000 tokens, and the only guidance a
caller will ever see. So a lesson that should change how a **caller** behaves has
to be written *there*. Writing it down here reaches nobody but us.

Keep this file to rules — the things a future session must not get wrong. The
story of how each rule was found is in `git log`, where the commit messages run to
forty lines and cost nothing to carry.

## The one number that matters

**A search must stay around 500 tokens.** The obvious way to build a docs-search
tool — return the matched sections — costs 10k+ for the same question, and that
gap is the entire reason this project exists. `search_docs` returns snippets and
`read_doc` is a separate call *because* of it. If a change makes search verbose,
it has broken the point of the project.

And the budget must be *spent on something*. **`SEARCH_SQL` dedupes by
`(source, path)`, never by anchor**: eight rows must be eight distinct pages,
because the only decision a search supports is *which page to read*, and a second
section of a page the caller already has spends a slot on nothing. The 200-char
snippet is what pays for it. (Per-anchor dedup delivered 4.5 distinct pages per
search; recall@8 0.859 → 0.897, precision unmoved on every ruler.)

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

### And a dead end is the loudest failure of all

`search_docs` was healthy the whole time; what was broken sat one call later.
`read_doc` **could not return 22 sections at all** — a caller who did exactly the
right thing hit a wall with nothing to do next. Four invariants came out of it,
and each is a way to fail that now points somewhere (`git log 94933e8`):

- **An over-long section is outlined if it has children and paginated if it does
  not** (`_shrink`). Thirteen of the 22 are one enormous table — the settings
  reference, the env-vars reference, every slash command — and for those an outline
  is not a poor answer, it is the wrong question. `part=` is the escape hatch that
  always exists.
- **An outline reads the level *below the parent*.** An H3's children are H4s;
  outlining `en/hooks` § `PreToolUse` against H2/H3 gave a mid-row table cut above
  **an empty menu**. Every anchor an outline prints round-trips through `read_doc`
  — there is a corpus-wide test.
- **The anchor `search_docs` hands you must be one `read_doc` accepts.**
  `slug_style` is *not in the shipped index*, so the server does not try to know
  it: it offers all three spellings and keeps whichever the caller used
  (`SLUG_STYLES`). That makes 4 headings in 638 pages ambiguous, and `read_doc`
  says so rather than returning the first of two in silence.
- **A `LIMIT` is a lie told quietly.** `list_pages` (once 7,600 tokens on
  claude-code, from the tool that calls itself a cheap map), `grep_docs`' per-page
  cap, and the outlink footer all now name what they left out, with an exact total.

**The rescue's false alarms are the price of the fix, and they are not cheap.**
Over 16 natural-language questions it fired 5 times and 4 were noise — `stop` (105
pages), `happens` (44), `prompt` (49) — naming unrelated pages and telling the
caller to read one before answering. The whole sandbox fix rests on the caller
*believing* the NOTE, and a note that cries wolf spends exactly that. Five ways to
suppress it were measured; all five are in the rejected table. Nothing cheap in
this index buys them down. What the rescue *still* cannot see is under **Known,
and not fixed** — it is the largest open defect in the project.

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
| **Any query-time signal that the eight rows are weak.** Four measured over 500 real questions, baseline hit@8 0.864 | None separates. **Per-row term coverage** (the best row's share of the query's terms) runs 0.79 → 0.89 across its whole range — the intuition is that OR scatters a bad query across rows that each match a different slice, and it is simply not true. **Top-row coverage**, same. **Union coverage — which is exactly the rescue's trigger — carries almost nothing**: 479 of 500 queries have every term covered somewhere, at the baseline hit rate. **BM25 margin** stayed dead, as the row below says it would. |
| **Tightening the rescue's trigger to fire more** — testing the word against title/heading instead of the whole chunk | The obvious reading of "a caller routes on titles, so a word buried in a body never *reached* them", and it is a disaster: **291 fires over 500 questions, 246 of them at a caller who already had the answer.** It warns that `hook events list` does not contain `list` while handing back `en/hooks`. Content words live in bodies; that is what bodies are. `visible` (title + heading + the 200-char snippet the caller can actually see) is the principled version and it is no better — precision 0.023. Measured, all three, in `scripts/eval_rescue.py`. |
| Raising `MIN_CHUNK` off the floor because it drops 369 sections | Almost all of what it drops is a JSX landing stub (`<CodexCliLanding />`). The real short sections it loses (`Vim editor mode`) are covered elsewhere in the corpus and still rank. Not a user-visible bug. Verify the claim before acting on it: a report that `PermissionBehavior` was unsearchable was simply wrong — it ranks first. |

## Retrieval changes need evidence

Do not tune by feel. `scripts/eval_search.py` scores three gold sets — and there is
a fourth, `scripts/eval_rescue.py`, for the NOTE. **Each measures exactly one
thing, and using the wrong one has shipped a bad change more than once.**

- **hand** — 15 hand-written questions. Precision. Realistic, but **a one-case
  swing is noise**.
- **auto** — 284 cases (it moves with the corpus): each page's llms.txt
  description as the query, that page as the answer. Broad ranking movement. Fair
  because `description` lives only in `pages` and is not one of the indexed FTS
  columns, so it is a paraphrase, not the text being searched.
- **anchor** — 1,956 cases: the anchor text of the docs' own links
  (`[hook events list](/en/hooks)` → `en/hooks`). **Recall@8, and nothing else.**
- **rescue** (`scripts/eval_rescue.py`, not in CI — it spends model calls) — 500
  natural-language *questions*, built by expanding each anchor phrase into the
  sentence a developer would type. **The expander sees only the phrase and the
  product name, never the target page**, so it cannot copy vocabulary it was never
  shown and the gold comes free from the anchor set. It exists because the NOTE is
  a *confidence* signal and nothing else here has a question with filler in it —
  and filler is the whole problem. It scores the four outcomes that matter:
  **TRUE** (shot 1 missed, the NOTE named the gold), **FALSE** (missed, named
  something else), **CRY WOLF** (shot 1 *had* the gold and the NOTE sent the caller
  away anyway), and **silent** (missed, and nothing warned). Cry-wolf is the one
  that costs; see "Known, and not fixed".

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

### An anchor miss is 59% not a miss. Size a problem before you fix it.

`scripts/eval_served.py` shows a judge the question and the eight rows as
`search_docs` renders them, **never the gold label**, and asks the only question
that matters: could a developer answer from these? Of the 68 anchor-gold MISSES in
500 questions — **SERVED 40 (59%)**, PARTIAL 19, **FAILED 9 (13%)**. `How do I set
up a managed policy?` returns `en/admin-setup#decide-what-to-enforce` and is graded
**wrong**, because that one link pointed at `en/memory`.

So **counting anchor misses overstates the real failure rate by 8x** — 13.6% against
a true 1.8%. Recall@8 is still the right ruler for *comparing* two rankers, because
the noise is constant across them. It is **not** a count of failed callers, and it
was being read as one: "sixteen rows are worth +4pp of recall" is true, and worth
less than it sounds, because most of what those rows recover was already served.

The judge wobbles a case or two per run (FAILED held at 9 across two); read the
split as ±. ~70 model calls, so it is not in CI and it is not for A/B-ing a ranker.
It is for finding out whether the thing you are about to spend a week on is 13% or
2%.

**The auto set scores ranking. It cannot score confidence.** Its query is the
page's `description` — one long, distinctive sentence, which is nothing like the
three-to-six keywords a caller types. So any statistic that reads the *shape* of
the query or of the score curve will look superb on it and collapse in use. BM25
margin did exactly that: 0.993 vs 0.55 hit@1 on the auto set, and live,
`rate limit 429 retry backoff` lands the right page with a margin of 0.028 while
`output styles` lands it with 0.003. A short keyword query scores its top few
chunks nearly level *when it is right*. Measure confidence against the hand set
and real questions, never against the auto set.

**And the anchor set cannot score a reformulation.** `scripts/eval_2shot.py`
(~200 model calls, not in CI) asked whether a second search re-aimed at the docs'
vocabulary beats simply showing more rows. Headline 0.897 → 0.945 — but the
control, *the same query with sixteen rows*, already gives **0.937**. So +4.0pp is
slots and **+0.8 is the rewrite**, and what it recovered says why: `learn more
about hooks →` → `hooks`. **The anchor set's misses are link-label noise, not
vocabulary mismatch**, and the flagship case is not in the set at all. A third
ruler with a third blind spot: it cannot judge anything about how a query is
phrased. Two things it *did* establish, both bigger than the headline — **sixteen
rows are worth +4pp**, which is exactly why a *second* search is the right shape
(same ceiling at ~550 expected tokens, not 1,000, because the 90% that already
work never pay); and **0.945 assumes an oracle**, because the eval told the
rewriter it had missed. Do not quote it. (`git log 039d2bf`.)

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
- **The silent weak result — real, and 1.8% of questions, not the 13% it looked
  like.** Asked `limit which model an org member can select`, Cursor's docs return
  org roles and spend limits, no NOTE — every word matched *something* — and the
  answer is not in the eight. `rescue_term` only sees a word that reached
  *nothing*, so the failure where every word reached the **wrong** thing is
  invisible.

  **Size it before you spend a week on it.** Counting anchor-gold misses says this
  happens 13.6% of the time. A gold-blind judge says 59% of those misses are pages
  that answer the question perfectly well and were graded wrong (`eval_served.py`).
  The real rate is **9 in 500**. It is worth fixing and it is not an emergency, and
  the difference between those two sentences is a week.

  The half that is fixed: **the caller can usually produce the missing name.**
  Closed-book, the model guessed "Model Allowlist? Allowed Models? *Model
  Access*?" — and `model access` returns the right page at 1, 2 and 3. It held the
  key and never used it, because nothing told it to. `search_docs` now says a
  search costs ~500 tokens and it should **budget for two**. No index change.

  The half that is not: **nothing can tell the caller the rows are weak.** Four
  candidate signals are in the rejected table. The one that was supposed to do it is
  the NOTE, and —

- **The rescue costs more than the disease it treats. Leave it alone.** Over 500
  natural-language questions (`scripts/eval_rescue.py`, `scripts/eval_served.py`):

  | | per 500 questions |
  | --- | --- |
  | a caller is genuinely failed, silently (judge-confirmed) | **9 (1.8%)** |
  | the NOTE fires | 21 (4.2%) |
  | …**at a caller who already had the answer in the eight rows** | **18 (3.6%)** |
  | …and catches one of the 9 real failures | **1** |

  It warns on `work`, `manage`, `using`, `configure`, `integrating`, `difference` —
  grammatical filler that survives the stoplist and carries no topic. The one thing
  it *reliably* rescues is a version number (`2.1.114` → `en/changelog`).

  The cry-wolf count is the trustworthy one: "the gold was in the eight rows" is a
  *positive* fact, so label noise cannot inflate it. Search demonstrably found the
  page and the NOTE told the caller to go read three others.

  **So do not try to fix the trigger, and do not chase the silent failure with it.**
  Every reading of "this word did not reach the results" is in the rejected table,
  the disease is 1.8%, and the cure already runs at 3.6%. On the hand set — keyword
  queries, which is what `search_docs` asks callers to write — the NOTE fires zero
  times and cry-wolfs zero times. **The whole pathology is in natural-language
  phrasing**, and the founding case (`sandbox`, a distinctive noun in a keyword
  query) still works. That is the shape it is good at; leave it there.

  Two facts to keep in view if you come back to this. The trigger is **a function of
  `limit`** — `unmatched_terms` looks for the word anywhere in the returned chunks,
  bodies included, so more rows means more places for it to be buried and the
  warning quietly switches off (`limit=3` fires on the query above and names the
  answer page first; `limit=8` says nothing). And **nothing measured separates a
  topic from a verb**: five suppressors and three trigger variants, all in the
  rejected table. If you find a signal that does, this is where to spend it.

## Verify before committing

```bash
uv run pytest -q
uv run anydocs-build                      # real ingest; ~1 min
uv run python scripts/eval_search.py      # no regression
uv run python scripts/verify_anchors.py   # anchors still resolve live
```

`scripts/eval_2shot.py` is not part of that loop — it spends ~200 model calls and
answers one question, about reformulation, that nothing in CI can regress.
