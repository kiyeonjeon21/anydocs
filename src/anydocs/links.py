from __future__ import annotations

import re
from urllib.parse import urljoin

from anydocs.chunk import ANY_HEADING_RE, FENCE_RE
from anydocs.models import Page, Source, slug_path

# The cross-reference graph the docs authors already wrote by hand.
#
# Retrieval was never the weak link. Asked how to lock four coding agents down,
# a model searched permissions four times, never thought to ask about sandboxes,
# and concluded that only Codex had one — while `claude-code/en/sandboxing` sat
# in the index, and while the page it *did* read carried a "See also" pointing
# straight at it. What failed was exploration, not search, and no amount of
# embedding fixes a question that was never asked.
#
# But the authors had already answered it: that "See also" is a relatedness edge,
# curated by a human who knows the corpus. Every source ships thousands of them.
# Surfacing them costs nothing and beats approximating the same relation.

# `[text](href)` — the optional title (`[x](/y "Title")`) would otherwise glue
# itself to the href.
MD_LINK = re.compile(r"\[[^\]]*\]\(\s*([^)\s]+)(?:\s+[\"'][^)]*)?\)")

# Headings under which a link is a deliberate "read this next" rather than an
# incidental mention. Worth ranking above the rest.
SEEALSO_RE = re.compile(r"see\s+also|next\s+steps?|related|learn\s+more|further\s+reading", re.I)

SKIP_SCHEMES = ("mailto:", "tel:", "data:", "javascript:")


def site_base(pages: list[Page]) -> str:
    """The URL prefix that turns a page URL back into its path.

    Derived, not configured. `Source.base_url` is absent on the llms-full sources
    (xai builds its URLs from a template instead), and declaring the same string
    in a second place is a second place for it to go wrong.
    """
    for page in pages:
        if page.path and page.url.endswith(page.path):
            return page.url[: -len(page.path)]
    return ""


def _candidates(href: str, page_url: str, bases: list[str]) -> list[str]:
    """Every path `href` could plausibly mean. The caller keeps the ones that exist.

    A site-absolute link means different things on different sites, and nothing
    on the page says which: Claude Code writes `/en/sandboxing` for
    `<base>/en/sandboxing` — rooted at the docs — while opencode writes
    `/docs/config` for `<base>/config`, rooted at the host. Rather than configure
    that per source, offer both readings and let the page table decide; a path
    that does not exist is not a link.
    """
    if href.startswith(("http://", "https://")):
        return [slug_path(href, b) for b in bases if href.startswith(b)]
    if href.startswith("/"):
        rooted_at_host = [slug_path(urljoin(b, href), b) for b in bases]
        rooted_at_docs = [slug_path(b + href.lstrip("/"), b) for b in bases]
        return rooted_at_host + rooted_at_docs
    return [slug_path(urljoin(page_url, href), bases[0])] if bases else []


def page_links(page: Page, bases: list[str], known: set[str]) -> list[tuple[str, bool]]:
    """(to_path, in_seealso) for each link on `page` that lands on an indexed page.

    Links off the index are dropped: pointing a caller at a page it cannot read
    is worse than saying nothing.
    """
    found: dict[str, bool] = {}
    in_fence = seealso = False

    for line in page.body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:  # a URL in a code sample is an example, not a cross-reference
            continue
        if match := ANY_HEADING_RE.match(line):
            seealso = bool(SEEALSO_RE.search(match["text"]))
            continue
        for link in MD_LINK.finditer(line):
            href = link.group(1).split("#")[0].split("?")[0].strip()
            if not href or href.startswith(SKIP_SCHEMES):
                continue
            for path in _candidates(href, page.url, bases):
                if path in known and path != page.path:
                    found[path] = found.get(path, False) or seealso
                    break
    return list(found.items())


def build_links(source: Source, pages: list[Page]) -> list[tuple[str, str, str, int, int]]:
    """Rows for the `links` table: (source, from_path, to_path, in_seealso, ord).

    `ord` is the link's position in the page, so a caller can show them back in
    the order the author wrote them — the only ordering that is not our guess.
    """
    bases = [b for b in (site_base(pages), *source.link_bases) if b]
    known = {p.path for p in pages}
    return [
        (source.id, page.path, to_path, int(seealso), i)
        for page in pages
        for i, (to_path, seealso) in enumerate(page_links(page, bases, known))
    ]
