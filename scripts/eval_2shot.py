"""Does a second search buy anything a bigger first one would not?

`search_docs` costs ~500 tokens precisely so a caller can afford to run it twice.
Nothing in the server ever invited the second call, and the one open defect in
AGENTS.md — the silent weak result — is exactly a query that needed one:
`limit which model an org member can select` returns org roles and spend limits,
while `model access control`, the docs' own name for the feature, returns the
right page first. The caller can usually produce that name. It just never did.

This scores that claim on the anchor gold set (recall@8, the only ruler here that
does not leak), and against the one control that matters:

  shot1@8     the ranker as shipped
  shot1@16    THE NULL HYPOTHESIS. A 2-shot caller reads 16 rows. If simply
              showing 16 rows of the SAME query recovers as much, then the
              reformulation is worth nothing and the gain is just slots.
  2shot@8+8   shot 1, then — only where shot 1 missed — a model rewrites the
              query from the 8 wrong titles alone and we search again.
              Union of both result sets. Same 16 rows as the control.

The rewriter never sees the gold path. It sees what a caller sees: its own query,
the source, and the headings that came back. It runs in a scratch cwd with no
project context and no MCP, because AGENTS.md names `model access control` in
prose — a rewriter that reads it is not guessing, it is copying, and the number
would be a lie.

Understates the real gain, on purpose: an anchor phrase ("hook events list") is a
fragment, where a caller has a whole question to re-aim from.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from anydocs.artifact import ensure_index  # noqa: E402
from anydocs.index import connect  # noqa: E402
from anydocs.query import search  # noqa: E402

from eval_search import anchor_gold  # noqa: E402

BATCH = 12
WORKERS = 6
NUMBERED = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$")

PROMPT = """You are rewriting failed documentation search queries.

Each item below is a search that FAILED: the query returned eight results and none
of them was the right page. You get the query, the product whose docs were
searched, and the titles of the eight wrong results.

Rewrite each query. The original is phrased the way an outsider refers to the
topic; the index is lexical (BM25), so it only finds a page whose own text uses
your words. Guess what the documentation *calls* this thing — the feature name, the
config key, the API parameter — and use that. Short keyword queries beat sentences.
The wrong results tell you which words misfired; do not reuse them.

Reply with one line per item, "N. <rewritten query>", and nothing else.

{items}"""


def rewrite(batch: list[tuple[int, str, str, list[str]]], cwd: str) -> dict[int, str]:
    """Ask a model for a better query. Returns {case index: new query}."""
    items = "\n\n".join(
        f"{n}. product: {src}\n   failed query: {q}\n   wrong results: {'; '.join(titles)}"
        for n, (_, src, q, titles) in enumerate(batch, 1)
    )
    proc = subprocess.run(
        [
            "claude", "-p", PROMPT.format(items=items),
            "--model", "haiku",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "Bash,Read,Grep,Glob,WebFetch,WebSearch,Task,Edit,Write",
        ],
        capture_output=True, text=True, cwd=cwd, timeout=300,
    )
    out: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        if m := NUMBERED.match(line):
            n = int(m[1])
            if 1 <= n <= len(batch):
                out[batch[n - 1][0]] = m[2]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources")
    ap.add_argument("--cache", default="build/2shot_rewrites.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    conn = connect(ensure_index(), read_only=True)
    gold = anchor_gold(conn, Path(args.sources))
    print(f"anchor gold: {len(gold)} cases\n")

    # Shot 1, and the control: the same query, twice as many rows.
    misses: list[tuple[int, str, str, list[str]]] = []
    hit8 = hit16 = 0
    first_pages: dict[int, set[str]] = {}
    for i, (src, query, path) in enumerate(gold):
        rows16, _ = search(conn, query, sources=[src], limit=16)
        top8 = rows16[:8]
        pages8 = {r["path"] for r in top8}
        first_pages[i] = pages8
        hit8 += path in pages8
        hit16 += path in {r["path"] for r in rows16}
        if path not in pages8:
            titles = [f"{r['title']} > {r['heading']}" for r in top8]
            misses.append((i, src, query, titles))

    n = len(gold)
    print(f"shot1@8    recall {hit8:5d}/{n} ({hit8 / n:.3f})   <- as shipped")
    print(f"shot1@16   recall {hit16:5d}/{n} ({hit16 / n:.3f})   <- CONTROL: same query, 16 rows")
    print(f"\nshot 1 missed {len(misses)} cases. Rewriting those.\n")

    cache_path = Path(args.cache)
    cached: dict[str, str] = {}
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())

    todo = [m for m in misses if str(m[0]) not in cached]
    if todo:
        with tempfile.TemporaryDirectory() as clean:
            batches = [todo[i : i + BATCH] for i in range(0, len(todo), BATCH)]
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for got in pool.map(lambda b: rewrite(b, clean), batches):
                    cached |= {str(k): v for k, v in got.items()}
                    print(f"  rewritten {len(cached)}/{len(misses)}", end="\r", flush=True)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cached, indent=1))
    print(f"  rewritten {len(cached)}/{len(misses)}      \n")

    # Shot 2: search the rewrite, union with shot 1's eight rows.
    recovered, unchanged, examples = 0, 0, []
    for i, src, query, _ in misses:
        new_q = cached.get(str(i))
        if not new_q:
            continue
        if new_q.strip().lower() == query.strip().lower():
            unchanged += 1
        rows, _ = search(conn, new_q, sources=[src], limit=8)
        gold_path = gold[i][2]
        if gold_path in {r["path"] for r in rows} | first_pages[i]:
            recovered += 1
            if len(examples) < 8:
                examples.append((src, query, new_q, gold_path))

    two = hit8 + recovered
    print(f"2shot@8+8  recall {two:5d}/{n} ({two / n:.3f})   <- shot1 + rewrite, 16 rows")
    print()
    print(f"recovered by the rewrite : {recovered}/{len(misses)} of shot-1 misses")
    print(f"beyond the control       : {two - hit16:+d} cases ({(two - hit16) / n:+.3f} recall)")
    print(f"rewrites that changed nothing: {unchanged}")

    if args.verbose and examples:
        print("\nrecovered:")
        for src, q, nq, path in examples:
            print(f"  [{src}] {q!r}\n      -> {nq!r}\n      => {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
