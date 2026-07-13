"""Sweep chunk size and bm25 weights against two gold sets.

Re-chunking needs no network: `pages.body` is already in the index, so a whole
rebuild is a few seconds and a sweep is cheap. Scored on both the hand-written
questions and the 276 auto-derived ones, because 15 cases cannot choose a
hyperparameter — a one-case swing there is noise.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from anydocs import chunk as chunkmod
from anydocs.artifact import ensure_index
from anydocs.chunk import chunk_page
from anydocs.index import SCHEMA
from anydocs.models import Page
from anydocs.query import POOL, SEARCH_SQL, SNIPPET_TOKENS, query_units

sys.path.insert(0, str(Path(__file__).parent))
from eval_search import GOLD  # noqa: E402

SLUG_STYLE = {"claude-code": "collapse", "cursor": "collapse", "codex": "verbatim",
              "xai": "verbatim"}


def load_pages(src: sqlite3.Connection) -> list[Page]:
    src.row_factory = sqlite3.Row
    return [
        Page(r["source"], r["path"], r["url"], r["title"], r["description"], r["body"])
        for r in src.execute("SELECT * FROM pages")
    ]


def reindex(pages: list[Page], max_chunk: int) -> sqlite3.Connection:
    chunkmod.MAX_CHUNK = max_chunk
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pages VALUES (?,?,?,?,?,?)",
        [(p.source, p.path, p.url, p.title, p.description, p.body) for p in pages],
    )
    rows = [
        (c.source, c.path, c.anchor, c.breadcrumb, c.title, c.heading, c.body)
        for p in pages
        for c in chunk_page(p, SLUG_STYLE.get(p.source, "collapse"))
    ]
    conn.executemany(
        "INSERT INTO chunks(source,path,anchor,breadcrumb,title,heading,body)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid,title,heading,body)"
        " SELECT id,title,heading,body FROM chunks"
    )
    return conn


def hand_score(conn, w) -> tuple[int, int, bool]:
    sql = SEARCH_SQL.format(source_filter="AND c.source IN (?)", pool=POOL)
    at1 = at3 = 0
    fixed = False
    for case in GOLD:
        expr = " OR ".join(query_units(case.query))
        paths = [r["path"] for r in conn.execute(sql, [*w, SNIPPET_TOKENS, expr, case.source, 3, 3])]
        top = bool(paths) and paths[0] in case.gold
        in3 = bool(set(paths) & set(case.gold))
        at1 += bool(top)
        at3 += bool(in3)
        if case.query == "settings file precedence order" and in3:
            fixed = True
    return at1, at3, fixed


def auto_score(conn, w) -> tuple[float, float]:
    """Query = the page's llms.txt description; gold = that page.

    Fair because `description` lives only in `pages` — it is not one of the
    indexed FTS columns, so this is a paraphrase, not the text being searched.
    cursor/xai are excluded: their descriptions are scraped from the body.
    """
    sql = SEARCH_SQL.format(source_filter="AND c.source IN (?)", pool=POOL)
    gold = conn.execute(
        "SELECT source,path,description FROM pages"
        " WHERE source IN ('claude-code','codex') AND length(description) > 40"
    ).fetchall()
    at1 = 0
    rr = 0.0
    for g in gold:
        expr = " OR ".join(query_units(g["description"]))
        paths = [r["path"] for r in conn.execute(sql, [*w, SNIPPET_TOKENS, expr, g["source"], 5, 5])]
        if paths and paths[0] == g["path"]:
            at1 += 1
        for i, p in enumerate(paths, 1):
            if p == g["path"]:
                rr += 1 / i
                break
    n = len(gold)
    return at1 / n, rr / n


def main() -> int:
    pages = load_pages(sqlite3.connect(f"file:{ensure_index()}?immutable=1", uri=True))
    print(f"{len(pages)} pages re-chunked in memory (no network)\n")

    sizes = [1000, 1500, 2000, 2500, 3000, 4000]
    weights = [(10.0, 5.0, 1.0), (10.0, 10.0, 1.0), (5.0, 10.0, 1.0)]

    print(f"{'chunk':>6}{'chunks':>8}{'title/head':>12}"
          f"{'auto@1':>9}{'autoMRR':>9}{'hand@1':>8}{'hand@3':>8}{'prec?':>7}")
    print("-" * 68)
    for size in sizes:
        conn = reindex(pages, size)
        n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        for w in weights:
            a1, mrr = auto_score(conn, w)
            h1, h3, fixed = hand_score(conn, w)
            print(f"{size:>6}{n:>8}{f'{w[0]:.0f}/{w[1]:.0f}':>12}"
                  f"{a1:>9.3f}{mrr:>9.3f}{h1:>6}/15{h3:>6}/15{'YES' if fixed else '-':>7}")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
