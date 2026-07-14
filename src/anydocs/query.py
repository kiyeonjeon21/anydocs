from __future__ import annotations

import math
import re
import sqlite3

# Weights: a hit in the page title beats one in a heading beats one in prose.
BM25_WEIGHTS = (10.0, 5.0, 1.0)
POOL = 200  # rows ranked before quotas are applied
# One row per page (see SEARCH_SQL) means 8 slots buy 8 pages instead of 4.5, and
# the extra rows have to come from somewhere: a shorter snippet. That is the
# right way round. search_docs exists to name the page worth reading — read_doc
# supplies the text — and what these lose is the tail of an API field list, not
# anything a caller routes on. Measured: 442 -> 500 tokens, and the whole budget
# now goes on distinct pages.
SNIPPET_TOKENS = 16  # FTS5 silently clamps this at 64
SNIPPET_CHARS = 200

TERM = re.compile(r"[0-9A-Za-zÀ-ɏ]+")

# Anything the tokenizer cannot use. The indexed docs are English, so a query in
# another script matches nothing — the words are dropped. Silently dropping them
# is the danger: "claude code 훅 이벤트" would quietly become "claude code" and
# return a confident, unrelated answer. Detect it and say so.
NON_LATIN = re.compile(r"[^\W\d_]", re.UNICODE)


def dropped_terms(raw: str) -> list[str]:
    """Words in the query that carry meaning but cannot reach the index."""
    return [
        w
        for w in raw.split()
        if not TERM.findall(w) and NON_LATIN.search(w)
    ]

# Filler that swamps BM25 on natural-language queries. Measured: "how do I add a
# hook" ranked an xAI FAQ ("How do I add other sign-in methods?") first until
# these were dropped; without them the real answer (hooks-guide › Set up your
# first hook) never surfaced.
STOP = frozenset(
    """a an the how do does did i you your to of in on for is are was were be
    can could should would what when where which with and or my me it this that
    there then from by at as if""".split()
)


NOT_CONTRACTION = re.compile(r"n[’']t\b")
CONTRACTION = re.compile(r"[’']\w{1,2}\b")


def query_units(raw: str) -> list[str]:
    """Split user text into quoted FTS5 units.

    Raw text is never interpolated: FTS5 reads `-`, `:`, `(`, `*` as operators,
    so `--dangerously-skip-permissions` or `Bash(git:*)` raise
    `fts5: syntax error`. Only bare word tokens are emitted, each quoted, which
    makes syntax injection structurally impossible.

    A word yielding several tokens was glued by `-`/`_`/`.`/`/` — i.e. it is a
    symbol — so it becomes an adjacency phrase: `--dangerously-skip-permissions`
    -> `"dangerously skip permissions"`, which matches the flag as written.

    **An apostrophe is not glue.** It took the same path and it should not have:
    `Where's` split to `["Where", "s"]` and became the phrase `"Where s"` — two
    words adjacent in that order, which appears in no document ever written. So it
    matched nothing, and `unmatched_terms` dutifully reported that nothing matched
    it, and the caller got a NOTE naming three pages where `Where s` supposedly
    lives. Over 500 natural-language questions this was **4 of the 25 rescues that
    fired**: pure noise, manufactured by punctuation.

    Contractions are stripped to their stem first, and the stem is usually a
    stopword anyway — `Where's` -> `Where`, `don't` -> `do`, `isn't` -> `is` — so
    the unit disappears entirely, which is what should have happened all along.
    """
    text = CONTRACTION.sub("", NOT_CONTRACTION.sub("", raw))
    words = [(w, TERM.findall(w)) for w in text.split()]
    kept = [ts for _, ts in words if ts and not (len(ts) == 1 and ts[0].lower() in STOP)]
    if not kept:  # a query made only of stopwords: keep them rather than match nothing
        kept = [ts for _, ts in words if ts]
    return ['"%s"' % " ".join(ts) for ts in kept]


def compile_query(raw: str) -> list[str]:
    """Compile user text into a ladder of MATCH expressions, tried in order.

    OR, not AND. AND looks like the precise choice, and it is a trap: a word
    like `list`, `file` or `order` sits in 10-18% of the corpus, so it
    discriminates nothing — but inside an AND it still has the power to *veto*
    the right answer. Asking for "hook events list" put a Python type reference
    first, because the canonical `Hooks reference > Hook events` section never
    happens to say "list". bm25's IDF already discounts those words to nearly
    zero, so OR loses no precision worth having and stops them from excluding
    anything. Scored on a 15-question gold set, OR beat AND-first and every
    document-frequency-thresholded variant on both hit@1 and hit@3.

    The static stopword list stays: grammatical filler (`how`, `do`, `a`) is
    frequent enough that even under OR it drags in whole-corpus noise — it once
    ranked an xAI FAQ ("How do I add other sign-in methods?") above the Claude
    Code hooks guide.
    """
    units = query_units(raw)
    if not units:
        return []
    return [
        " OR ".join(units),
        " OR ".join(f"{u}*" for u in units),  # prefix, for partial words
    ]


