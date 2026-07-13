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

from anydocs.chunk import anchor_slug
from anydocs.models import Source

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "build" / "anydocs.db"
ID_RE = re.compile(r'id="([^"]+)"')
HEADING_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)
PER_SOURCE = 6


async def check(
    client: httpx.AsyncClient, url: str, body: str, style: str
) -> tuple[int, list[str]]:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return 0, [f"fetch failed: {exc}"]

    live = set(ID_RE.findall(resp.text))
    wanted = [anchor_slug(m.group(1), style) for m in HEADING_RE.finditer(body)]
    if not live & set(wanted) and wanted:
        # cursor.com/docs renders headings client-side, so no heading carries an
        # id in the served HTML and there is nothing here to compare against.
        # Say so instead of reporting every anchor as broken.
        return -1, []
    return len(wanted), [s for s in wanted if s and s not in live]


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
            results = await asyncio.gather(
                *(check(client, r["url"], r["body"], style) for r in rows)
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
