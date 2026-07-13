from __future__ import annotations

import math
import re
import sqlite3

# Weights: a hit in the page title beats one in a heading beats one in prose.
BM25_WEIGHTS = (10.0, 5.0, 1.0)
POOL = 200  # rows ranked before quotas are applied
SNIPPET_TOKENS = 24  # FTS5 silently clamps this at 64
SNIPPET_CHARS = 300

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


def query_units(raw: str) -> list[str]:
    """Split user text into quoted FTS5 units.

    Raw text is never interpolated: FTS5 reads `-`, `:`, `(`, `*` as operators,
    so `--dangerously-skip-permissions` or `Bash(git:*)` raise
    `fts5: syntax error`. Only bare word tokens are emitted, each quoted, which
    makes syntax injection structurally impossible.

    A word yielding several tokens was glued by `-`/`_`/`.`/`/` — i.e. it is a
    symbol — so it becomes an adjacency phrase: `--dangerously-skip-permissions`
    -> `"dangerously skip permissions"`, which matches the flag as written.
    """
    words = [(w, TERM.findall(w)) for w in raw.split()]
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


SEARCH_SQL = """
WITH hits AS (
  SELECT c.source, c.path, c.anchor, c.breadcrumb, c.title, c.heading,
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
    SELECT *, ROW_NUMBER() OVER (PARTITION BY source, path, anchor ORDER BY score DESC) AS rn
    FROM hits
  ) WHERE rn = 1
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY source ORDER BY score DESC)       AS src_rank,
         ROW_NUMBER() OVER (PARTITION BY source, path ORDER BY score DESC) AS page_rank,
         ROW_NUMBER() OVER (ORDER BY score DESC)                           AS global_rank
  FROM dedup
)
SELECT source, path, anchor, breadcrumb, title, heading, description, score, snip
FROM ranked
WHERE page_rank <= 2
  AND (global_rank <= 3 OR src_rank <= ?)
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


def absent_terms(
    conn: sqlite3.Connection, query: str, sources: list[str] | None = None
) -> list[str]:
    """Query words that appear in no chunk at all.

    OR matching always finds something: asking about TensorFlow returns three
    confident-looking hits, because `model` and `loop` are everywhere in these
    docs. The scores are lower, but nothing tells the caller what "lower" means.
    Naming the word that is simply absent does — it turns a puzzling weak result
    into "the docs do not mention TensorFlow", and catches typos for free.
    """
    sql = "SELECT 1 FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?"
    params: list = []
    if sources:
        sql += f" AND c.source IN ({','.join('?' * len(sources))})"
        params = list(sources)
    sql += " LIMIT 1"

    missing = []
    for unit in query_units(query):
        try:
            hit = conn.execute(sql, [unit, *params]).fetchone()
        except sqlite3.OperationalError:
            continue
        if not hit:
            missing.append(unit.strip('"'))
    return missing


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
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            return rows, expr
    return [], ""