# One row per PAGE, not per section.
#
# It used to keep the best chunk of each (page, anchor) and allow a page two of
# the eight slots. That reads as generous and is not: `src_rank` is cut at the
# limit, so the candidate set was the top 8 *chunks* — and when three of those
# were second sections of pages already listed, the eight promised results came
# back as 4.5 distinct pages with nothing to refill the slots they took. Pages
# ranked ninth and tenth, one of which may be the answer, were never considered.
#
# A second section of a page the caller has already been handed adds nothing to
# the only decision search_docs supports: which page to read. Measured over 1,956
# anchor-text cases, spending those slots on new pages instead lifts recall@8
# from 0.851 to 0.902 and leaves precision untouched (hand 11/15 @1, 13/15 @3;
# auto hit@1 0.792 — identical).
SEARCH_SQL = """
WITH hits AS (
  SELECT c.id AS chunk_id,
         c.source, c.path, c.anchor, c.breadcrumb, c.title, c.heading,
         p.description,
         -bm25(chunks_fts, ?, ?, ?) AS score,
         snippet(chunks_fts, 2, '«', '»', '…', ?) AS snip
  FROM chunks_fts
  JOIN chunks c ON c.id = chunks_fts.rowid
  JOIN pages  p ON p.source = c.source AND p.path = c.path
  WHERE chunks_fts MATCH ?
    {source_filter}
  ORDER BY score DESC
  LIMIT {pool}
),
dedup AS (
  SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY source, path ORDER BY score DESC) AS rn
    FROM hits
  ) WHERE rn = 1
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY source ORDER BY score DESC) AS src_rank,
         ROW_NUMBER() OVER (ORDER BY score DESC)                     AS global_rank
  FROM dedup
)
SELECT chunk_id, source, path, anchor, breadcrumb, title, heading, description, score, snip
FROM ranked
WHERE global_rank <= 3 OR src_rank <= ?
ORDER BY score DESC
LIMIT ?
"""

FENCE = re.compile(r"^\s*(```|~~~).*$", re.MULTILINE)
WS = re.compile(r"\s+")
# `[hook events list](/en/hooks#hook-events)` reads as pure noise in a snippet,
# and the URL is usually longer than the text it labels. Stripped at display
# time only: taking it out of the indexed body instead measurably hurt ranking.
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
BARE_URL = re.compile(r"<https?://[^>]*>|https?://\S+")


TABLE_HEAD = re.compile(r"^\s*\|[^\n]*\|\s*\n\s*\|[\s|:-]+\|", re.MULTILINE)


