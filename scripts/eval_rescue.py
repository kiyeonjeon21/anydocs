"""A ruler for the rescue NOTE, which has never had one.

`search_docs` warns when a query word reached none of the results and names the
pages it is really on. That NOTE is the project's loudest safety net — the sandbox
fix is nothing but the caller *believing* it — and its precision has never been
measured. Sixteen hand-counted questions is all there has ever been, and 4 of the
5 fires in them were noise.

So nothing can be changed about it safely. In particular `unmatched_terms` has a
bug (AGENTS.md, "Known, and not fixed"): it asks whether a word appears anywhere
in the returned chunks *including their bodies*, so the more rows you return the
likelier it is buried in one, and the warning silently switches itself off. The
obvious fix is to scope the test to the fields a caller actually routes on —
title and heading. It necessarily fires MORE. Without a ruler, "more" is
indistinguishable from "worse".

## The gold set

The rescue is a *confidence* signal, and AGENTS.md is explicit: measure confidence
against real questions, never against the auto set, whose query is a page's own
description — one long distinctive sentence with no filler in it at all. Filler is
the whole problem: `stop`, `happens`, `prompt` survive the stoplist, carry no
topic, and fire the false alarms.

Neither does the anchor set have any: its queries are 2-6 word link labels.

So build questions from it. Each anchor phrase is expanded into the sentence a
developer would actually type — by a model that sees ONLY the phrase and the
product name, **never the target page**. The leak is structurally impossible: the
expander cannot copy vocabulary it was never shown. The gold path comes from the
anchor set, and the expansion is what introduces the filler words that make the
rescue misfire.

## What is scored

Per question, given shot 1's eight rows:

                      | shot 1 HAS the gold | shot 1 MISSED it
    ------------------|---------------------|---------------------------------
    no NOTE           | fine                | SILENT (the open defect)
    NOTE fires        | CRY WOLF            | TRUE if it names the gold,
                      |                     | FALSE if it points elsewhere

**CRY WOLF is the one that costs.** The caller was holding the answer and got told
to go read three other pages first. Every one of those spends the credibility the
sandbox fix is built on.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from anydocs.artifact import ensure_index  # noqa: E402
from anydocs.index import connect  # noqa: E402
from anydocs.query import query_units, rescue_term, search  # noqa: E402
from anydocs.server import RESCUE_MAX  # noqa: E402

from eval_search import GOLD, anchor_gold  # noqa: E402

BATCH = 12
WORKERS = 6
NUMBERED = re.compile(r"^\s*(\d+)[.)]\s*(.+?)\s*$")

EXPAND = """A developer is about to search {product}'s documentation.

For each item below you get a short phrase — the way the docs' own authors refer to
one of their pages when linking to it. Write the question the developer would
actually type to find that page: a natural sentence, in the words a working
developer reaches for, not a keyword list and not the phrase copied back.

You have not seen the page and must not pretend to. Do not invent config keys or
API names. Just ask the question.

Reply with one line per item, "N. <question>", and nothing else.

