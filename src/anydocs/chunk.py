from __future__ import annotations

import re
from dataclasses import dataclass

from anydocs.models import Page

HEADING_RE = re.compile(r"^(?P<hashes>#{2,3})\s+(?P<text>.+?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
ANY_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")


def iter_headings(body: str, min_level: int = 1, max_level: int = 6):
    """Yield (level, text) for real headings only.

    A `#` inside a fenced block is a shell comment, not a heading. Missing that
    is how `opencode/troubleshooting` ended up titled `or` — lifted from a bash
    `# or` — and a title carries 10x weight in the ranking.
    """
    in_fence = False
    for line in body.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if (m := ANY_HEADING_RE.match(line)) and min_level <= len(m["hashes"]) <= max_level:
            yield len(m["hashes"]), m["text"]

# A section past this is split at paragraph boundaries. The Claude Code hooks
# reference is a single 227 KB page; one chunk that size would swamp bm25's
# length normalisation and blow up any snippet we cut from it.
#
# Do not "tune" this. Swept 1000-4000 against both gold sets (scripts/
# sweep_chunk.py): nothing beats 4000. Smaller chunks look like they fix
# "settings file precedence order" — and they do — but they break "hook events
# list" and "config.toml model provider" in exchange, for no net gain. A run
# that reported 2000 as a win was measuring with a gold set that matched paths
# by substring, so `en/hooks` also "matched" `en/hooks-guide`.
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

    Read off the live HTML of each site, and they do not agree — hence `style`.
    The difference nobody would guess is what happens to a dot:

      collapse  (Mintlify: claude-code, cursor)
                `CLAUDE.md` -> claude-md          — dot becomes a separator
                `apt / dnf / apk` -> apt-/-dnf-/-apk  — the SLASH survives
                `` `/compact` - Compact… `` -> /compact-compact-…  — dashes collapse

      verbatim  (xai, codex)
                `Privacy & data lifecycle` -> privacy--data-lifecycle  — no collapsing
                `Network access <Badge />` -> network-access-          — trailing dash kept

      github    (Astro Starlight: opencode)
                `Avante.nvim` -> avantenvim       — dot simply DROPPED

    Common to all: lowercase, markdown and JSX stripped, whitespace to dashes.
    Dropping the slash — the obvious reading of "strip punctuation" — quietly
    breaks every Mintlify link to such a section, and nothing else would notice:
    a wrong slug still ranks fine, it just lands in the wrong place.
    """
    text = MD_FORMATTING.sub("", heading).lower()
    if style == "github":
        return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text).strip())

    text = re.sub(r"[^\w\s/.-]", "", text)
    text = re.sub(r"[\s.]", "-", text.strip() if style == "collapse" else text)
    if style == "collapse":
        text = re.sub(r"-+", "-", text).strip("-")
    return text


TABLE_ROW = re.compile(r"^\s*\|")


def _pack(blocks: list[str], sep: str, prefix: str = "", limit: int = MAX_CHUNK) -> list[str]:
    """Greedily fill parts up to `limit`, never splitting a block."""
    parts, buf = [], ""
    for block in blocks:
        if buf and len(prefix) + len(buf) + len(sep) + len(block) > limit:
            parts.append(prefix + buf)
            buf = block
        else:
            buf = f"{buf}{sep}{block}" if buf else block
    if buf:
        parts.append(prefix + buf)
    return parts


def split_block(block: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split one over-long paragraph.

    In practice this is always a big markdown table — the reference pages are
    built from them, and a table has no blank lines, so paragraph splitting
    cannot touch it (Claude Code's settings page holds a single 148 KB table).
    Break it by rows and repeat the header on every part, or the fragments are
    unreadable columns of values with nothing to name them.
    """
    lines = block.splitlines()
    header = ""
    if len(lines) >= 2 and TABLE_ROW.match(lines[0]) and set(lines[1].strip()) <= set("|-: "):
        header = "\n".join(lines[:2]) + "\n"
        lines = lines[2:]
    return _pack([ln[:limit] for ln in lines], "\n", header, limit)


def split_long(body: str, limit: int = MAX_CHUNK) -> list[str]:
    """Break a body into parts of at most `limit`, at the safest boundary available.

    `limit` is a parameter because read_doc serves the same tables back to a
    caller, and there it wants parts of ~20 KB, not the 4 KB the ranker wants.
    Defaulted, so the indexer's output is byte-identical.
    """
    if len(body) <= limit:
        return [body]
    blocks: list[str] = []
    for para in body.split("\n\n"):
        blocks.extend([para] if len(para) <= limit else split_block(para, limit))
    return _pack(blocks, "\n\n", "", limit)


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
        for part in split_long(body):
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