def clean_snippet(snip: str, fallback: str = "") -> str:
    """Flatten a snippet to one short line.

    FTS5 returns raw markdown, so a hit inside a code block drags fences and
    indentation along. When the match was in the title/heading only, snippet()
    has no body match to centre on and just returns the head of the body — the
    absence of « » markers is how we detect that, and the page description is a
    far better thing to show.

    That head is often a table header, because the reference pages are built
    from tables. It is exactly the wrong answer on exactly the pages people
    search for most: asking about a CLI flag and being shown
    `| Flag | Description | Example |` tells you nothing.
    """
    snip = BARE_URL.sub(" ", LINK.sub(r"\1", snip))
    text = WS.sub(" ", FENCE.sub(" ", TABLE_HEAD.sub(" ", snip))).strip()
    if "«" not in text and fallback:
        text = WS.sub(" ", fallback).strip()
    if len(text) > SNIPPET_CHARS:
        text = text[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def unmatched_terms(
    conn: sqlite3.Connection, query: str, rows: list[sqlite3.Row]
) -> list[str]:
    """Query words that appear in none of the results.

    OR matching always finds something, and the words it finds are the wrong
    ones. Asking Claude Code's docs about "cursorrules composer tab autocomplete"
    returns keyboard-shortcut pages: `cursorrules` and `composer` are mentioned
    once or twice in the whole corpus and never rank, while `tab` and
    `autocomplete` are everywhere. Nothing in the reply says the distinctive
    words missed, so an agent can report the shortcut docs as an answer about
    Cursor compatibility.

    Checking the corpus is not enough — a word mentioned in one chunk out of
    4,000 is present but useless. What matters is whether it reached the results
    the caller is about to read.
    """
    ids = [r["chunk_id"] for r in rows]
    if not ids:
        return []
    sql = (
        f"SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? "
        f"AND rowid IN ({','.join('?' * len(ids))}) LIMIT 1"
    )
    missing = []
    for unit in query_units(query):
        hit = conn.execute(sql, [unit, *ids]).fetchone()
        if not hit:
            missing.append(unit.strip('"'))
    return missing


OUTLINKS_SQL = """
SELECT l.to_path AS path, p.title, p.description, l.in_seealso
FROM links l
JOIN pages p ON p.source = l.source AND p.path = l.to_path
WHERE l.source = ? AND l.from_path = ?
ORDER BY l.in_seealso DESC, l.ord
LIMIT ?
"""

OUTLINKS_COUNT_SQL = """
SELECT COUNT(*) FROM links l
JOIN pages p ON p.source = l.source AND p.path = l.to_path
WHERE l.source = ? AND l.from_path = ?
"""


def outlinks(
    conn: sqlite3.Connection, source: str, path: str, limit: int
) -> tuple[list[sqlite3.Row], int]:
    """The pages this page points at — the docs' own cross-references (links.py).

    Returns (rows, total). The total is counted separately and exactly, the way
    `rescue_term` does it, because `LIMIT` is a lie told silently: Claude Code's
    settings page cross-references 51 others and the footer shows 8. Saying "8"
    and meaning "8 of 51" is the same class of bug as the rest of this file.

    "See also" first: those are the links an author filed under an explicit
    read-this-next heading, as opposed to the ones that merely happen to appear
    in the prose. Within each group, the order the author wrote them, which is
    the only ordering here that is not a guess of ours.
    """
    rows = conn.execute(OUTLINKS_SQL, (source, path, limit)).fetchall()
    total = conn.execute(OUTLINKS_COUNT_SQL, (source, path)).fetchone()[0]
    return rows, total


RESCUE_SQL = """
SELECT c.source, c.path, -bm25(chunks_fts, ?, ?, ?) AS score
FROM chunks_fts
JOIN chunks c ON c.id = chunks_fts.rowid
WHERE chunks_fts MATCH ?
  {source_filter}
ORDER BY score DESC
LIMIT {pool}
"""

RESCUE_COUNT_SQL = """
SELECT COUNT(*) FROM (
  SELECT DISTINCT c.source, c.path
  FROM chunks_fts
  JOIN chunks c ON c.id = chunks_fts.rowid
  WHERE chunks_fts MATCH ?
    {source_filter}
)
"""


def rescue_term(
    conn: sqlite3.Connection, term: str, sources: list[str] | None, limit: int
) -> tuple[list[str], int]:
    """Where a word that missed the results actually lives. Returns (pages, total).

    Matching is OR, so one distinctive word can be outvoted by the common ones
    beside it and never reach the results at all. That is not hypothetical: asked
    for `headless -p allowedTools disallowedTools sandbox flag`, the ranker
    handed back the headless and CLI pages, `sandbox` matched nothing in any of
    them — and `en/sandboxing` sat in the index, unmentioned. The caller went on
    to report that Claude Code has no sandbox.

    Saying "no result contains sandbox" was not enough; the caller ignored it.
    Run the word on its own and name the pages that do contain it. The total
    matters too: a word in 14 pages is a topic that was missed, a word in 2 is a
    passing mention, and the caller cannot tell those apart from a list alone.
    """
    filt = f"AND c.source IN ({','.join('?' * len(sources))})" if sources else ""
    unit = f'"{term}"'
    params = [unit, *(sources or [])]
    rows = conn.execute(
        RESCUE_SQL.format(source_filter=filt, pool=POOL),
        [*BM25_WEIGHTS, *params],
    ).fetchall()
    # Counted separately, and exactly: the ranked query above stops at POOL
    # chunks, so the pages it happens to reach are a floor, not the total.
    total = conn.execute(
        RESCUE_COUNT_SQL.format(source_filter=filt), params
    ).fetchone()[0]
    pages = list(dict.fromkeys(f"{r['source']}/{r['path']}" for r in rows))
    return pages[:limit], total


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    sources: list[str] | None = None,
    limit: int = 8,
) -> tuple[list[sqlite3.Row], str]:
    """Run the ladder, stopping at the first rung that returns anything.

    Returns the rows plus the MATCH expression that produced them, so a caller
    (or a human debugging) can see what actually ran.
    """
    # Without a source filter, cap how many slots any one source may take, or a
    # query that happens to align with the biggest corpus crowds the others out.
    per_source = limit if sources else max(2, math.ceil(limit / 3))
    filt = f"AND c.source IN ({','.join('?' * len(sources))})" if sources else ""
    sql = SEARCH_SQL.format(source_filter=filt, pool=POOL)

    for expr in compile_query(query):
        params = [*BM25_WEIGHTS, SNIPPET_TOKENS, expr, *(sources or []), per_source, limit]
        rows = conn.execute(sql, params).fetchall()
        if rows:
            return rows, expr
    return [], ""
