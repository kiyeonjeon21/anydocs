from __future__ import annotations

import asyncio
import sqlite3

import pytest

from anydocs import server
from anydocs.index import build, connect
from anydocs.models import Page, Source


def _source(source_id: str) -> Source:
    return Source(
        id=source_id,
        title=source_id.title(),
        strategy="llms-txt",
        entry="https://example.com/llms.txt",
        base_url=f"https://example.com/{source_id}/",
    )


def _page(source: str, path: str) -> Page:
    body = f"# {path}\n\n## Setup\n\n" + "documentation body text " * 12
    return Page(
        source=source,
        path=path,
        url=f"https://example.com/{source}/{path}",
        title=path.title(),
        description=f"Description for {path}",
        body=body,
    )


@pytest.fixture
def server_db(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    build(
        path,
        [
            (_source("codex"), [_page("codex", "config")]),
            (_source("claude-code"), [_page("claude-code", "en/hooks")]),
        ],
        "now",
    )
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)
    yield conn
    conn.close()


def test_read_doc_cannot_bypass_source_scope(server_db, monkeypatch):
    monkeypatch.setenv("ANYDOCS_SOURCES", "codex")

    assert server.list_sources().startswith("codex")
    assert "claude-code" not in server.list_sources()
    with pytest.raises(ValueError, match="disabled by ANYDOCS_SOURCES"):
        server.read_doc("claude-code/en/hooks")
    with pytest.raises(ValueError, match="no page"):
        server.read_doc("en/hooks")
    with pytest.raises(ValueError, match="names source 'claude-code'.*source='codex'"):
        server.read_doc("claude-code/en/hooks", source="codex")


@pytest.mark.parametrize("limit", [0, -1, 9, 1000, True])
def test_search_limit_is_enforced_before_query(limit):
    with pytest.raises(ValueError, match="limit must be an integer from 1 to 8"):
        server.search_docs("config", limit=limit)


def test_fts_failures_are_not_reported_as_no_matches(monkeypatch):
    monkeypatch.setattr(server, "db", lambda: object())

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("corrupt index")

    monkeypatch.setattr(server, "search", broken)
    with pytest.raises(RuntimeError, match="index query failed: corrupt index"):
        server.search_docs("config")


def test_mcp_metadata_and_dynamic_schema(server_db, monkeypatch):
    monkeypatch.setenv("ANYDOCS_SOURCES", "codex")
    mcp = server.build_mcp()
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert mcp.instructions == server.SERVER_INSTRUCTIONS
    assert set(tools) == {
        "list_sources",
        "search_docs",
        "read_doc",
        "grep_docs",
        "list_pages",
    }
    search_schema = tools["search_docs"].inputSchema
    source = next(
        item
        for item in search_schema["properties"]["source"]["anyOf"]
        if item.get("type") == "string"
    )
    assert source["enum"] == ["codex"]
    assert search_schema["properties"]["limit"]["minimum"] == 1
    assert search_schema["properties"]["limit"]["maximum"] == 8
    for tool in tools.values():
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


# --- read_doc must never hand back a dead end -------------------------------
#
# It used to. `server.HEADING_RE` matched H2/H3 only, so an over-long H3 section
# was "outlined" against children it could not see, and the caller got a table cut
# mid-row above an empty menu. Worse, 13 sections have no subheadings at any level
# because they are one enormous table — the settings reference, the env-var
# reference, every slash command — and for those an outline is not a poor answer,
# it is the wrong question. read_doc simply could not return them.


def _deep_page() -> Page:
    """An H3 whose only children are H4s — the `en/hooks` § `PreToolUse` shape."""
    filler = "prose about the event. " * 60
    return Page(
        source="claude-code",
        path="en/deep",
        url="https://example.com/claude-code/en/deep",
        title="Deep",
        description="A page with H4 children",
        body=(
            "# Deep\n\n## Hook events\n\nThe events, in lifecycle order.\n\n"
            "### PreToolUse\n\nRuns before a tool call.\n\n"
            f"#### PreToolUse input\n\n{filler * 12}\n\n"
            f"#### PreToolUse decision control\n\n{filler * 12}\n\n"
            "### PostToolUse\n\nRuns after.\n"
        ),
    )


