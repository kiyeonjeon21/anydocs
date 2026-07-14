"""Regression gate for the production search path.

Three rulers, and each measures one thing:

- **hand** — 15 realistic questions. Precision. A one-case swing is noise.
- **auto** — each page's llms.txt description as the query. Broad ranking movement.
  It is fair *only* because `description` is not an indexed FTS column, and it
  measures ranking *only*: its queries are long distinctive sentences, nothing like
  the three-to-six keywords a caller types, so any statistic that reads the shape
  of the query or of the score curve looks superb here and collapses in use.
- **anchor** — the anchor text of the docs' own links (`[hook events list](/en/hooks)`),
  a human writing the query he would use to ask for that page. Recall.

Gold paths are always matched exactly.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from anydocs.artifact import ensure_index
from anydocs.chunk import ANY_HEADING_RE, FENCE_RE
from anydocs.index import connect
from anydocs.links import SKIP_SCHEMES, _candidates, site_base
from anydocs.models import Page, Source
from anydocs.query import search

HAND_MIN_AT1 = 10
HAND_MIN_AT3 = 12
# Baseline on the complete 2026-07-14 corpus is 0.792 / 0.847. A 0.01 margin
# ignores a couple of moving-doc cases but still catches broad ranking damage.
AUTO_MIN_AT1 = 0.782
AUTO_MIN_MRR = 0.837
# Baseline 0.897 on the same corpus. Recall is what search_docs' eight rows are
# for, and it is the number that moves when a slot is wasted: page-level dedup
# lifted this from 0.859 by spending the duplicate rows on new pages instead.
ANCHOR_MIN_RECALL8 = 0.880


@dataclass
class Case:
    query: str
    source: str
    gold: tuple[str, ...]


GOLD = [
    Case("hook events list", "claude-code", ("en/hooks",)),
    Case("what hook events exist", "claude-code", ("en/hooks",)),
    Case("settings file precedence order", "claude-code", ("en/settings",)),
    Case("how do I add a hook", "claude-code", ("en/hooks-guide", "en/hooks")),
    Case("restrict which tools a subagent can use", "claude-code", ("en/sub-agents",)),
    Case("MCP server scopes local project user", "claude-code", ("en/mcp",)),
    Case("--dangerously-skip-permissions", "claude-code", ("en/cli-reference",)),
    Case("output styles", "claude-code", ("en/output-styles",)),
    Case("slash command frontmatter", "claude-code", ("en/commands",)),
    Case(
        "plugin marketplace",
        "claude-code",
        ("en/plugin-marketplaces", "en/discover-plugins"),
    ),
    Case(
        "sandbox modes approval policy",
        "codex",
        ("sandboxing", "config-file/config-advanced"),
    ),
    Case("AGENTS.md nesting precedence", "codex", ("agent-configuration/agents-md",)),
    Case(
        "config.toml model provider",
        "codex",
        ("config-file/config-basic", "config-file/config-advanced"),
    ),
    Case(
        "codex cloud environment setup",
        "codex",
        ("cloud", "environments/cloud-environment"),
    ),
    Case("custom prompts slash commands", "codex", ("custom-prompts",)),
]


def evaluate_hand(conn) -> tuple[int, int, list[str]]:
    at1 = at3 = 0
    misses = []
    for case in GOLD:
        rows, _ = search(conn, case.query, sources=[case.source], limit=3)
        paths = [r["path"] for r in rows]
        at1 += bool(paths and paths[0] in case.gold)
        at3 += bool(set(paths) & set(case.gold))
        if not set(paths) & set(case.gold):
            misses.append(f"{case.query!r} -> {paths[0] if paths else '(none)'}")
    return at1, at3, misses


def evaluate_auto(conn) -> tuple[int, int, float, float]:
    """Description is the query; its exact page is gold.

    Descriptions for Claude Code and Codex come from llms.txt and are not indexed
    FTS columns, so they remain paraphrases rather than answer leakage.
    """
    gold = conn.execute(
        "SELECT source,path,description FROM pages"
        " WHERE source IN ('claude-code','codex') AND length(description) > 40"
    ).fetchall()
    at1 = 0
    rr = 0.0
    for item in gold:
        rows, _ = search(
            conn, item["description"], sources=[item["source"]], limit=5
        )
        paths = [r["path"] for r in rows]
        at1 += bool(paths and paths[0] == item["path"])
        for rank, path in enumerate(paths, 1):
            if path == item["path"]:
                rr += 1 / rank
                break
    total = len(gold)
    return at1, total, at1 / total if total else 0.0, rr / total if total else 0.0


# Anchor text AND href — links.py resolves the href and throws the text away.
MD_LINK_TEXT = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)(?:\s+[\"'][^)]*)?\)")
WORD = re.compile(r"[0-9A-Za-z][0-9A-Za-z._/-]*")
# Navigational filler. "See also" is how you get to a page, not what you call it.
GENERIC = re.compile(
    r"^(here|this|that|link|docs?|documentation|guide|reference|read more|learn more|"
    r"see (also|here|more)|more|click here|back|next|previous|overview|home|page|"
    r"this page|the docs?|view|details|example|examples)$",
    re.I,
)


def anchor_gold(conn, sources_dir: Path) -> list[tuple[str, str, str]]:
    """(source, query, gold_path) from the anchor text of every internal link.

    The docs' authors have already written thousands of queries for us. When one
    page links to another it names it in *referring* vocabulary — the words you
    would use to ask for that page, not the words the page uses about itself —
    and that text is indexed nowhere: not in `chunks_fts`, not in `description`.
    It is the only text in this corpus that can judge retrieval without leaking.

    Two things it cannot do, and they are why this is scored on recall@8 alone:
    the anchor phrase sits verbatim in the *linking* page's body, which IS
    indexed, so the exact match is on the wrong page; and the label is noisy —
    'tracking costs and usage' is graded wrong for answering `en/costs`, because
    that one link happened to point at `agent-sdk/cost-tracking`. Both wreck
    hit@1. Neither touches "was the right page in the eight rows at all", which
    is the question search_docs is actually answering.
    """
    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for source in Source.load_all(sources_dir):
        pages = [
            Page(source.id, r["path"], r["url"], "", "", r["body"])
            for r in conn.execute(
                "SELECT path, url, body FROM pages WHERE source=?", (source.id,)
            )
        ]
        if not pages:
            continue
        bases = [b for b in (site_base(pages), *source.link_bases) if b]
        known = {p.path for p in pages}

        for page in pages:
            in_fence = False
            for line in page.body.splitlines():
                if FENCE_RE.match(line):
                    in_fence = not in_fence
                    continue
                if in_fence or ANY_HEADING_RE.match(line):
                    continue
                for m in MD_LINK_TEXT.finditer(line):
                    text, href = m[1].strip(), m[2].split("#")[0].split("?")[0].strip()
                    if not href or href.startswith(SKIP_SCHEMES):
                        continue
                    if GENERIC.match(text) or len(WORD.findall(text)) < 2:
                        continue
                    if text.startswith("!") or "http" in text:  # an image, or a raw URL
                        continue
                    for path in _candidates(href, page.url, bases):
                        if path in known and path != page.path:
                            pairs[(source.id, text.lower())].add(path)
                            break

    # One phrase used for two pages is not a label.
    return [(s, t, next(iter(v))) for (s, t), v in pairs.items() if len(v) == 1]


def evaluate_anchor(conn, gold) -> tuple[int, int, float]:
    hits = 0
    for source, query, path in gold:
        rows, _ = search(conn, query, sources=[source], limit=8)
        hits += path in {r["path"] for r in rows}
    return hits, len(gold), hits / len(gold) if gold else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--sources", type=Path, default=Path(__file__).resolve().parents[1] / "sources"
    )
    args = parser.parse_args()

    conn = connect(ensure_index(), read_only=True)
    hand_at1, hand_at3, misses = evaluate_hand(conn)
    auto_at1, auto_total, auto_rate, auto_mrr = evaluate_auto(conn)
    anc_hits, anc_total, anc_rate = evaluate_anchor(conn, anchor_gold(conn, args.sources))

    print(f"hand    hit@1 {hand_at1}/{len(GOLD)}  hit@3 {hand_at3}/{len(GOLD)}")
    print(f"auto    hit@1 {auto_at1}/{auto_total} ({auto_rate:.3f})  MRR {auto_mrr:.3f}")
    print(f"anchor  recall@8 {anc_hits}/{anc_total} ({anc_rate:.3f})")
    if args.verbose and misses:
        print("\nhand-set misses:")
        for miss in misses:
            print(f"  {miss}")

    failures = []
    if hand_at1 < HAND_MIN_AT1:
        failures.append(f"hand hit@1 {hand_at1} < {HAND_MIN_AT1}")
    if hand_at3 < HAND_MIN_AT3:
        failures.append(f"hand hit@3 {hand_at3} < {HAND_MIN_AT3}")
    if auto_rate < AUTO_MIN_AT1:
        failures.append(f"auto hit@1 {auto_rate:.3f} < {AUTO_MIN_AT1:.3f}")
    if auto_mrr < AUTO_MIN_MRR:
        failures.append(f"auto MRR {auto_mrr:.3f} < {AUTO_MIN_MRR:.3f}")
    if anc_rate < ANCHOR_MIN_RECALL8:
        failures.append(f"anchor recall@8 {anc_rate:.3f} < {ANCHOR_MIN_RECALL8:.3f}")
    for failure in failures:
        print(f"REGRESSION: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
