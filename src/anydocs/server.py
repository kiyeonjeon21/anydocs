from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from collections import Counter

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from anydocs.artifact import ensure_index
from anydocs.index import connect
from anydocs.chunk import ANY_HEADING_RE, FENCE_RE, iter_headings, split_long
from anydocs.query import (
    clean_snippet,
    dropped_terms,
    outlinks,
    rescue_term,
    search,
    unmatched_terms,
)

# Anything longer than this is summarised as an outline instead of returned whole.
# The Claude Code hooks reference is 227 KB, and guarding only the page is not
# enough: its `Hook events` section carries every event as a child heading and
# comes to 121 KB on its own, which blew the caller's context just the same.
BIG_PAGE = 40_000
BIG_SECTION = 20_000
# Prose kept above an outline, so the caller gets the paragraph that explains the
# list and not just the names. Cut at a line boundary, and said out loud.
OUTLINE_INTRO = 2_000

# Pages to name per rescued term, and cross-references to list under a page.
# read_doc is already returning a whole page, so a few lines of footer are free;
# the cap matters anyway, because Claude Code's settings page links to 51 others.
RESCUE_MAX = 3
OUTLINK_MAX = 8
OUTLINK_DESC = 90

# list_pages called itself "a cheap map" and cost 7,600 tokens on claude-code —
# 15x the whole search budget, from the one tool in this server with no cap at
# all. The descriptions are almost the entire bill, so past a certain size it
# lists paths only: that is still a map, and it is the map the caller asked for.
LIST_DESCRIPTIONS_UPTO = 40
LIST_MAX = 200

# grep exists to be cheap and exact. Uncapped it would reproduce the very
# token-burn failure this server was built to avoid. Scanning every page's body
# with Python's re takes ~25-90 ms over the whole corpus, so there is no reason
# to shell out to ripgrep — which is not reliably installed anyway.
GREP_MAX_MATCHES = 40
GREP_PER_PAGE = 3
GREP_MAX_COLS = 200
SEARCH_MAX_RESULTS = 8

SERVER_INSTRUCTIONS = """Search indexed product documentation with search_docs before
answering documentation questions. Pass source when the product is known, use concise
English keywords, then call read_doc on the relevant paths. Treat WARNING and NOTE as
incomplete evidence: follow rescued and related pages before concluding that a feature
does not exist. Use grep_docs only for exact regex lookup after search_docs."""

READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_conn: sqlite3.Connection | None = None
_enabled: list[str] = []


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = connect(ensure_index(), read_only=True)
    return _conn


def enabled_sources() -> list[str]:
    """ANYDOCS_SOURCES scopes a project to the docs it actually uses, so an
    unrelated corpus can't pollute its results."""
    raw = os.environ.get("ANYDOCS_SOURCES", "").strip()
    return [s.strip() for s in raw.split(",") if s.strip()] if raw else []


def known_sources() -> list[str]:
    ids = [r["id"] for r in db().execute("SELECT id FROM sources ORDER BY id")]
    scope = enabled_sources()
    return [i for i in ids if not scope or i in scope]


def indexed_sources() -> list[str]:
    return [r["id"] for r in db().execute("SELECT id FROM sources ORDER BY id")]


def scope_for(source: str | None) -> list[str] | None:
    """Resolve the `source` filter, refusing a name that does not exist.

    A wrong name must not pass quietly. Filtering to an unknown source used to
    return "no matches", which reads as "the docs don't cover this" — so a model
    that guessed `claude` instead of `claude-code` would confidently report that
    Claude Code has no hooks documentation.
    """
    if source is None:
        return enabled_sources() or None
    indexed = indexed_sources()
    if source not in indexed:
        raise ValueError(f"unknown source {source!r}. Available: {', '.join(known_sources())}")
    known = known_sources()
    if source not in known:
        raise ValueError(
            f"source {source!r} is disabled by ANYDOCS_SOURCES. "
            f"Available: {', '.join(known)}"
        )
    return [source]


