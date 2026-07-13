from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

import httpx
import zstandard

REPO = os.environ.get("ANYDOCS_REPO", "kiyeonjeon21/anydocs")
RELEASE_TAG = os.environ.get("ANYDOCS_RELEASE", "index-latest")
ARTIFACT_NAME = "anydocs-index.tar.zst"

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


def _download(dest: Path) -> None:
    url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/{ARTIFACT_NAME}"
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        resp = client.get(url)
        resp.raise_for_status()

    raw = zstandard.ZstdDecompressor().decompress(resp.content, max_output_size=1 << 31)
    staging = dest.with_suffix(".partial")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        tar.extractall(staging, filter="data")

    # Swap in atomically: a half-extracted index must never be opened.
    shutil.rmtree(dest, ignore_errors=True)
    staging.rename(dest)


def _published_hash() -> str | None:
    """Content hash of the published index, from the release's manifest.

    manifest.json is ~400 bytes, so this is a cheap check to run once per server
    start. Returns None when offline — the cached index is then used as-is
    rather than failing, because stale docs beat no docs.
    """
    url = f"https://github.com/{REPO}/releases/download/{RELEASE_TAG}/manifest.json"
    try:
        with httpx.Client(follow_redirects=True, timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.json().get("content_hash")
    except Exception:  # noqa: BLE001 - offline, rate-limited, whatever: use the cache
        return None


def _cached_hash(root: Path) -> str | None:
    try:
        return json.loads((root / "manifest.json").read_text()).get("content_hash")
    except Exception:  # noqa: BLE001
        return None


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
        cached = CACHE / RELEASE_TAG
        if _needs_download(cached):
            _download(cached)
        _root = cached
    return _root


def _needs_download(cached: Path) -> bool:
    """Re-download when the docs actually changed.

    Checking only whether the file exists would pin a client to whatever the
    docs said on the day it was installed — the release tag is fixed, so the
    cache path never changes — and the daily sync would reach nobody.

    The upstream sync runs daily, so asking more than a few times a day buys
    nothing and costs ~0.5s of startup latency every session. Throttle it.
    """
    manifest = cached / "manifest.json"
    if not (cached / DB_NAME).exists() or not manifest.exists():
        return True
    forced = os.environ.get("ANYDOCS_REFRESH") == "1"
    if not forced and time.time() - manifest.stat().st_mtime < CHECK_INTERVAL:
        return False

    published = _published_hash()
    if published is None:  # offline: stale docs beat no docs
        return False
    if published == _cached_hash(cached):
        manifest.touch()  # checked and current; don't ask again for a while
        return False
    return True


def ensure_index() -> Path:
    db = index_root() / DB_NAME
    if not db.exists():
        raise FileNotFoundError(f"no index at {db}")
    return db
