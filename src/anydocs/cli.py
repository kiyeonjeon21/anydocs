from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sys
import tarfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import zstandard

from anydocs.index import SCHEMA_VERSION, build
from anydocs.ingest import ingest_source
from anydocs.models import Page, Source

ROOT = Path(__file__).resolve().parents[2]
DB_NAME = "anydocs.db"
ARTIFACT_NAME = "anydocs-index.tar.zst"


def versioned_artifact_name(content_hash: str) -> str:
    return f"anydocs-index-{content_hash}.tar.zst"


async def run_build(sources_dir: Path, out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    synced_at = datetime.now(UTC).isoformat(timespec="seconds")
    ingested: list[tuple[Source, list[Page]]] = []
    errors: list[str] = []
    warnings: list[str] = []
    configured = Source.load_all(sources_dir)
    if not configured:
        errors.append(f"no source configurations found in {sources_dir}")

    for source in configured:
        try:
            pages, page_errors = await ingest_source(source)
        except Exception as exc:  # noqa: BLE001
            # Keep building the other sources for diagnostics, but never publish
            # a partial index. The last healthy release remains available.
            errors.append(f"{source.id}: {type(exc).__name__}: {exc}")
            print(f"  {source.id:<12} FAILED: {exc}", file=sys.stderr)
            continue

        if not pages:
            errors.append(f"{source.id}: ingested zero pages")

        # Too few means the site moved; too many means a filter stopped matching.
        # Both are silent otherwise: opencode's 17 locales slipped past a glob
        # that looked right, and quietly multiplied the source by 17.
        if source.expect_pages and not (
            source.expect_pages * 0.8 <= len(pages) <= source.expect_pages * 1.25
        ):
            errors.append(
                f"{source.id}: got {len(pages)} pages, expected ~{source.expect_pages}"
            )
        for err in page_errors:
            errors.append(f"{source.id}: skipped page: {err}")
            print(f"  {source.id:<12} skip: {err.splitlines()[0]}", file=sys.stderr)

        ingested.append((source, pages))
        print(f"  {source.id:<12} {len(pages):>4} pages")

    stats = build(out / DB_NAME, ingested, synced_at)

    # A source whose cross-references all fail to resolve still indexes, searches
    # and looks entirely healthy — Codex's did exactly that, because its bodies
    # link to a second host and nothing checked. Say so.
    for sid, stat in stats.items():
        if not stat["links"]:
            errors.append(
                f"{sid}: no internal links resolved — its cross-reference graph is "
                f"empty. If the site links to itself under another host, add it to "
                f"link_bases in sources/{sid}.yaml."
            )

    missing = sorted({s.id for s in configured} - set(stats))
    if missing:
        errors.append(f"configured sources missing from index: {', '.join(missing)}")

    digest = content_hash(ingested)
    artifact_name = versioned_artifact_name(digest)
    manifest = {
        "synced_at": synced_at,
        # Hash of the docs themselves, not of the artifact: the .tar.zst differs
        # on every run because synced_at is baked in, so only this can answer
        # "did the documentation actually change today?"
        "content_hash": digest,
        "artifact_name": artifact_name,
        "healthy": not errors,
        "sources": stats,
        "warnings": warnings,
        "errors": errors,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    pack(out, artifact_name)
    total = sum(s["chunks"] for s in stats.values())
    size = (out / artifact_name).stat().st_size
    print(f"\n{total} chunks -> {artifact_name} ({size / 1e6:.1f} MB)")
    for warn in warnings:
        print(f"warning: {warn}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1 if errors else 0


# The modules that decide how pages become an index. Hashing them means a change
# to chunking, titles or slugs republishes — hashing only the page bodies did
# not, so a fix to how the docs are indexed could sit undelivered forever while
# CI cheerfully reported "documentation unchanged".
INDEXER_MODULES = ["cli.py", "chunk.py", "index.py", "links.py", "models.py"]


def indexer_paths() -> list[Path]:
    src = Path(__file__).parent
    return sorted(
        [*(src / name for name in INDEXER_MODULES), *(src / "ingest").rglob("*.py")]
    )


def _hash_field(digest, value: str) -> None:
    data = value.encode()
    digest.update(len(data).to_bytes(8, "big"))
    digest.update(data)


def content_hash(ingested: list[tuple[Source, list[Page]]]) -> str:
    digest = hashlib.sha256()
    _hash_field(digest, SCHEMA_VERSION)
    for path in indexer_paths():
        _hash_field(digest, path.relative_to(Path(__file__).parent).as_posix())
        digest.update(path.read_bytes())
    for source, pages in sorted(ingested, key=lambda item: item[0].id):
        _hash_field(
            digest,
            json.dumps(asdict(source), sort_keys=True, separators=(",", ":")),
        )
        for page in sorted(pages, key=lambda p: (p.source, p.path)):
            for value in (
                page.source,
                page.path,
                page.url,
                page.title,
                page.description,
                page.body,
            ):
                _hash_field(digest, value)
    return digest.hexdigest()


def pack(out: Path, artifact_name: str) -> None:
    """Ship the DB alone. Page bodies live in `pages.body`, so read_doc and
    grep_docs both work straight off it — a markdown tree next to it would just
    be a second copy of the same text, free to drift."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(out / DB_NAME, arcname=DB_NAME)
        tar.add(out / "manifest.json", arcname="manifest.json")
    data = zstandard.ZstdCompressor(level=10).compress(buf.getvalue())
    (out / artifact_name).write_bytes(data)
    # Compatibility for clients that predate manifest.artifact_name.
    (out / ARTIFACT_NAME).write_bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(prog="anydocs-build")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources")
    parser.add_argument("--out", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    return asyncio.run(run_build(args.sources, args.out))


if __name__ == "__main__":
    sys.exit(main())
