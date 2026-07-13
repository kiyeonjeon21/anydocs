from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from anydocs.index import build
from anydocs.ingest import ingest_source
from anydocs.models import Page, Source

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "anydocs.db"
ARTIFACT_NAME = "anydocs-index.tar.zst"


async def run_build(sources_dir: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    synced_at = datetime.now(UTC).isoformat(timespec="seconds")
    ingested: list[tuple[Source, list[Page]]] = []
    failures: list[str] = []

    for source in Source.load_all(sources_dir):
        try:
            pages, errors = await ingest_source(source)
        except Exception as exc:  # noqa: BLE001
            # One site's redesign must not sink the whole index. Keep the other
            # sources and let CI surface this as a warning.
            failures.append(f"{source.id}: {type(exc).__name__}: {exc}")
            print(f"  {source.id:<12} FAILED: {exc}", file=sys.stderr)
            continue

        if source.expect_pages and len(pages) < source.expect_pages * 0.8:
            failures.append(
                f"{source.id}: got {len(pages)} pages, expected ~{source.expect_pages}"
            )
        for err in errors:
            print(f"  {source.id:<12} skip: {err.splitlines()[0]}", file=sys.stderr)

        ingested.append((source, pages))
        print(f"  {source.id:<12} {len(pages):>4} pages")

    if not ingested:
        print("no sources ingested", file=sys.stderr)
        return 1

    stats = build(out / DB_NAME, ingested, synced_at)
    manifest = {
        "synced_at": synced_at,
        # Hash of the docs themselves, not of the artifact: the .tar.zst differs
        # on every run because synced_at is baked in, so only this can answer
        # "did the documentation actually change today?"
        "content_hash": content_hash(ingested),
        "sources": stats,
        "warnings": failures,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    pack(out)
    total = sum(s["chunks"] for s in stats.values())
    size = (out / ARTIFACT_NAME).stat().st_size
    print(f"\n{total} chunks -> {ARTIFACT_NAME} ({size / 1e6:.1f} MB)")
    for warn in failures:
        print(f"warning: {warn}", file=sys.stderr)
    return 0


def content_hash(ingested: list[tuple[Source, list[Page]]]) -> str:
    digest = hashlib.sha256()
    for _, pages in ingested:
        for page in sorted(pages, key=lambda p: (p.source, p.path)):
            digest.update(f"{page.source}\0{page.path}\0".encode())
            digest.update(page.body.encode())
    return digest.hexdigest()


def pack(out: Path) -> None:
    """Ship the DB alone. Page bodies live in `pages.body`, so read_doc and
    grep_docs both work straight off it — a markdown tree next to it would just
    be a second copy of the same text, free to drift."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(out / DB_NAME, arcname=DB_NAME)
        tar.add(out / "manifest.json", arcname="manifest.json")
    data = zstandard.ZstdCompressor(level=10).compress(buf.getvalue())
    (out / ARTIFACT_NAME).write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(prog="anydocs-build")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources")
    parser.add_argument("--out", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    return asyncio.run(run_build(args.sources, args.out))


if __name__ == "__main__":
    sys.exit(main())