def _table_page() -> Page:
    rows = "\n".join(f"| `VAR_{i}` | what VAR_{i} does, at some length. |" for i in range(700))
    return Page(
        source="claude-code",
        path="en/env-vars",
        url="https://example.com/claude-code/en/env-vars",
        title="Env vars",
        description="Every environment variable",
        body=f"# Env vars\n\n## Variables\n\n| Variable | Purpose |\n| :--- | :--- |\n{rows}\n",
    )


@pytest.fixture
def deep_db(tmp_path, monkeypatch):
    path = tmp_path / "deep.db"
    build(path, [(_source("claude-code"), [_deep_page(), _table_page()])], "now")
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)
    yield conn
    conn.close()


def test_outline_offers_children_at_the_next_level_down(deep_db):
    """An H3's children are H4s. Looking only for H2/H3 finds none and offers an
    empty menu — which is what shipped, on the most-searched page in the corpus."""
    out = server.read_doc("claude-code/en/deep", section="PreToolUse")
    assert "too large to return whole" in out
    assert "- PreToolUse input" in out
    assert "- PreToolUse decision control" in out


def test_every_anchor_an_outline_prints_can_actually_be_read(deep_db):
    """The regression test for the dead end: whatever the menu names must resolve."""
    out = server.read_doc("claude-code/en/deep", section="Hook events")
    offered = [
        line.strip()[2:]
        for line in out.split("one of these>:")[1].splitlines()
        if line.startswith("  - ")
    ]
    assert offered, "an outline with nothing on it is the bug"
    for name in offered:
        assert name in server.read_doc("claude-code/en/deep", section=name)


def test_a_section_can_be_asked_for_below_h3(deep_db):
    """H4/H5 were unreachable: extract_section's regex stopped at H3."""
    assert "PreToolUse input" in server.read_doc("claude-code/en/deep", section="PreToolUse input")


def test_a_giant_table_is_paginated_not_dead_ended(deep_db):
    """No subheadings at any level, so there is nothing to outline. Serve it."""
    first = server.read_doc("claude-code/en/env-vars", section="Variables")
    assert "part 1 of" in first
    assert "grep_docs" in first  # one entry is far cheaper fetched than paged to
    assert "| `VAR_0` |" in first

    second = server.read_doc("claude-code/en/env-vars", section="Variables", part=2)
    assert "| Variable | Purpose |" in second, "each part repeats the table header"
    assert "| `VAR_0` |" not in second

    parts = int(first.split("part 1 of ")[1].split(".")[0])
    seen = "".join(
        server.read_doc("claude-code/en/env-vars", section="Variables", part=i)
        for i in range(1, parts + 1)
    )
    for i in range(700):
        assert f"| `VAR_{i}` |" in seen, f"row {i} is reachable by no call at all"


def test_part_out_of_range_is_loud(deep_db):
    for bad in (0, 999):
        with pytest.raises(ValueError, match="does not exist"):
            server.read_doc("claude-code/en/env-vars", section="Variables", part=bad)
    # Ignoring part= on a small section hands back part 1 to a caller who thinks
    # they are reading part 2.
    with pytest.raises(ValueError, match="returned whole"):
        server.read_doc("claude-code/en/deep", section="PostToolUse", part=2)


# --- anchors the caller was handed must resolve ------------------------------


def _slug_page(source: str) -> Page:
    return Page(
        source=source,
        path="rules",
        url=f"https://example.com/{source}/rules",
        title="Rules",
        description="Rules",
        body=(
            "# Rules\n\n### Using opencode.json\n\nUse the `instructions` field. "
            + "body text " * 20
            + "\n\n### Project Rules\n\nTitle case. "
            + "body text " * 20
            + "\n\n### Project rules\n\nSentence case, a different section. "
            + "body text " * 20
        ),
    )


@pytest.fixture
def slug_db(tmp_path, monkeypatch):
    src = Source(
        id="opencode",
        title="opencode",
        strategy="llms-txt",
        entry="https://example.com/llms.txt",
        base_url="https://example.com/opencode/",
        slug_style="github",  # dots are DROPPED, not turned into separators
    )
    path = tmp_path / "slug.db"
    build(path, [(src, [_slug_page("opencode")])], "now")
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)
    yield conn
    conn.close()


