from __future__ import annotations

import re

import httpx

from anydocs.ingest.fetch import TIMEOUT, fetch_many, fetch_text
from anydocs.ingest.filters import allowed, clean_body
from anydocs.models import Page, Source, slug_path

# "- [Title](https://host/path.md): description"
ENTRY_RE = re.compile(r"^\s*[-*]\s*\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)\s*(?::\s*(?P<desc>.*))?$")


def page_fetch_url(url: str, source: Source) -> str:
    if not source.fetch_base_url:
        return url
    path = slug_path(url, source.base_url)
    return f"{source.fetch_base_url.rstrip('/')}/{path}.md"


async def ingest(source: Source) -> tuple[list[Page], list[str]]:
    """llms.txt used as an *index*: each line links to a page's markdown twin.

    The links may point at a different host than the llms.txt itself (codex
    serves its index from learn.chatgpt.com but every page from
    developers.openai.com), so links are followed as given.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        index = await fetch_text(client, source.entry)

    meta: dict[str, tuple[str, str, str]] = {}
    for line in index.splitlines():
        m = ENTRY_RE.match(line)
        if not m:
            continue
        url = m["url"]
        if not url.endswith(".md") or not allowed(url, source):
            continue
        fetch_url = page_fetch_url(url, source)
        meta[fetch_url] = (url, m["title"].strip(), (m["desc"] or "").strip())

    fetched = await fetch_many(list(meta))
    pages, errors = [], []
    for fetch_url, (url, title, desc) in meta.items():
        body = fetched[fetch_url]
        if isinstance(body, Exception):
            errors.append(
                f"{url} (fetched as {fetch_url}): {type(body).__name__}: {body}"
            )
            continue
        pages.append(
            Page(
                source=source.id,
                path=slug_path(url, source.base_url),
                url=url.removesuffix(".md"),
                title=title,
                description=desc,
                body=clean_body(body),
            )
        )
    return pages, errors
