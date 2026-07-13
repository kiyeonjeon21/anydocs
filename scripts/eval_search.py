"""Score retrieval strategies against a gold set. Empirical, not theoretical.

Each case names the page a competent human would open. A strategy is judged on
whether that page appears at rank 1 (hit@1) and in the top 3 (hit@3).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass

from anydocs.artifact import ensure_index
from anydocs.index import connect
from anydocs.query import BM25_WEIGHTS, POOL, SEARCH_SQL, SNIPPET_TOKENS, query_units


def doc_freq(conn: sqlite3.Connection, unit: str) -> int:
    try:
        return conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (unit,)
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


@dataclass
class Case:
    query: str
    source: str
    gold: tuple[str, ...]  # any of these page-path substrings is a correct answer


GOLD = [
    Case("hook events list", "claude-code", ("en/hooks",)),
    Case("what hook events exist", "claude-code", ("en/hooks",)),
    Case("settings file precedence order", "claude-code", ("en/settings",)),
    Case("how do I add a hook", "claude-code", ("en/hooks",)),
    Case("restrict which tools a subagent can use", "claude-code", ("en/sub-agents",)),
    Case("MCP server scopes local project user", "claude-code", ("en/mcp",)),
    Case("--dangerously-skip-permissions", "claude-code", ("en/cli-reference",)),
    Case("output styles", "claude-code", ("en/output-styles",)),
    Case("slash command frontmatter", "claude-code", ("en/slash-commands",)),
    Case("plugin marketplace", "claude-code", ("en/plugin",)),
    # Two pages genuinely answer this: `sandboxing` explains the modes, and
    # config-advanced has a section literally titled "Approval policies and
    # sandbox modes". Scoring one of them as a miss would be scoring the gold
    # set's opinion, not the retrieval.
    Case("sandbox modes approval policy", "codex", ("sandboxing", "config-advanced")),
    Case("AGENTS.md nesting precedence", "codex", ("agents-md",)),
    Case("config.toml model provider", "codex", ("config",)),
    Case("codex cloud environment setup", "codex", ("cloud",)),
    Case("custom prompts slash commands", "codex", ("custom-prompts",)),
]


def and_all(units: list[str], **_) -> list[str]:
    return [" ".join(units), " OR ".join(units)]


def or_only(units: list[str], **_) -> list[str]:
    return [" OR ".join(units)]


def df_and(units: list[str], *, df, total, cutoff: float) -> list[str]:
    counts = {u: df(u) for u in units}
    keep = [u for u in units if 0 < counts[u] <= cutoff * total]
    if not keep:
        present = [u for u in units if counts[u]]
        keep = [min(present, key=lambda u: counts[u])] if present else []
    rungs = [" ".join(keep)] if keep else []
    rungs += [" ".join(units), " OR ".join(units)]
    return list(dict.fromkeys(rungs))


def run(conn: sqlite3.Connection, expr: str, source: str, limit: int) -> list[str]:
    sql = SEARCH_SQL.format(source_filter="AND c.source IN (?)", pool=POOL)
    params = [*BM25_WEIGHTS, SNIPPET_TOKENS, expr, source, limit, limit]
    try:
        return [r["path"] for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def evaluate(conn, name: str, strategy, **kw) -> tuple[int, int, list[str]]:
    total = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    at1 = at3 = 0
    misses = []
    for case in GOLD:
        units = query_units(case.query)
        rungs = strategy(units, df=lambda u: doc_freq(conn, u), total=total, **kw)
        paths: list[str] = []
        for expr in rungs:
            paths = run(conn, expr, case.source, 3)
            if paths:
                break
        if paths and any(g in paths[0] for g in case.gold):
            at1 += 1
        if any(g in p for p in paths for g in case.gold):
            at3 += 1
        else:
            misses.append(f"{case.query!r} -> {paths[0] if paths else '(none)'}")
    return at1, at3, misses


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = connect(ensure_index(), read_only=True)
    n = len(GOLD)
    strategies = [
        ("AND-first", and_all, {}),
        ("OR-only", or_only, {}),
        ("DF-AND 0.05", df_and, {"cutoff": 0.05}),
        ("DF-AND 0.10", df_and, {"cutoff": 0.10}),
        ("DF-AND 0.15", df_and, {"cutoff": 0.15}),
        ("DF-AND 0.20", df_and, {"cutoff": 0.20}),
        ("DF-AND 0.30", df_and, {"cutoff": 0.30}),
    ]
    print(f"{'strategy':<14}{'hit@1':>8}{'hit@3':>8}")
    print("-" * 30)
    results = []
    for name, fn, kw in strategies:
        at1, at3, misses = evaluate(conn, name, fn, **kw)
        results.append((name, at1, at3, misses))
        print(f"{name:<14}{at1}/{n:<6}{at3}/{n:>5}")

    if args.verbose:
        for name, _, _, misses in results:
            if misses:
                print(f"\n{name} misses:")
                for m in misses:
                    print(f"  {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
