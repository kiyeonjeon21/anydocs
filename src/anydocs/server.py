from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from anydocs.artifact import ensure_index
from anydocs.index import connect
from anydocs.query import clean_snippet, dropped_terms, search, unmatched_terms

# Anything longer than this is summarised as an outline instead of returned whole.
# The Claude Code hooks reference is 227 KB, and guarding only the page is not
# enough: its `Hook events` section carries every event as a child heading and
# comes to 121 KB on its own, which blew the caller's context just the same.
BIG_PAGE = 40_000
BIG_SECTION = 20_000

# grep exists to be cheap and exact. Uncapped it would reproduce the very
# token-burn failure this server was built to avoid. Scanning every page's body
# with Python's re takes ~25-90 ms over the whole corpus, so there is no reason
# to shell out to ripgrep — which is not reliably installed anyway.
GREP_MAX_MATCHES = 40
GREP_PER_PAGE = 3
GREP_MAX_COLS = 200

mcp = FastMCP("anydocs")

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


def scope_for(source: str | None) -> list[str] | None:
    """Resolve the `source` filter, refusing a name that does not exist.

    A wrong name must not pass quietly. Filtering to an unknown source used to
    return "no matches", which reads as "the docs don't cover this" — so a model
    that guessed `claude` instead of `claude-code` would confidently report that
    Claude Code has no hooks documentation.
    """
    if source is None:
        return enabled_sources() or None
    known = known_sources()
    if source not in known:
        raise ValueError(f"unknown source {source!r}. Available: {', '.join(known)}")
    return [source]


def annotate_source_params() -> None:
    """Put the source catalogue into the tool schemas themselves.

    Otherwise `source` is a bare optional string and the model has no way to
    know which names are valid without spending a call on list_sources first —
    or guessing, which is how it gets a confident wrong answer.
    """
    rows = db().execute("SELECT id, title FROM sources ORDER BY id").fetchall()
    scope = enabled_sources()
    rows = [r for r in rows if not scope or r["id"] in scope]
    ids = [r["id"] for r in rows]
    catalog = ", ".join(f"{r['id']} ({r['title']})" for r in rows)

    for tool in mcp._tool_manager._tools.values():
        prop = tool.parameters.get("properties", {}).get("source")
        if prop is None:
            continue
        # The parameter is `str` on list_pages and `str | None` elsewhere.
        target = next((b for b in prop.get("anyOf", []) if b.get("type") == "string"), prop)
        target["enum"] = ids
        prop["description"] = f"One of: {catalog}"
        tool.description = f"{tool.description}\n\nIndexed sources: {catalog}."


def resolve(path: str, source: str | None) -> tuple[str, str]:
    """Accept either ("claude-code/en/hooks", None) or ("en/hooks", "claude-code").

    Paths are source-qualified because bare ones collide: `overview` exists in
    several corpora at once.
    """
    if source:
        scope_for(source)  # an unknown name must say so, not report a missing page
        return source, path.removeprefix(f"{source}/")
    head, _, rest = path.partition("/")
    if rest and db().execute("SELECT 1 FROM sources WHERE id=?", (head,)).fetchone():
        return head, rest
    rows = db().execute("SELECT source FROM pages WHERE path=?", (path,)).fetchall()
    if len(rows) == 1:
        return rows[0]["source"], path
    if not rows:
        raise ValueError(f"no page at {path!r}")
    found = ", ".join(f"{r['source']}/{path}" for r in rows)
    raise ValueError(f"{path!r} is ambiguous across sources: {found}")


@mcp.tool()
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


