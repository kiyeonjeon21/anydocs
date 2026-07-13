from __future__ import annotations

import re
from fnmatch import fnmatch

from anydocs.models import Source


def allowed(url: str, source: Source) -> bool:
    if source.include and not any(fnmatch(url, pat) for pat in source.include):
        return False
    return not any(fnmatch(url, pat) for pat in source.exclude)


TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)


def clean_body(body: str) -> str:
    """Drop the nav preamble some sites stamp onto every page.

    Every Claude Code page opens with the same blockquote pointing at llms.txt;
    left in, it is 165 identical copies of text that can surface in snippets.
    Only blockquote/blank lines ahead of the first H1 are dropped, so a page
    whose real content precedes its H1 is left alone.
    """
    lines = body.splitlines()
    first_h1 = next((i for i, ln in enumerate(lines) if TITLE_RE.match(ln)), None)
    if first_h1 is None:
        return body.strip()
    preamble = lines[:first_h1]
    if preamble and all(not ln.strip() or ln.lstrip().startswith(">") for ln in preamble):
        lines = lines[first_h1:]
    return "\n".join(lines).strip()


def extract_title(body: str, fallback: str) -> str:
    """First H1. Pages may open with a nav breadcrumb ("#### CLI") before it."""
    m = TITLE_RE.search(body)
    return m["title"].strip() if m else fallback


def extract_description(body: str) -> str:
    """First non-empty prose line after the H1, capped for the catalog listing."""
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if TITLE_RE.match(ln)), -1)
    for line in lines[start + 1 :]:
        line = line.strip()
        if line and not line.startswith(("#", "<", "|", "```", ">", "-", "*")):
            return line[:200]
    return ""
