from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import httpx
import zstandard

REPO = os.environ.get("ANYDOCS_REPO", "kiyeonjeon21/anydocs")
RELEASE_TAG = os.environ.get("ANYDOCS_RELEASE", "index-latest")
ARTIFACT_NAME = "anydocs-index.tar.zst"
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


def index_root() -> Path:
    """Directory holding anydocs.db plus the docs/ markdown tree."""
    global _root
    if _root is not None:
        return _root

    if override := os.environ.get("ANYDOCS_INDEX"):
        _root = Path(override).expanduser()
    elif local := _local_build():
        _root = local
    else:
        cached = CACHE / RELEASE_TAG
        if not (cached / DB_NAME).exists():
            _download(cached)
        _root = cached
    return _root


def ensure_index() -> Path:
    db = index_root() / DB_NAME
    if not db.exists():
        raise FileNotFoundError(f"no index at {db}")
    return db