@mcp.tool()
def search_docs(query: str, source: str | None = None, limit: int = 8) -> str:
    """Search the documentation. Returns ranked snippets, not full pages.

    Use this first for any question about a documented tool. Follow up with
    read_doc on the paths it returns.

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
    scope = scope_for(source)
    rows, expr = search(db(), query, sources=scope, limit=limit)
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
    if missed := unmatched_terms(db(), query, rows):
        # Matching is OR, so a query always finds *something* — usually off its
        # least interesting words. Say which words never reached the results, or
        # a question about `cursorrules` reads as answered by the pages that
        # merely happened to contain `tab`.
        lines += [
            f"NOTE: no result below contains {', '.join(missed)}. They matched on "
            f"the other words only — so if {missed[0]!r} is the point of the "
            f"question, these are not the answer (try grep_docs, or check the spelling).",
            "",
        ]
    lines += [f"{len(rows)} results (matched: {expr})", ""]
    for r in rows:
        anchor = f"#{r['anchor']}" if r["anchor"] else ""
        lines.append(f"[{r['score']:.1f}] {r['source']}/{r['path']}{anchor}")
        lines.append(f"      {r['breadcrumb']}")
        lines.append(f"      {clean_snippet(r['snip'], r['description'])}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def read_doc(path: str, source: str | None = None, section: str | None = None) -> str:
    """Read a documentation page, or one section of it.

    `path` is what search_docs returns, e.g. "claude-code/en/hooks". Pass
    `section` (a heading or its anchor) to read just that part — required for
    very large pages, which otherwise return an outline to choose from.
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

    if section:
        part = extract_section(body, section)
        if part is None:
            heads = "\n".join(f"  - {h}" for h in headings(body))
            raise ValueError(f"no section {section!r} in {src}/{rel}. Sections:\n{heads}")
        if len(part) > BIG_SECTION:
            # Outline the children, not the section itself, and keep its own
            # heading line so the caller still knows where they are.
            own_heading, _, rest = part.partition("\n")
            return f"{head}\n{own_heading}\n" + _outline(rest, "This section", lead_in=True)
        return f"{head}\n{part}"

    if len(body) > BIG_PAGE:
        return (
            f"{head}# {row['title']}\n\n{row['description']}\n\n"
            + _outline(body, "This page")
        )
    return f"{head}\n{body}"


def _outline(text: str, what: str, *, lead_in: bool = False) -> str:
    """Describe an over-long chunk of markdown instead of returning it.

    With `lead_in`, keep the prose before the first subheading: for a section
    like `Hook events` that is the paragraph actually explaining the list, and
    dropping it would leave the caller with nothing but names.
    """
    subs = headings(text)
    out = ""
    if lead_in:
        first = HEADING_RE.search(text)
        intro = (text[: first.start()] if first else text).strip()
        out += f"\n{intro[:2000]}\n"
    listing = "\n".join(f"  - {h}" for h in subs)
    return (
        f"{out}\n{what} is {len(text) // 1000} KB — too large to return whole.\n"
        f"Re-call read_doc with section=<one of these>:\n\n{listing}"
    )


HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.MULTILINE)


def headings(body: str) -> list[str]:
    return [m.group(2) for m in HEADING_RE.finditer(body)]


def extract_section(body: str, wanted: str) -> str | None:
    """Return a heading and everything under it, up to the next same-or-higher heading."""
    from anydocs.chunk import anchor_slug

    target = anchor_slug(wanted)
    marks = list(HEADING_RE.finditer(body))
    for i, m in enumerate(marks):
        if anchor_slug(m.group(2)) != target:
            continue
        level = len(m.group(1))
        end = len(body)
        for nxt in marks[i + 1 :]:
            if len(nxt.group(1)) <= level:
                end = nxt.start()
                break
        return body[m.start() : end].strip()
    return None


@mcp.tool()
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
        per_page = 0
        for lineno, line in enumerate(row["body"].splitlines(), 1):
            if not rx.search(line):
                continue
            text = line.strip()[:GREP_MAX_COLS]
            hits.append(f"{row['source']}/{row['path']}:{lineno}: {text}")
            per_page += 1
            if per_page >= GREP_PER_PAGE:
                break  # one page cannot flood the result list
        if len(hits) >= GREP_MAX_MATCHES:
            truncated = True
            break

    if not hits:
        return f"no matches for {pattern!r}"
    out = "\n".join(hits[:GREP_MAX_MATCHES])
    if truncated:
        out += f"\n… more than {GREP_MAX_MATCHES} matches; narrow the pattern or pass source="
    return out


@mcp.tool()
def list_pages(source: str, prefix: str = "") -> str:
    """List a source's pages with their descriptions — a cheap map of what exists."""
    scope_for(source)  # reject an unknown name instead of returning an empty map
    rows = db().execute(
        "SELECT path, title, description FROM pages "
        "WHERE source=? AND path LIKE ? ORDER BY path",
        (source, f"{prefix}%"),
    ).fetchall()
    if not rows:
        raise ValueError(f"no pages in {source!r} under {prefix!r}")
    return "\n".join(
        f"{source}/{r['path']}\n      {r['title']}"
        + (f" — {r['description'][:110]}" if r["description"] else "")
        for r in rows
    )


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
    known = [r["id"] for r in db().execute("SELECT id FROM sources ORDER BY id")]
    if unknown := [s for s in scope if s not in known]:
        raise ValueError(
            f"ANYDOCS_SOURCES names unknown sources: {', '.join(unknown)}. "
            f"Available: {', '.join(known)}"
        )


def main() -> int:
    try:
        ensure_index()
        check_scope()
        annotate_source_params()
    except Exception as exc:  # noqa: BLE001
        print(f"anydocs: {exc}", file=sys.stderr)
        return 1
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
