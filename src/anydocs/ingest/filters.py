from __future__ import annotations

import re
from fnmatch import fnmatch

from anydocs.chunk import iter_headings
from anydocs.models import Source


def allowed(url: str, source: Source) -> bool:
    if source.include and not any(fnmatch(url, pat) for pat in source.include):
        return False
    return not any(fnmatch(url, pat) for pat in source.exclude)


TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)


def prettify_path(path: str) -> str:
    """`agent/tools/web-search` -> `Web Search`. A last resort when a page has no
    H1 at all — 70 pages across cursor and opencode — and the raw path was being
    shown as the title."""
    return path.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()


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
    """First real H1 — not one inside a fenced block, where `#` is a comment.

    opencode's pages mostly have no H1, so the first `# ...` line in the file was
    a bash comment in an example: `troubleshooting` was titled `or`, `rules` was
    titled `SST v3 Monorepo Project`. Titles carry 10x weight in the ranking.
    """
    title = next((t for lvl, t in iter_headings(body, 1, 1)), None)
    return title or prettify_path(fallback)


def extract_description(body: str) -> str:
    """First non-empty prose line after the H1, capped for the catalog listing."""
    lines = body.splitlines()
    start = next((i for i, ln in enumerate(lines) if TITLE_RE.match(ln)), -1)
    for line in lines[start + 1 :]:
        line = line.strip()
        if line and not line.startswith(("#", "<", "|", "```", ">", "-", "*")):
            return line[:200]
    return ""
