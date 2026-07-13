from __future__ import annotations

import re

import httpx

from anydocs.ingest.fetch import TIMEOUT, fetch_text
from anydocs.ingest.filters import clean_body, extract_description, extract_title
from anydocs.models import Page, Source


async def ingest(source: Source) -> tuple[list[Page], list[str]]:
    """llms.txt used as the *corpus*: one file holding every page, split by a
    delimiter line (xai: "===/build/cli/reference==="). There are no .md twins
    to fetch — the file we already have is the body."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:
        blob = await fetch_text(client, source.entry)

    delim = re.compile(source.delimiter, re.MULTILINE)
    marks = list(delim.finditer(blob))
    if not marks:
        return [], [f"{source.entry}: delimiter {source.delimiter!r} matched nothing"]

    pages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(blob)
        body = clean_body(blob[m.end() : end])
        if not body:
            continue
        raw_path = m["path"]
        path = raw_path.strip("/")
        pages.append(
            Page(
                source=source.id,
                path=path,
                url=source.url_template.format(path=raw_path),
                title=extract_title(body, path),
                description=extract_description(body),
                body=body,
            )
        )
    return pages, []
