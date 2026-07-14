from __future__ import annotations

import asyncio
import json
import os
import sqlite3

import pytest

from anydocs import artifact, cli
from anydocs.index import build
from anydocs.models import Page, Source


def _source(**changes) -> Source:
    values = {
        "id": "codex",
        "title": "OpenAI Codex",
        "strategy": "llms-txt",
        "entry": "https://example.com/llms.txt",
        "base_url": "https://example.com/docs/",
        "expect_pages": 2,
    }
    values.update(changes)
    return Source(**values)


def _pages(description="First page", url="https://example.com/docs/one") -> list[Page]:
    return [
        Page(
            "codex",
            "one",
            url,
            "One",
            description,
            "# One\n\n" + "intro text " * 10 + "\n\n[Two](https://example.com/docs/two)",
        ),
        Page(
            "codex",
            "two",
            "https://example.com/docs/two",
            "Two",
            "Second page",
            "# Two\n\n" + "reference text " * 10,
        ),
    ]


def _valid_root(root, content_hash="abc"):
    root.mkdir(parents=True, exist_ok=True)
    build(root / "anydocs.db", [(_source(), _pages())], "now")
    manifest = {
        "synced_at": "now",
        "content_hash": content_hash,
        "artifact_name": f"anydocs-index-{content_hash}.tar.zst",
        "healthy": True,
        "sources": {"codex": {"pages": 2, "chunks": 2, "links": 1}},
        "warnings": [],
        "errors": [],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_content_hash_covers_source_and_every_page_field():
    source = _source()
    original = cli.content_hash([(source, _pages())])

    assert original != cli.content_hash([(_source(slug_style="verbatim"), _pages())])
    assert original != cli.content_hash([(source, _pages(description="Changed"))])
    assert original != cli.content_hash(
        [(source, _pages(url="https://other.example/docs/one"))]
    )


def test_build_writes_healthy_versioned_and_legacy_artifacts(tmp_path, monkeypatch):
    source = _source()
    monkeypatch.setattr(cli.Source, "load_all", classmethod(lambda cls, path: [source]))

    async def ingest(_):
        return _pages(), []

    monkeypatch.setattr(cli, "ingest_source", ingest)
    out = tmp_path / "out"
    assert asyncio.run(cli.run_build(tmp_path, out)) == 0

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["healthy"] is True
    assert manifest["errors"] == []
    assert (out / manifest["artifact_name"]).read_bytes() == (
        out / cli.ARTIFACT_NAME
    ).read_bytes()


def test_empty_sources_directory_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Source, "load_all", classmethod(lambda cls, path: []))
    out = tmp_path / "out"

    assert asyncio.run(cli.run_build(tmp_path, out)) == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["healthy"] is False
    assert manifest["sources"] == {}
    assert any("no source configurations" in error for error in manifest["errors"])


@pytest.mark.parametrize(
    ("pages", "page_errors", "message"),
    [
        ([], [], "ingested zero pages"),
        (_pages(), ["one.md: HTTP 500"], "skipped page"),
        (_pages()[:1], [], "expected ~2"),
    ],
)
def test_unhealthy_ingest_is_diagnostic_but_nonzero(
    tmp_path, monkeypatch, pages, page_errors, message
):
    source = _source()
    monkeypatch.setattr(cli.Source, "load_all", classmethod(lambda cls, path: [source]))

    async def ingest(_):
        return pages, page_errors

    monkeypatch.setattr(cli, "ingest_source", ingest)
    out = tmp_path / "out"
    assert asyncio.run(cli.run_build(tmp_path, out)) == 1
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["healthy"] is False
    assert any(message in error for error in manifest["errors"])


def test_validate_index_checks_health_schema_and_contents(tmp_path):
    root = tmp_path / "valid"
    _valid_root(root)
    assert artifact.validate_index(root)["content_hash"] == "abc"

    conn = sqlite3.connect(root / "anydocs.db")
    conn.execute("UPDATE meta SET value='old' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="schema mismatch"):
        artifact.validate_index(root)


def test_download_uses_versioned_name_and_validates_before_swap(tmp_path, monkeypatch):
    source = tmp_path / "source"
    manifest = _valid_root(source)
    cli.pack(source, manifest["artifact_name"])
    payload = (source / manifest["artifact_name"]).read_bytes()
    seen = []

    class Response:
        content = payload

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            seen.append(url)
            return Response()

    monkeypatch.setattr(artifact.httpx, "Client", Client)
    dest = tmp_path / "cache" / "tag"
    dest.parent.mkdir()
    artifact._download(dest, manifest)

    assert seen[0].endswith(manifest["artifact_name"])
    assert artifact.validate_index(dest)["content_hash"] == "abc"


def test_refresh_failure_uses_valid_stale_cache(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "cache"
    root = cache / "tag"
    _valid_root(root)
    old = 1_000_000_000
    os.utime(root / "manifest.json", (old, old))

    monkeypatch.setattr(artifact, "CACHE", cache)
    monkeypatch.setattr(artifact, "RELEASE_TAG", "tag")
    monkeypatch.setattr(artifact, "_root", None)
    monkeypatch.setattr(artifact, "_local_build", lambda: None)
    monkeypatch.setattr(
        artifact, "_published_manifest", lambda: (_ for _ in ()).throw(OSError("offline"))
    )

    assert artifact.index_root() == root
    assert "using the last valid cache" in capsys.readouterr().err


def test_refresh_failure_without_cache_is_loud(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(artifact, "RELEASE_TAG", "tag")
    monkeypatch.setattr(artifact, "_root", None)
    monkeypatch.setattr(artifact, "_local_build", lambda: None)
    monkeypatch.setattr(
        artifact, "_published_manifest", lambda: (_ for _ in ()).throw(OSError("offline"))
    )

    with pytest.raises(RuntimeError, match="cannot obtain a valid anydocs index"):
        artifact.index_root()
