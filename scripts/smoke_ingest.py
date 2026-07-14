"""Ingest every source and report page counts, so a site redesign is loud."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from anydocs.ingest import ingest_source
from anydocs.models import Source

ROOT = Path(__file__).resolve().parent.parent


async def main() -> int:
    failed = False
    for source in Source.load_all(ROOT / "sources"):
        try:
            pages, errors = await ingest_source(source)
        except Exception as exc:  # noqa: BLE001 - one dead site must not sink the rest
            print(f"{source.id:<12} FAILED  {type(exc).__name__}: {exc}")
            failed = True
            continue

        total = sum(len(p.body) for p in pages)
        html = [p.path for p in pages if p.body.lstrip().lower().startswith(("<!doctype", "<html"))]
        print(
            f"{source.id:<12} {len(pages):>4} pages  {total / 1e6:>5.2f} MB"
            f"  errors={len(errors):<3} html_leaks={len(html)}"
        )
        for err in errors[:3]:
            print(f"  ! {err}")
        if errors:
            failed = True
        if not pages:
            print("  !! source returned zero pages")
            failed = True
        if source.expect_pages and not (
            source.expect_pages * 0.8 <= len(pages) <= source.expect_pages * 1.25
        ):
            print(f"  !! expected about {source.expect_pages} pages")
            failed = True
        if html:
            print(f"  !! HTML leaked into corpus: {html[:3]}")
            failed = True
        if pages:
            sample = pages[0]
            print(f"  e.g. {sample.path}  |  {sample.title}  |  {sample.url}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
