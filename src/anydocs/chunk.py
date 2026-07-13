from __future__ import annotations

import re
from dataclasses import dataclass

from anydocs.models import Page

HEADING_RE = re.compile(r"^(?P<hashes>#{2,3})\s+(?P<text>.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")

# A section past this is split at paragraph boundaries. The Claude Code hooks
# reference is a single 227 KB page; one chunk that size would swamp bm25's
# length normalisation and blow up any snippet we cut from it.
MAX_CHUNK = 4000
MIN_CHUNK = 60


@dataclass
class Chunk:
    source: str
    path: str
    anchor: str
    breadcrumb: str
    title: str
    heading: str
    body: str


MD_FORMATTING = re.compile(r"<[^>]*>|`|\*\*|\*|~~|\]\([^)]*\)|[\[\]]")


def anchor_slug(heading: str, style: str = "collapse") -> str:
    """Slugify a heading the way the docs site actually does, so `path#anchor` resolves.

    Checked against the live HTML, and the sites do not agree — hence `style`:

      claude-code  `/compact` - Compact conversation history -> /compact-compact-…
                   (runs of dashes collapse, and the slash SURVIVES:
                    `apt / dnf / apk` -> `apt-/-dnf-/-apk`)
      xai          Privacy & data lifecycle -> privacy--data-lifecycle
      codex        Network access <ElevatedRiskBadge /> -> network-access-
                   (no collapsing, and the trailing dash is kept)

    Common to all: lowercase, markdown and JSX stripped, `.` and whitespace act
    as separators (`CLAUDE.md` -> `claude-md`), `/` is literal, other
    punctuation is dropped. Dropping the slash — the obvious reading of "strip
    punctuation" — quietly breaks every link to such a section.
    """
    text = MD_FORMATTING.sub("", heading).lower()
    text = re.sub(r"[^\w\s/.-]", "", text)
    text = re.sub(r"[\s.]", "-", text.strip() if style == "collapse" else text)
    if style == "collapse":
        text = re.sub(r"-+", "-", text).strip("-")
    return text


def _split_long(body: str) -> list[str]:
    if len(body) <= MAX_CHUNK:
        return [body]
    parts, buf = [], ""
    for para in body.split("\n\n"):
        if buf and len(buf) + len(para) + 2 > MAX_CHUNK:
            parts.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    return parts


def chunk_page(page: Page, style: str = "collapse") -> list[Chunk]:
    """Split a page at its H2/H3 headings.

    Headings inside fenced code blocks are not headings — a bash comment like
    `## build the image` would otherwise start a phantom section.
    """
    sections: list[tuple[str, str, list[str]]] = []  # (heading, anchor, lines)
    current: tuple[str, str, list[str]] = ("", "", [])
    h2 = ""
    trail: dict[str, str] = {}  # anchor -> breadcrumb tail
    in_fence = False

    for line in page.body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            sections.append(current)
            heading = m["text"]
            anchor = anchor_slug(heading, style)
            if len(m["hashes"]) == 2:
                h2 = heading
                trail[anchor] = heading
            else:
                trail[anchor] = f"{h2} › {heading}" if h2 else heading
            current = (heading, anchor, [])
        else:
            current[2].append(line)
    sections.append(current)

    chunks = []
    for heading, anchor, lines in sections:
        body = "\n".join(lines).strip()
        if len(body) < MIN_CHUNK:
            continue
        tail = trail.get(anchor, heading)
        breadcrumb = f"{page.title} › {tail}" if tail else page.title
        for part in _split_long(body):
            chunks.append(
                Chunk(
                    source=page.source,
                    path=page.path,
                    anchor=anchor,
                    breadcrumb=breadcrumb,
                    title=page.title,
                    heading=heading,
                    body=part,
                )
            )
    return chunks