def tool_models() -> list[Tool]:
    """Build public FastMCP Tool models with runtime source/limit schemas."""
    rows = db().execute("SELECT id, title FROM sources ORDER BY id").fetchall()
    scope = enabled_sources()
    rows = [r for r in rows if not scope or r["id"] in scope]
    ids = [r["id"] for r in rows]
    catalog = ", ".join(f"{r['id']} ({r['title']})" for r in rows)

    tools = []
    for fn in TOOL_FUNCTIONS:
        tool = Tool.from_function(fn, annotations=READ_ONLY_ANNOTATIONS)
        prop = tool.parameters.get("properties", {}).get("source")
        if prop is not None:
            # The parameter is `str` on list_pages and `str | None` elsewhere.
            target = next(
                (b for b in prop.get("anyOf", []) if b.get("type") == "string"), prop
            )
            target["enum"] = ids
            prop["description"] = f"One of: {catalog}"
            tool.description = f"{tool.description}\n\nIndexed sources: {catalog}."
        if tool.name == "search_docs":
            limit = tool.parameters["properties"]["limit"]
            limit["minimum"] = 1
            limit["maximum"] = SEARCH_MAX_RESULTS
        tools.append(tool)
    return tools


def build_mcp() -> FastMCP:
    return FastMCP("anydocs", instructions=SERVER_INSTRUCTIONS, tools=tool_models())


