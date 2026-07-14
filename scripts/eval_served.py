"""The only question that matters, asked without a gold label: was the caller served?

Every other ruler here asks "did we return the page some link happened to point
at". That is a proxy, and `scripts/eval_rescue.py` turned up how badly it leaks:
of the 68 questions (in 500) where the anchor gold says search MISSED, a gold-blind
judge says **38 were served anyway**. The label was wrong, not the search.

    'How do I set up a managed policy?'  -> en/admin-setup#decide-what-to-enforce
                                            graded WRONG, gold is `en/memory`
    'security and privacy?'              -> en/security#privacy-safeguards
                                            graded WRONG, gold is `en/monitoring-usage`

So ask a judge instead, and never show it the label. It sees exactly what the
caller sees — the question and the eight rows as `search_docs` renders them — and
answers the caller's question:

    SERVED   at least one row is clearly the page to open
    PARTIAL  adjacent; they would get somewhere, not straight there
    FAILED   none of these is about what was asked. The caller is misled, or
             concludes the docs do not cover it.

**The judge is not decisive and must not be quoted as if it were.** Run to run it
flips cases at the margin — `What should I know about security and privacy?` came
back SERVED once and FAILED once. Read the split as ±, and only trust gaps that are
much wider than the wobble. SERVED 38 vs FAILED 9 is such a gap. A five-case
difference between two rankers would not be.

What this ruler is *for*: knowing how big a problem actually is before spending a
week on it. It priced the silent weak result at **1.8% of questions**, not the 13%
the anchor gold implied — which is why the rescue, firing on 4.2% and crying wolf
on 3.6%, costs more than the disease it treats.

What it is NOT for: A/B-ing a ranking change. It spends ~70 model calls and wobbles
by a couple of cases. Use `eval_search.py` for that, and use this to find out
whether the thing you are about to A/B is worth the week.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from anydocs.artifact import ensure_index  # noqa: E402
from anydocs.index import connect  # noqa: E402
from anydocs.query import search  # noqa: E402

from eval_rescue import question_gold  # noqa: E402

BATCH = 8
WORKERS = 6
VERDICT = re.compile(r"^\s*(\d+)[.)]\s*(SERVED|PARTIAL|FAILED)\s*[—\-:]*\s*(.*)$", re.I)

PROMPT = """You are auditing a documentation search engine.

Each case is a developer's question and the eight results the search returned — the
page path, its heading trail, and a snippet. You do NOT get the "correct" answer,
on purpose. Judge only this:

  Could a developer answer this question by opening one of these eight pages?

  SERVED   yes. At least one result is clearly the right page to read.
  PARTIAL  a result is adjacent; they would get somewhere, but not straight there.
  FAILED   no. None of these is about what was asked. The caller would be misled,
           or would conclude the docs do not cover it.

Be strict about FAILED and strict about SERVED. Most cases are one or the other.

Reply one line per case: "N. VERDICT — six words why". Nothing else.

{items}"""


def render(src: str, question: str, rows) -> str:
    out = [f"question: {question}", f"product: {src}", "results:"]
    for r in rows:
        snip = re.sub(r"\s+", " ", r["snip"] or "")[:150]
        out.append(f"  - {r['path']}#{r['anchor']} | {r['breadcrumb']} | {snip}")
    return "\n".join(out)


def judge(batch: list[tuple[str, str]], cwd: str) -> dict[str, tuple[str, str]]:
    items = "\n\n".join(f"=== case {n} ===\n{body}" for n, (_, body) in enumerate(batch, 1))
    proc = subprocess.run(
        [
            "claude", "-p", PROMPT.format(items=items),
            "--model", "sonnet",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "Bash,Read,Grep,Glob,WebFetch,WebSearch,Task,Edit,Write",
        ],
        capture_output=True, text=True, cwd=cwd, timeout=600,
    )
    out: dict[str, tuple[str, str]] = {}
    for line in proc.stdout.splitlines():
        if (m := VERDICT.match(line)) and 1 <= int(m[1]) <= len(batch):
            out[batch[int(m[1]) - 1][0]] = (m[2].upper(), m[3].strip())
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="sources")
    ap.add_argument("--questions", default="build/rescue_questions.json")
    ap.add_argument("--cache", default="build/served_verdicts.json")
    ap.add_argument("-n", type=int, default=500)
    ap.add_argument(
        "--all",
        action="store_true",
        help="judge every question, not only the ones the anchor gold calls a miss "
        "(slow, and the misses are where the label is suspect)",
    )
    args = ap.parse_args()

    conn = connect(ensure_index(), read_only=True)
    gold = question_gold(conn, Path(args.sources), Path(args.questions), args.n)

    cases, misses = [], 0
    for src, question, paths in gold:
        rows, _ = search(conn, question, sources=[src], limit=8)
        if not rows:
            continue
        hit = bool({r["path"] for r in rows} & set(paths))
        misses += not hit
        if hit and not args.all:
            continue
        cases.append((f"{src}|{question}", render(src, question, rows), question, paths, rows))

    scope = "every question" if args.all else "the ones the anchor gold calls a MISS"
    print(f"\nanchor gold: {misses} misses in {len(gold)} questions")
    print(f"judging {len(cases)} cases ({scope}), gold-blind...\n")

    seen: dict[str, list[str]] = {}
    cache = Path(args.cache)
    if cache.exists():
        seen = json.loads(cache.read_text())
    todo = [(k, body) for k, body, *_ in cases if k not in seen]
    if todo:
        batches = [todo[i : i + BATCH] for i in range(0, len(todo), BATCH)]
        with tempfile.TemporaryDirectory() as clean:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for got in pool.map(lambda b: judge(b, clean), batches):
                    seen |= {k: list(v) for k, v in got.items()}
                    print(f"  judged {len(seen)}/{len(cases)}", end="\r", flush=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(seen, indent=1))
    print(f"  judged {len(seen)}/{len(cases)}      ")

    counts = Counter(seen[k][0] for k, *_ in cases if k in seen)
    n = sum(counts.values()) or 1
    print(f"\nOf the {n} cases the anchor gold calls a MISS, a gold-blind judge says:")
    for v in ("SERVED", "PARTIAL", "FAILED"):
        print(f"  {v:8} {counts[v]:4d}   ({counts[v] / n:.0%})")
    print(
        f"\nTrue silent-failure rate: {counts['FAILED']}/{len(gold)} "
        f"= {counts['FAILED'] / len(gold):.1%} of questions."
    )
    print("The anchor gold called it "
          f"{misses / len(gold):.1%}. It overstates by {misses / max(counts['FAILED'], 1):.0f}x.")
    print("\nThe judge wobbles by a case or two between runs. Read these as ±, and\n"
          "only believe gaps much wider than that.")

    print("\n--- FAILED: what a caller actually walks away wrong about ---")
    for k, _, question, paths, rows in cases:
        if seen.get(k, [""])[0] == "FAILED":
            print(f"  {question!r}\n      why:  {seen[k][1]}")
            print(f"      top:  {rows[0]['path']}#{rows[0]['anchor']}")
            print(f"      gold: {paths[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
