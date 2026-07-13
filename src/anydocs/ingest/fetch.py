from __future__ import annotations

import asyncio

import httpx

CONCURRENCY = 8
TIMEOUT = httpx.Timeout(30.0)


class SoftNotFound(Exception):
    """The server answered 200 but the body is not markdown.

    docs.cursor.com serves its Next.js 404 shell with HTTP 200 + text/html for
    every unknown path, so the status code alone cannot be trusted. Indexing
    those shells would silently poison the corpus with identical junk pages.
    """


def validate_markdown(resp: httpx.Response) -> str:
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype:
        raise SoftNotFound(f"{resp.url} returned {ctype}")
    text = resp.text
    if text.lstrip()[:200].lower().startswith(("<!doctype", "<html")):
        raise SoftNotFound(f"{resp.url} returned an HTML document body")
    if not text.strip():
        raise SoftNotFound(f"{resp.url} returned an empty body")
    return text


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return validate_markdown(resp)


async def fetch_many(urls: list[str]) -> dict[str, str | Exception]:
    """Fetch every URL concurrently. Failures are returned, not raised: one dead
    page must not sink the whole source."""
    sem = asyncio.Semaphore(CONCURRENCY)
    results: dict[str, str | Exception] = {}

    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT) as client:

        async def one(url: str) -> None:
            async with sem:
                try:
                    results[url] = await fetch_text(client, url)
                except Exception as exc:  # noqa: BLE001 - recorded per-URL
                    results[url] = exc

        await asyncio.gather(*(one(u) for u in urls))

    return results