{items}"""


@dataclass
class Verdict:
    fired: int = 0
    true: int = 0
    false: int = 0
    crywolf: int = 0
    silent: int = 0  # shot 1 missed and nothing warned — the open defect
    misses: int = 0  # shot 1 missed, warned or not
    examples: dict[str, list[str]] = field(default_factory=dict)


WORD = re.compile(r"\w+")

# Three readings of "the word did not reach the results", and they are not close.
#
#   body     what ships. The word is looked for anywhere in the returned chunks,
#            bodies included — so it can be buried 3 KB down in a page the caller
#            will never scroll, and the warning stays quiet. This is why the NOTE
#            is a function of `limit`: more rows, more places to be buried.
#   visible  what the caller can actually SEE: the title, the heading, and the
#            200-char snippet. Nothing else is on their screen.
#   heading  title and heading only. The strictest reading, and the one I was
#            about to ship on intuition.
VARIANTS = ("body", "visible", "heading")


def missed_terms(conn, query: str, rows, variant: str) -> list[str]:
    """Query words that reached none of the results, under one reading of 'reached'."""
    ids = [r["chunk_id"] for r in rows]
    if not ids:
        return []

    if variant == "visible":
        seen = {
            w.lower()
            for r in rows
            for w in WORD.findall(f"{r['title']} {r['heading']} {r['snip']}")
        }
        return [
            u.strip('"')
            for u in query_units(query)
            if not set(WORD.findall(u.strip('"').lower())) <= seen
        ]

    holes = ",".join("?" * len(ids))
    sql = f"SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid IN ({holes}) LIMIT 1"
    out = []
    for unit in query_units(query):
        expr = f"{{title heading}} : {unit}" if variant == "heading" else unit
        if not conn.execute(sql, [expr, *ids]).fetchone():
            out.append(unit.strip('"'))
    return out


def score(conn, gold: list[tuple[str, str, tuple[str, ...]]], variant: str) -> Verdict:
    v = Verdict(examples={"true": [], "crywolf": [], "false": []})
    for src, question, paths in gold:
        rows, _ = search(conn, question, sources=[src], limit=8)
        if not rows:
            continue
        hit = bool({r["path"] for r in rows} & set(paths))
        v.misses += not hit
        terms = missed_terms(conn, question, rows, variant)

        if not terms:
            v.silent += not hit
            continue

        v.fired += 1
        # rescue_term yields "source/path" strings, which is what the NOTE prints.
        named: set[str] = set()
        for t in terms:
            pages, _ = rescue_term(conn, t, [src], limit=RESCUE_MAX)
            named |= set(pages)
        wanted = {f"{src}/{p}" for p in paths}

        if hit:
            kind = "crywolf"
            v.crywolf += 1
        elif named & wanted:
            kind = "true"
            v.true += 1
        else:
            kind = "false"
            v.false += 1
        if len(v.examples[kind]) < 5:
            got = f" -> {sorted(named & wanted)[0]}" if kind == "true" else ""
            v.examples[kind].append(f"[{src}] {question!r}  missed={terms}{got}")
    return v


def expand(batch: list[tuple[int, str, str]], cwd: str, product: str) -> dict[int, str]:
    items = "\n".join(f"{n}. {phrase}" for n, (_, _, phrase) in enumerate(batch, 1))
    proc = subprocess.run(
        [
            "claude", "-p", EXPAND.format(product=product, items=items),
            "--model", "haiku",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "Bash,Read,Grep,Glob,WebFetch,WebSearch,Task,Edit,Write",
        ],
        capture_output=True, text=True, cwd=cwd, timeout=300,
    )
    out: dict[int, str] = {}
    for line in proc.stdout.splitlines():
        if (m := NUMBERED.match(line)) and 1 <= int(m[1]) <= len(batch):
            out[batch[int(m[1]) - 1][0]] = m[2]
    return out


def question_gold(conn, sources_dir: Path, cache: Path, n: int):
    """Anchor phrases, expanded into the questions a developer would type."""
    anchors = anchor_gold(conn, sources_dir)
    if n <= 0:
        return []
    # Deterministic sample, spread across the corpus rather than the first N.
    picked = anchors[:: max(1, len(anchors) // n)][:n]

    seen: dict[str, str] = json.loads(cache.read_text()) if cache.exists() else {}
    todo = [(i, s, p) for i, (s, p, _) in enumerate(picked) if str(i) not in seen]
    if todo:
        by_src: dict[str, list] = {}
        for item in todo:
            by_src.setdefault(item[1], []).append(item)
        jobs = [
            (items[i : i + BATCH], src)
            for src, items in by_src.items()
            for i in range(0, len(items), BATCH)
        ]
        with tempfile.TemporaryDirectory() as clean:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for got in pool.map(lambda j: expand(j[0], clean, j[1]), jobs):
                    seen |= {str(k): v for k, v in got.items()}
                    print(f"  expanded {len(seen)}/{len(picked)}", end="\r", flush=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(seen, indent=1))
    print(f"  expanded {len(seen)}/{len(picked)}      ")
    return [(s, seen[str(i)], (g,)) for i, (s, _, g) in enumerate(picked) if str(i) in seen]


def report(name: str, gold, conn, *, show: str = "visible") -> None:
    if not gold:
        return
    print(f"\n{'=' * 78}\n{name}  ({len(gold)} questions)\n{'=' * 78}")
    print(f"{'':22}{'fires':>6}{'TRUE':>6}{'FALSE':>7}{'CRYWOLF':>9}{'silent':>8}  precision")
    shown = None
    for variant in VARIANTS:
        v = score(conn, gold, variant)
        prec = v.true / v.fired if v.fired else 0.0
        tag = " <- ships" if variant == "body" else ""
        print(
            f"  {variant:20}{v.fired:6d}{v.true:6d}{v.false:7d}{v.crywolf:9d}{v.silent:8d}"
            f"     {prec:.3f}{tag}"
        )
        if variant == show:
            shown = v
    print(f"  (shot 1 missed the gold in {shown.misses} of {len(gold)})")
    for kind in ("true", "crywolf", "false"):
        if shown.examples[kind]:
            print(f"\n  {kind.upper()} — under '{show}':")
            for e in shown.examples[kind]:
                print(f"    {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources")
    ap.add_argument("--cache", default="build/rescue_questions.json")
    ap.add_argument("-n", type=int, default=500)
    ap.add_argument("--show", choices=VARIANTS, default="body", help="whose examples to print")
    args = ap.parse_args()

    conn = connect(ensure_index(), read_only=True)

    hand = [(c.source, c.query, c.gold) for c in GOLD]
    report("hand — 15 real questions, nothing generated", hand, conn, show=args.show)

    print("\nbuilding the question set (anchor phrases -> natural questions)...")
    gold = question_gold(conn, Path(args.sources), Path(args.cache), args.n)
    report("questions — anchor phrases expanded, gold inherited", gold, conn, show=args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
