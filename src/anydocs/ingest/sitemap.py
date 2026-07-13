from __future__ import annotations

import re

import httpx

from anydocs.ingest.fetch import TIMEOUT, fetch_many, fetch_text
from anydocs.ingest.filters import allowed, clean_body, extract_description, extract_title
from anydocs.models import Page, Source, slug_path

LOC_RE = re.compile(r"<loc>\s*(?P<url>[^<\s]+)\s*</loc>")


async def ingest(source: Source) -> tuple[list[Page], list[str]]:
    """No llms.txt: take the page list from sitemap.xml, body from the .md twin.

    Sitemaps carry every locale (cursor lists 3347 URLs across 13 languages), so
    `include`/`exclude` in the source config do the language filtering.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        xml = await fetch_text(client, source.entry)

    # dict, not set: preserves sitemap order so builds are reproducible
    locs = {url: None for url in LOC_RE.findall(xml) if allowed(url, source)}
    md_urls = {loc + source.page_suffix: loc for loc in locs}

    fetched = await fetch_many(list(md_urls))
    pages, errors = [], []
    for md_url, page_url in md_urls.items():
        body = fetched[md_url]
        if isinstance(body, Exception):
            errors.append(f"{md_url}: {body}")
            continue
        body = clean_body(body)
        path = slug_path(page_url, source.base_url)
        pages.append(
            Page(
                source=source.id,
                path=path,
                url=page_url,
                title=extract_title(body, path),
                description=extract_description(body),
                body=body,
            )
        )
    return pages, errors