def test_the_anchor_search_docs_hands_back_resolves_whatever_the_slug_style(slug_db):
    """opencode slugs `Using opencode.json` to `using-opencodejson`, dropping the
    dot. extract_section re-slugged with the default style, got `using-opencode.json`,
    and told the caller a section it had just pointed at did not exist. The style is
    not in the shipped index, so the server offers every spelling instead."""
    anchor = slug_db.execute(
        "SELECT anchor FROM chunks WHERE heading = 'Using opencode.json'"
    ).fetchone()["anchor"]
    assert anchor == "using-opencodejson"
    assert "instructions" in server.read_doc("opencode/rules", section=anchor)
    # and the heading itself still works
    assert "instructions" in server.read_doc("opencode/rules", section="Using opencode.json")


def test_two_headings_sharing_an_anchor_are_declared(slug_db):
    """`Project Rules` and `Project rules` slug alike. Returning the first in
    silence is how a caller reads confidently wrong text."""
    out = server.read_doc("opencode/rules", section="project-rules")
    assert "also matches 'Project rules'" in out
    assert "Title case" in out


# --- nothing may truncate in silence -----------------------------------------


def test_outlink_footer_names_the_total(tmp_path, monkeypatch):
    """Claude Code's settings page cross-references 51 pages and the footer showed
    8, saying nothing of the other 43."""
    targets = [_page("claude-code", f"en/t{i}") for i in range(20)]
    links = "\n".join(f"- [target {i}](/en/t{i})" for i in range(20))
    hub = Page(
        source="claude-code",
        path="en/hub",
        url="https://example.com/claude-code/en/hub",
        title="Hub",
        description="Links to everything",
        body=f"# Hub\n\n## See also\n\n{links}\n",
    )
    path = tmp_path / "links.db"
    build(path, [(_source("claude-code"), [hub, *targets])], "now")
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)
    out = server.read_doc("claude-code/en/hub")
    assert f"showing {server.OUTLINK_MAX} of 20" in out
    conn.close()


def test_grep_says_how_many_matches_it_hid_on_a_page(tmp_path, monkeypatch):
    """`plugin` matched 52 lines of opencode/plugins; three were shown and nothing
    said the other 49 existed. The global cap of 40 announced itself; the per-page
    cap of 3 did not, so a caller could not tell a page with 3 hits from one with 52."""
    hits = "\n".join(f"- the plugin does thing {i}" for i in range(12))
    page = Page(
        source="codex",
        path="plugins",
        url="https://example.com/codex/plugins",
        title="Plugins",
        description="Plugins",
        body=f"# Plugins\n\n## Overview\n\n{hits}\n",
    )
    path = tmp_path / "grep.db"
    build(path, [(_source("codex"), [page])], "now")
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)

    out = server.grep_docs("does thing", source="codex")  # matches the 12 list lines
    assert out.count("codex/plugins:") == server.GREP_PER_PAGE + 1  # 3 hits + the notice
    assert f"{12 - server.GREP_PER_PAGE} more matches on this page" in out
    conn.close()


def test_list_pages_does_not_blow_the_budget(tmp_path, monkeypatch):
    """It called itself "a cheap map" and cost 7,600 tokens on claude-code — the one
    tool here with no cap at all. Past a point it lists paths, not descriptions."""
    pages = [_page("claude-code", f"en/sub/p{i}") for i in range(120)]
    path = tmp_path / "many.db"
    build(path, [(_source("claude-code"), pages)], "now")
    conn = connect(path, read_only=True)
    monkeypatch.setattr(server, "_conn", conn)

    out = server.list_pages("claude-code")
    assert "120 pages" in out
    assert "en/sub/ (120)" in out  # the prefix to narrow with, and its count
    assert "Description for" not in out  # the descriptions are what cost the tokens
    assert len(out) < 8_000

    # ...and narrowing brings them back
    assert "Description for" in server.list_pages("claude-code", prefix="en/sub/p1%")
    conn.close()