def resolve(path: str, source: str | None) -> tuple[str, str]:
    """Accept either ("claude-code/en/hooks", None) or ("en/hooks", "claude-code").

    Paths are source-qualified because bare ones collide: `overview` exists in
    several corpora at once.
    """
    if source:
        scope_for(source)  # an unknown name must say so, not report a missing page
        head, _, rest = path.partition("/")
        if rest and head in indexed_sources() and head != source:
            raise ValueError(
                f"path {path!r} names source {head!r}, but source={source!r} was requested"
            )
        return source, path.removeprefix(f"{source}/")
    head, _, rest = path.partition("/")
    if rest and head in indexed_sources():
        scope_for(head)
        return head, rest
    visible = known_sources()
    placeholders = ",".join("?" * len(visible))
    rows = db().execute(
        f"SELECT source FROM pages WHERE path=? AND source IN ({placeholders})",
        [path, *visible],
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["source"], path
    if not rows:
        raise ValueError(f"no page at {path!r}")
    found = ", ".join(f"{r['source']}/{path}" for r in rows)
    raise ValueError(f"{path!r} is ambiguous across sources: {found}")


def list_sources() -> str:
    """List the documentation sets in the index, with page counts and freshness."""
    rows = db().execute("SELECT * FROM sources ORDER BY id").fetchall()
    scope = enabled_sources()
    out = []
    for r in rows:
        if scope and r["id"] not in scope:
            continue
        tags = ", ".join(json.loads(r["tags"] or "[]"))
        out.append(f"{r['id']:<14} {r['page_count']:>4} pages  [{tags}]  {r['title']}")
    return "\n".join(out) or "index is empty"


def search_docs(query: str, source: str | None = None, limit: int = 8) -> str:
    """Search the documentation. Returns ranked snippets, not full pages.

    Use this first for any question about a documented tool. Follow up with
    read_doc on the paths it returns.

    **If a NOTE says one of your words missed, believe it.** Matching is OR, so a
    distinctive word can be outvoted by the common ones next to it — the note
    names the pages that word really lives on. Read one before you conclude the
    feature does not exist.

    **Pass `source` whenever the question names one product.** These doc sets
    cover the same ground in different words, so an unfiltered search spends
    slots on the wrong products: a question about Claude Code hooks will also
    return Cursor's and Codex's. Omit `source` only to compare products, or when
    you genuinely do not know which one holds the answer.

    **Query in English.** The indexed docs are English and matching is lexical,
    so a question in another language finds nothing — translate it to English
    keywords first ("훅 이벤트 목록" -> "hook events list").

    Keyword-style queries work best and filler words are dropped. Symbols are
    fine here — `AGENTS.md`, `PreToolUse`, `--flag-name`, `spec_version` all
    match, because punctuation is treated as a word boundary rather than
    dropped. Do not reach for grep_docs just because the query contains one.
    There is no fuzzy matching, so a typo finds nothing.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= SEARCH_MAX_RESULTS:
        raise ValueError(f"limit must be an integer from 1 to {SEARCH_MAX_RESULTS}")
    scope = scope_for(source)
    try:
        rows, expr = search(db(), query, sources=scope, limit=limit)
    except sqlite3.Error as exc:
        raise RuntimeError(f"index query failed: {exc}") from exc
    dropped = dropped_terms(query)

    if not rows:
        if dropped:
            return (
                f"no matches: the index is English-only, and {', '.join(dropped)!r} "
                f"cannot be searched. Translate the query to English keywords."
            )
        return (
            f"no matches for {query!r}. There is no fuzzy matching, so check the "
            f"spelling; for an exact symbol or regex, try grep_docs({query!r})."
        )

    lines = []
    if dropped:
        # Never let a half-understood query pass as a confident answer.
        lines += [
            f"WARNING: {', '.join(dropped)} was ignored (the index is English-only), "
            f"so these results answer only {expr}. Re-search in English.",
            "",
        ]
    try:
        lines += rescue_block(query, rows, scope)
    except sqlite3.Error as exc:
        raise RuntimeError(f"index query failed: {exc}") from exc
    lines += [f"{len(rows)} results (matched: {expr})", ""]
    for r in rows:
        anchor = f"#{r['anchor']}" if r["anchor"] else ""
        lines.append(f"[{r['score']:.1f}] {r['source']}/{r['path']}{anchor}")
        lines.append(f"      {r['breadcrumb']}")
        lines.append(f"      {clean_snippet(r['snip'], r['description'])}")
        lines.append("")
    return "\n".join(lines)


def rescue_block(query: str, rows: list[sqlite3.Row], scope: list[str] | None) -> list[str]:
    """For each query word that reached none of the results, say where it does live.

    Matching is OR, so a search always returns *something* — and what it returns
    is ranked on whichever words were common enough to win. A distinctive word
    can be outvoted and vanish. Asked for
    `headless -p allowedTools disallowedTools sandbox flag`, the ranker returned
    the headless and CLI pages, not one of which said `sandbox`, while
    `en/sandboxing` sat in the index. The caller read the results and reported
    that Claude Code has no sandbox. It has one.

    An earlier version of this said only "no result contains sandbox" and left
    the caller to act on it. It did not. So do the search the caller should have
    done: run the missed word alone and name the pages it is actually on. The
    page count separates the two cases that look identical from a list — a word
    on 14 pages is a topic that got buried, a word on 2 is a passing mention.
    """
    out: list[str] = []
    for term in unmatched_terms(db(), query, rows):
        pages, total = rescue_term(db(), term, scope, limit=RESCUE_MAX)
        if not pages:
            out.append(
                f"NOTE: {term!r} does not appear anywhere in the indexed docs, and no "
                f"result below contains it. Check the spelling, or the docs may not "
                f"cover it."
            )
            continue
        where = f"{total} indexed pages do" if total > 1 else "one indexed page does"
        out.append(
            f"NOTE: no result below contains {term!r} — it was outvoted by the more "
            f"common words in the query. But {where}, best first: "
            f"{', '.join(pages)}. If {term!r} is the point of the question, read one "
            f"of those before you answer; the results below are not about it."
        )
    return [*out, ""] if out else []


def read_doc(
    path: str, source: str | None = None, section: str | None = None, part: int = 1
) -> str:
    """Read a documentation page, or one section of it.

    `path` is what search_docs returns, e.g. "claude-code/en/hooks". Pass
    `section` (a heading or its anchor, at any depth) to read just that part —
    required for very large pages, which otherwise return an outline to choose
    from.

    A section that is one huge table — `en/settings` § `Available settings`,
    `en/env-vars` § `Variables` — has no subheadings to outline, so it comes back
    in parts, each carrying the table's header row. The reply names the part
    count; pass `part=2`, `part=3`… for the rest, or use grep_docs to pull a
    single entry out of it.

    The **Related pages** footer is the page's own outgoing cross-references —
    what its authors thought you should read next. Follow them when the question
    spans more than the one page you happened to land on.
    """
    src, rel = resolve(path, source)
    row = db().execute(
        "SELECT * FROM pages WHERE source=? AND path=?", (src, rel)
    ).fetchone()
    if not row:
        raise ValueError(f"no page at {src}/{rel}")

    # The body already opens with its own H1, so the header is just the source link.
    head = f"<!-- {src}/{rel} — {row['url']} -->\n"
    body = row["body"]

    foot = outlink_footer(src, rel)

    if section:
        found = extract_section(body, section)
        if found is None:
            heads = "\n".join(f"  - {h}" for h in headings(body))
            raise ValueError(f"no section {section!r} in {src}/{rel}. Sections:\n{heads}")
        text, level, also = found
        note = ""
        if also:
            # Two headings on one page slugging to the same anchor. Returning the
            # first silently is how a caller reads confidently wrong text.
            note = (
                f"\nNOTE: {section!r} also matches {also!r} on this page. This is the "
                f"first of the two — pass the other heading verbatim to read it instead.\n"
            )
        if len(text) > BIG_SECTION:
            text = _shrink(text, level, part)
        elif part != 1:
            # Silently ignoring it would hand back part 1 while the caller
            # believed they were reading part 2.
            raise ValueError(
                f"section {section!r} of {src}/{rel} is {len(text) // 1000} KB and is "
                f"returned whole — there is no part {part}. Drop part=."
            )
        return f"{head}{note}\n{text}\n{foot}"

    if part != 1:
        raise ValueError("part= applies to a section; pass section= as well")

    if len(body) > BIG_PAGE:
        body = (
            f"# {row['title']}\n\n{row['description']}\n\n"
            + _outline(body, 1, "This page")
        )
    return f"{head}\n{body}\n{foot}"


def _shrink(text: str, level: int, part: int) -> str:
    """An over-long section, made reachable — by outline if it has children, by
    pagination if it does not.

    The outline path used to look only for H2/H3 children, so an H3 section like
    `en/hooks` § `PreToolUse` — 21 KB, fourteen H4 children — offered a menu of
    nothing, over the top of a table cut mid-row. And 13 sections have no
    subheadings at *any* level, because they are one enormous table: the settings
    reference, the env-var reference, every slash command. For those an outline
    is not a poor answer, it is the wrong question, and read_doc simply could not
    return them.
    """
    own_heading, _, rest = text.partition("\n")
    subs = _outline(rest, level, "This section", lead_in=True)
    if subs is not None:
        return f"{own_heading}\n{subs}"

    parts = split_long(rest.strip(), BIG_SECTION)
    if not 1 <= part <= len(parts):
        raise ValueError(
            f"part {part} does not exist; this section has {len(parts)} parts (1-{len(parts)})"
        )
    return (
        f"{own_heading}\n\n{parts[part - 1]}\n\n"
        f"This section is a {len(rest) // 1000} KB table with no subheadings — "
        f"part {part} of {len(parts)}. Re-call read_doc with the same section= and "
        f"part={part + 1 if part < len(parts) else 1} for another, or use grep_docs "
        f"to pull out a single entry."
    )


def outlink_footer(src: str, rel: str) -> str:
    """The page's own cross-references, named at the end where they can be acted on.

    These are already in the body — and being in the body is not enough. The
    model that concluded Claude Code has no sandbox had *read*
    `en/permission-modes`, whose "See also" links straight to `en/sandboxing`; it
    was one bullet in fifteen kilobytes of markdown and went by unread. The same
    fact, as a short labelled list at the end of the response, is something a
    caller can act on.

    Always the whole page's links, even when only a section was asked for: a
    "See also" block sits at the *foot* of a page, so scoping this to the section
    would hide it from exactly the caller who read straight to the part they were
    pointed at.
    """
    rows, total = outlinks(db(), src, rel, limit=OUTLINK_MAX)
    if not rows:
        return ""
    shown = f"showing {len(rows)} of {total}" if total > len(rows) else f"{total}"
    out = ["", f"Related pages (cross-referenced by this one — {shown}):"]
    for r in rows:
        desc = " ".join((r["description"] or r["title"] or "").split())
        if len(desc) > OUTLINK_DESC:
            desc = desc[:OUTLINK_DESC].rsplit(" ", 1)[0] + "…"
        out.append(f"  {src}/{r['path']}" + (f" — {desc}" if desc else ""))
    return "\n".join(out)


def _outline(text: str, level: int, what: str, *, lead_in: bool = False) -> str | None:
    """List the headings one level below `level`. None when there are none.

    The level matters: children of an H3 are H4s, and looking only for H2/H3 —
    which is what this did — finds nothing under `PreToolUse` and offers the
    caller an empty menu.

    With `lead_in`, keep the prose before the first subheading: for a section
    like `Hook events` that is the paragraph actually explaining the list, and
    dropping it would leave the caller with nothing but names.
    """
    subs = [t for lvl, t in iter_headings(text, level + 1, 6) if lvl == level + 1]
    if not subs:
        return None
    out = ""
    if lead_in:
        first = re.search(rf"^#{{{level + 1}}}\s", text, re.MULTILINE)
        intro = (text[: first.start()] if first else text).strip()
        if len(intro) > OUTLINE_INTRO:
            intro = intro[:OUTLINE_INTRO].rsplit("\n", 1)[0] + "\n…(cut)"
        out += f"\n{intro}\n"
    listing = "\n".join(f"  - {h}" for h in subs)
    return (
        f"{out}\n{what} is {len(text) // 1000} KB — too large to return whole.\n"
        f"Re-call read_doc with section=<one of these>:\n\n{listing}"
    )


def headings(body: str) -> list[str]:
    # Fence-aware: a `## foo` inside a bash block is a comment, not a section the
    # caller can ask for.
    return [text for _, text in iter_headings(body, 2, 3)]


# Every spelling a heading's anchor could have. The server does not know the
# source's slug_style — it is not in the shipped index — and it does not need to:
# offer all three and keep whichever the caller used. Across the corpus this makes
# only 4 headings out of 638 pages ambiguous, and all four are the same title in
# two cases ("Project Rules" / "Project rules"), which read_doc now says out loud.
SLUG_STYLES = ("collapse", "github", "verbatim")


def _spellings(heading: str) -> set[str]:
    from anydocs.chunk import anchor_slug

    return {heading.strip().lower(), *(anchor_slug(heading, s) for s in SLUG_STYLES)}


def extract_section(body: str, wanted: str) -> tuple[str, int, str] | None:
    """Find a heading at any depth and return (text, level, colliding heading or "").

    `wanted` may be the heading itself or its anchor in any site's slug style.
    search_docs hands back the anchor as the *source site* spells it — opencode
    slugs `Using opencode.json` to `using-opencodejson`, dropping the dot — and
    re-slugging that with the default style finds nothing, so the caller was told
    a section it had just been pointed at did not exist.
    """
    target = wanted.strip().lower().lstrip("#")
    marks = [
        (m, len(m["hashes"]), m["text"])
        for m in _iter_heading_matches(body)
    ]
    hits = [i for i, (_, _, text) in enumerate(marks) if target in _spellings(text)]
    if not hits:
        return None

    i, (m, level, _) = hits[0], marks[hits[0]]
    end = len(body)
    for nxt, nxt_level, _ in marks[i + 1 :]:
        if nxt_level <= level:
            end = nxt.start()
            break
    also = marks[hits[1]][2] if len(hits) > 1 else ""
    return body[m.start() : end].strip(), level, also


def _iter_heading_matches(body: str):
    """ANY_HEADING_RE matches, fence-aware — iter_headings gives text, not offsets."""
    pos, in_fence = 0, False
    for line in body.splitlines(keepends=True):
        if FENCE_RE.match(line):
            in_fence = not in_fence
        elif not in_fence and (m := ANY_HEADING_RE.match(line)):
            yield _Mark(m, pos)
        pos += len(line)


class _Mark:
    """An ANY_HEADING_RE match, re-based onto the whole body."""

    def __init__(self, m: re.Match, offset: int) -> None:
        self._m, self._offset = m, offset

    def __getitem__(self, key: str) -> str:
        return self._m[key]

    def start(self) -> int:
        return self._offset + self._m.start()


def grep_docs(pattern: str, source: str | None = None, ignore_case: bool = True) -> str:
    """Regex search over the raw documentation markdown. **Use search_docs first.**

    This is the last resort, not the first move. It returns raw matching lines,
    so it costs several times what a search costs and gives you no ranking — an
    unscoped grep for a common term burns ~1.5k tokens and still hits its cap.

    Symbols are NOT a reason to come here: `AGENTS.md`, `PreToolUse` and
    `--flag-name` all match in search_docs. Come here only when search_docs
    missed, or when you need *every* occurrence of a literal — an env var, a
    config key, a flag — rather than the best passages about it.

    `pattern` is a Python regex. Pass `source` unless you truly want all of them.
    """
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ValueError(f"bad regex {pattern!r}: {exc}") from None

    scope = scope_for(source)
    sql = "SELECT source, path, body FROM pages"
    params: list = []
    if scope:
        sql += f" WHERE source IN ({','.join('?' * len(scope))})"
        params = scope
    sql += " ORDER BY source, path"

    hits, truncated = [], False
    for row in db().execute(sql, params):
        page = f"{row['source']}/{row['path']}"
        found = [
            (n, line.strip())
            for n, line in enumerate(row["body"].splitlines(), 1)
            if rx.search(line)
        ]
        for lineno, line in found[:GREP_PER_PAGE]:
            text = line[:GREP_MAX_COLS] + ("…" if len(line) > GREP_MAX_COLS else "")
            hits.append(f"{page}:{lineno}: {text}")
        # One page cannot flood the list — but it must not be able to hide behind
        # the cap either. `plugin` matches 52 lines of opencode/plugins and three
        # were shown, with nothing to say the other 49 existed.
        if len(found) > GREP_PER_PAGE:
            hits.append(f"{page}: … {len(found) - GREP_PER_PAGE} more matches on this page")
        if len(hits) >= GREP_MAX_MATCHES:
            truncated = True
            break

    if not hits:
        return f"no matches for {pattern!r}"
    out = "\n".join(hits[:GREP_MAX_MATCHES])
    if truncated:
        out += f"\n… more than {GREP_MAX_MATCHES} matches; narrow the pattern or pass source="
    return out


def list_pages(source: str, prefix: str = "") -> str:
    """List a source's pages — a cheap map of what exists.

    Descriptions come with the listing while it is small. A large one is served
    as paths only, with the directory prefixes and their page counts, because the
    descriptions alone would cost more than fifteen searches. Pass `prefix` (one
    of the ones it names) to narrow it and get the descriptions back.
    """
    scope_for(source)  # reject an unknown name instead of returning an empty map
    rows = db().execute(
        "SELECT path, title, description FROM pages "
        "WHERE source=? AND path LIKE ? ORDER BY path",
        (source, f"{prefix}%"),
    ).fetchall()
    if not rows:
        raise ValueError(f"no pages in {source!r} under {prefix!r}")

    if len(rows) <= LIST_DESCRIPTIONS_UPTO:
        return "\n".join(
            f"{source}/{r['path']}\n      {r['title']}"
            + (f" — {trim(r['description'], 110)}" if r["description"] else "")
            for r in rows
        )

    dirs = Counter(
        r["path"].rsplit("/", 1)[0] + "/" if "/" in r["path"] else "(top level)"
        for r in rows
    )
    shown = rows[:LIST_MAX]
    out = [
        f"{len(rows)} pages in {source!r}"
        + (f" under {prefix!r}" if prefix else "")
        + " — too many to describe. Paths only; pass prefix= for titles and descriptions.",
        "",
        "  " + "  ".join(f"{d} ({n})" for d, n in dirs.most_common()),
        "",
        *(f"{source}/{r['path']}" for r in shown),
    ]
    if len(rows) > LIST_MAX:
        out.append(f"… and {len(rows) - LIST_MAX} more; narrow with prefix=")
    return "\n".join(out)


def trim(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def check_scope() -> None:
    """Fail loudly on a bad ANYDOCS_SOURCES rather than serving an empty index.

    A typo here used to disable the whole server without saying anything:
    every source got filtered away, so list_sources answered "index is empty"
    and every search answered "no matches" — which the caller reads as "the docs
    do not cover this" rather than "your config is wrong".
    """
    scope = enabled_sources()
    if not scope:
        return
    known = indexed_sources()
    if unknown := [s for s in scope if s not in known]:
        raise ValueError(
            f"ANYDOCS_SOURCES names unknown sources: {', '.join(unknown)}. "
            f"Available: {', '.join(known)}"
        )


TOOL_FUNCTIONS = (list_sources, search_docs, read_doc, grep_docs, list_pages)


def main() -> int:
    try:
        ensure_index()
        check_scope()
        server = build_mcp()
    except Exception as exc:  # noqa: BLE001
        print(f"anydocs: {exc}", file=sys.stderr)
        return 1
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
