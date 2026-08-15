"""Check generated anchors against the live sites' real `id=` attributes.

An anchor the agent can't click is a lie, and nothing else in the pipeline would
ever notice — search still ranks fine with a broken fragment. So sample real
pages per source and diff.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import httpx

from anydocs.chunk import anchor_slug, iter_headings
from anydocs.models import Source

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "build" / "anydocs.db"
ID_RE = re.compile(r'id="([^"]+)"')
PER_SOURCE = 6


async def check(
    client: httpx.AsyncClient, url: str, body: str, style: str, max_level: int
) -> tuple[int, list[str]]:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return 0, [f"fetch failed: {exc}"]

    live = set(ID_RE.findall(resp.text))
    # A heading inside a fenced ```md example is not a real anchor. Codex's
    # agents-md page shows a `## Code Review Rules` block by example, and a
    # fence-blind scan reported both it and its `### Experiment cohorts` child
    # as broken every day. iter_headings skips fences, exactly as the chunker
    # that built these anchors does, so the two never disagree.
    wanted = [anchor_slug(text, style) for _, text in iter_headings(body, 2, max_level)]
    if not live & set(wanted) and wanted:
        # cursor.com/docs renders headings client-side, so no heading carries an
        # id in the served HTML and there is nothing here to compare against.
        # Say so instead of reporting every anchor as broken.
        return -1, []
    return len(wanted), [s for s in wanted if s and s not in live]


# cursor.com never puts a live `id=` on an H3 -- confirmed across FAQ,
# steps, and plain content pages (account/regions, account/enterprise/*):
# the rendered page anchors H2 "section" headings only, H3s are members of
# UI components (accordions, step lists) with no anchor of their own. That
# is a real fact about the site, not a checker quirk, so the check is
# capped at H2 for this source rather than reporting a permanent false
# "broken" on every H3 sampled. Whether `read_doc` should still offer H3
# anchors for cursor (page loads, fragment just doesn't scroll) is a
# separate, open question -- this only stops the checker lying about it.
MAX_HEADING_LEVEL = {"cursor": 2}


async def main() -> int:
    styles = {s.id: s.slug_style for s in Source.load_all(ROOT / "sources")}
    conn = sqlite3.connect(f"file:{DB}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    by_source: dict[str, list] = defaultdict(list)
    for row in conn.execute("SELECT source, path, url, body FROM pages ORDER BY source, path"):
        if len(by_source[row["source"]]) < PER_SOURCE:
            by_source[row["source"]].append(row)

    bad = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        for source, rows in by_source.items():
            style = styles.get(source, "collapse")
            max_level = MAX_HEADING_LEVEL.get(source, 3)
            results = await asyncio.gather(
                *(check(client, r["url"], r["body"], style, max_level) for r in rows)
            )
            if all(n == -1 for n, _ in results):
                print(f"--  {source:<12} client-rendered HTML; anchors not verifiable")
                continue
            total = sum(n for n, _ in results if n > 0)
            missing = [(r["path"], m) for r, (_, m) in zip(rows, results, strict=True) if m]
            ok = total - sum(len(m) for _, m in missing)
            status = "OK " if not missing else "BAD"
            print(f"{status} {source:<12} {ok}/{total} anchors resolve on the live site")
            for path, miss in missing[:3]:
                print(f"      {path}: {miss[:4]}")
            bad += len(missing)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
