from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import zstandard
from filelock import FileLock

from anydocs.index import SCHEMA_VERSION

REPO = os.environ.get("ANYDOCS_REPO", "kiyeonjeon21/anydocs")
RELEASE_TAG = os.environ.get("ANYDOCS_RELEASE", "index-latest")
ARTIFACT_NAME = "anydocs-index.tar.zst"
LOCK_TIMEOUT = 180

# How stale the cache may get before we ask GitHub again. The check is a ~400
# byte GET (~150 ms) against a server that takes longer than that to boot, so
# this exists only to stop rapid restarts hammering it. It was 6 h, which meant a
# fixed index could sit undelivered for most of a working day while the tool
# looked broken. ANYDOCS_REFRESH=1 skips the throttle outright.
CHECK_INTERVAL = 3600
DB_NAME = "anydocs.db"

CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "anydocs"
_root: Path | None = None


def _local_build() -> Path | None:
    """A build/ directory next to the source tree wins, so `anydocs-build &&
    anydocs` works offline while developing."""
    candidate = Path(__file__).resolve().parents[2] / "build"
    return candidate if (candidate / DB_NAME).exists() else None


def _download(dest: Path, published_manifest: dict) -> None:
    artifact_name = published_manifest.get("artifact_name") or ARTIFACT_NAME
    url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{artifact_name}"
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        resp = client.get(url)
        resp.raise_for_status()

    raw = zstandard.ZstdDecompressor().decompress(resp.content, max_output_size=1 << 31)
    staging = Path(tempfile.mkdtemp(prefix=f".{RELEASE_TAG}-", dir=dest.parent))
    backup = dest.with_name(f"{dest.name}.previous")
    try:
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            tar.extractall(staging, filter="data")
        extracted = validate_index(staging)
        if extracted["content_hash"] != published_manifest.get("content_hash"):
            raise ValueError(
                "downloaded artifact content_hash does not match the published manifest"
            )

        shutil.rmtree(backup, ignore_errors=True)
        if dest.exists():
            dest.rename(backup)
        try:
            staging.rename(dest)
        except Exception:
            if backup.exists() and not dest.exists():
                backup.rename(dest)
            raise
        shutil.rmtree(backup, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _published_manifest() -> dict:
    """Fetch the small release manifest used to select a versioned artifact."""
    url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/manifest.json"
    with httpx.Client(follow_redirects=True, timeout=5.0) as client:
        resp = client.get(url)
        resp.raise_for_status()
        manifest = resp.json()
    if not isinstance(manifest, dict) or not manifest.get("content_hash"):
        raise ValueError(f"published manifest at {url} has no content_hash")
    return manifest


def validate_index(root: Path) -> dict:
    """Validate a cache/build before exposing it to MCP tools."""
    manifest_path = root / "manifest.json"
    db_path = root / DB_NAME
    if not manifest_path.exists() or not db_path.exists():
        raise FileNotFoundError(f"index at {root} is missing {DB_NAME} or manifest.json")
    manifest = json.loads(manifest_path.read_text())
    if not isinstance(manifest, dict) or not manifest.get("content_hash"):
        raise ValueError(f"index manifest at {manifest_path} has no content_hash")
    if manifest.get("healthy", True) is not True:
        errors = "; ".join(manifest.get("errors") or [])
        raise ValueError(f"index at {root} is unhealthy" + (f": {errors}" if errors else ""))

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if not quick or quick[0] != "ok":
            raise ValueError(f"SQLite quick_check failed for {db_path}: {quick}")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not row or row[0] != SCHEMA_VERSION:
            found = row[0] if row else "missing"
            raise ValueError(
                f"index schema mismatch at {db_path}: have {found}, need {SCHEMA_VERSION}"
            )
        required = {"sources", "pages", "chunks", "chunks_fts", "links"}
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        if missing := sorted(required - tables):
            raise ValueError(f"index at {db_path} is missing tables: {', '.join(missing)}")
        for table in ("sources", "pages", "chunks"):
            count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if count == 0:
                raise ValueError(f"index at {db_path} has no {table}")
    finally:
        conn.close()
    return manifest


def _validated(root: Path) -> tuple[dict | None, Exception | None]:
    try:
        return validate_index(root), None
    except Exception as exc:  # noqa: BLE001 - returned with context to the caller
        return None, exc


def _fresh(root: Path) -> bool:
    manifest = root / "manifest.json"
    return manifest.exists() and time.time() - manifest.stat().st_mtime < CHECK_INTERVAL


def _warn_stale(exc: Exception) -> None:
    print(f"anydocs: index refresh failed; using the last valid cache: {exc}", file=sys.stderr)


def index_root() -> Path:
    """Directory holding anydocs.db."""
    global _root
    if _root is not None:
        return _root

    if override := os.environ.get("ANYDOCS_INDEX"):
        _root = Path(override).expanduser()
    elif local := _local_build():
        _root = local
    else:
        CACHE.mkdir(parents=True, exist_ok=True)
        cached = CACHE / RELEASE_TAG
        lock = FileLock(str(CACHE / f"{RELEASE_TAG}.lock"), timeout=LOCK_TIMEOUT)
        with lock:
            cached_manifest, cached_error = _validated(cached)
            forced = os.environ.get("ANYDOCS_REFRESH") == "1"
            if cached_manifest is not None and not forced and _fresh(cached):
                _root = cached
                return _root

            try:
                published = _published_manifest()
                if (
                    cached_manifest is not None
                    and published["content_hash"] == cached_manifest["content_hash"]
                ):
                    (cached / "manifest.json").touch()
                else:
                    _download(cached, published)
            except Exception as exc:  # noqa: BLE001 - stale valid data is the fallback
                if cached_manifest is None:
                    detail = f"; cached index is invalid: {cached_error}" if cached_error else ""
                    raise RuntimeError(f"cannot obtain a valid anydocs index: {exc}{detail}") from exc
                _warn_stale(exc)
                (cached / "manifest.json").touch()
        _root = cached
    return _root


def ensure_index() -> Path:
    root = index_root()
    validate_index(root)
    db = root / DB_NAME
    return db
