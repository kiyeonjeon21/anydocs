from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    strategy: str
    entry: str
    tags: list[str] = field(default_factory=list)
    base_url: str = ""
    page_suffix: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    delimiter: str = ""
    url_template: str = ""
    expect_pages: int | None = None
    # How the live site slugs its heading anchors. See chunk.anchor_slug — the
    # sites genuinely differ, and a wrong slug is a link that silently misses.
    slug_style: str = "collapse"

    @classmethod
    def load_all(cls, directory: Path) -> list[Source]:
        return [cls(**yaml.safe_load(p.read_text())) for p in sorted(directory.glob("*.yaml"))]


@dataclass
class Page:
    source: str
    path: str  # source-relative, no extension, e.g. "en/hooks"
    url: str
    title: str
    description: str
    body: str


def slug_path(url: str, base_url: str) -> str:
    """URL -> source-relative path. Strips the base prefix and any .md suffix.

    The docs root itself (`opencode.ai/docs/`) reduces to the empty string, which
    search happily returned as `opencode/` — a path read_doc then refused. An
    8 KB intro page was visible and unreadable.
    """
    path = url.removeprefix(base_url) if base_url and url.startswith(base_url) else url
    path = re.sub(r"^https?://[^/]+/", "", path)
    return path.removesuffix(".md").strip("/") or "index"
