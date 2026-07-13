"""Compare query strategies against the real index. Empirical, not theoretical."""

from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "build" / "anydocs.db"
TERM = re.compile(r"[0-9A-Za-zÀ-ɏ]+")

QUERIES = [
    "--dangerously-skip-permissions",
    "PreToolUse hook matcher",
    "how do I add a hook",
    "AGENTS.md",
    "grok model list",
    "MCP server configuration",
]


STOP = {
    "a", "an", "the", "how", "do", "does", "did", "i", "you", "to", "of", "in",
    "on", "for", "is", "are", "was", "were", "be", "can", "could", "should",
    "would", "what", "when", "where", "which", "with", "and", "or", "my", "me",
    "it", "this", "that", "there", "then", "from", "by", "at", "as", "if",
}


def units(raw: str, *, drop_stop: bool = False) -> list[str]:
    """A word that yields >1 term was glued by -_./: => it's a symbol => phrase."""
    out = []
    for word in raw.split():
        ts = TERM.findall(word)
        if not ts:
            continue
        if drop_stop and len(ts) == 1 and ts[0].lower() in STOP:
            continue
        out.append('"%s"' % " ".join(ts))
    return out or units(raw) if drop_stop else out


def or_only(raw: str) -> list[str]:
    u = units(raw)
    return [" OR ".join(u)] if u else []


def ladder(raw: str) -> list[str]:
    u = units(raw)
    if not u:
        return []
    rungs = [" ".join(u)]  # implicit AND
    if len(u) > 1:
        rungs.append(" OR ".join(u))
    rungs.append(" OR ".join(x + "*" for x in u))
    return rungs


def stop_ladder(raw: str) -> list[str]:
    """Drop filler words, then AND -> OR -> prefix-OR."""
    u = units(raw, drop_stop=True)
    if not u:
        return []
    rungs = [" ".join(u)]
    if len(u) > 1:
        rungs.append(" OR ".join(u))
    rungs.append(" OR ".join(x + "*" for x in u))
    return rungs


SQL = """
SELECT c.source, c.path, c.heading, -bm25(chunks_fts,10.0,5.0,1.0) AS s
FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
WHERE chunks_fts MATCH ? ORDER BY s DESC LIMIT 5
"""


def run(conn: sqlite3.Connection, rungs: list[str]) -> tuple[str, list]:
    for expr in rungs:
        try:
            rows = conn.execute(SQL, (expr,)).fetchall()
        except sqlite3.OperationalError as exc:
            return f"ERROR {exc}", []
        if rows:
            return expr, rows
    return "(no match)", []


def main() -> int:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    for q in QUERIES:
        print(f"\n{'=' * 78}\nQ: {q!r}")
        strategies = (("OR-only", or_only(q)), ("ladder", ladder(q)), ("stop+ladder", stop_ladder(q)))
        for label, rungs in strategies:
            expr, rows = run(conn, rungs)
            print(f"  [{label:<8}] {expr}")
            for src, path, heading, score in rows[:3]:
                print(f"      {score:6.2f}  {src}/{path}  › {heading[:40]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
