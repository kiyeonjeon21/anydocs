from __future__ import annotations

import sqlite3

import pytest

from anydocs.chunk import anchor_slug, chunk_page
from anydocs.ingest.fetch import SoftNotFound, validate_markdown
from anydocs.models import Page
from anydocs.index import SCHEMA
from anydocs.query import (
    absent_terms,
    clean_snippet,
    compile_query,
    dropped_terms,
    query_units,
)


class FakeResponse:
    def __init__(self, text: str, ctype: str = "text/markdown") -> None:
        self.text = text
        self.headers = {"content-type": ctype}
        self.url = "https://example.com/x.md"


def test_soft_404_is_rejected():
    """docs.cursor.com answers 200 + an HTML shell for every unknown path."""
    with pytest.raises(SoftNotFound):
        validate_markdown(FakeResponse("<!DOCTYPE html><html>404", "text/html"))
    with pytest.raises(SoftNotFound):
        # HTML body sneaking through with a non-html content-type
        validate_markdown(FakeResponse("<!doctype html><html>404", "text/plain"))
    with pytest.raises(SoftNotFound):
        validate_markdown(FakeResponse("   ", "text/markdown"))
    assert validate_markdown(FakeResponse("# Real page")) == "# Real page"


@pytest.mark.parametrize(
    ("raw", "expect_first"),
    [
        # Glued by punctuation => a symbol => adjacency phrase, not separate terms.
        ("--dangerously-skip-permissions", '"dangerously skip permissions"'),
        ("spec_version", '"spec version"'),
        ("Bash(git:*)", '"Bash git"'),
        ("PreToolUse hook", '"PreToolUse" "hook"'),
        # Filler is dropped, or an xAI FAQ outranks the real hooks guide.
        ("how do I add a hook", '"add" "hook"'),
    ],
)
def test_compile_query(raw, expect_first):
    assert " ".join(query_units(raw)) == expect_first


def test_compile_query_survives_fts5_operators():
    """Raw text in a MATCH raises `fts5: syntax error`; only quoted words go in."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
    conn.execute("INSERT INTO t VALUES ('use --dangerously-skip-permissions to bypass')")

    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM t WHERE t MATCH ?", ("--dangerously-skip-permissions",))

    expr = compile_query("--dangerously-skip-permissions")[0]
    assert conn.execute("SELECT * FROM t WHERE t MATCH ?", (expr,)).fetchall()


def test_compile_query_all_stopwords_still_matches():
    assert compile_query("how do I") == ['"how" OR "do" OR "I"', '"how"* OR "do"* OR "I"*']
    assert compile_query("!!!") == []


def test_anchor_slug_matches_live_site_ids():
    """Every expectation here was read off the real rendered HTML."""
    # claude-code (collapse): the slash survives; dash runs collapse
    assert anchor_slug("Set up your first hook") == "set-up-your-first-hook"
    assert anchor_slug("The `/hooks` menu") == "the-/hooks-menu"
    assert anchor_slug("apt / dnf / apk") == "apt-/-dnf-/-apk"
    assert anchor_slug("HTTP/SSE servers") == "http/sse-servers"
    assert anchor_slug("`/compact` - Compact conversation history") == (
        "/compact-compact-conversation-history"
    )
    # A dot is a separator, not punctuation to drop: CLAUDE.md -> claude-md
    assert anchor_slug("Project instructions (CLAUDE.md and rules)") == (
        "project-instructions-claude-md-and-rules"
    )
    # xai/codex (verbatim): dash runs are NOT collapsed and a trailing dash stays
    assert anchor_slug("Privacy & data lifecycle", "verbatim") == "privacy--data-lifecycle"
    assert anchor_slug('Network access <ElevatedRiskBadge class="ml-2" />', "verbatim") == (
        "network-access-"
    )


def test_clean_snippet_flattens_and_truncates():
    assert clean_snippet("a\n\n  b   c") == "a b c"
    long = "x " * 400
    assert len(clean_snippet(long)) <= 301
    # No « » means the match was title-only, so snippet() just returned the head.
    assert clean_snippet("no highlight here", fallback="better text") == "better text"
    assert clean_snippet("has «hit» here", fallback="unused") == "has «hit» here"


def test_clean_snippet_drops_link_urls_but_keeps_highlights():
    snip = "See the [«hook» «events» list](/en/hooks#hook-events) for more"
    assert clean_snippet(snip) == "See the «hook» «events» list for more"
    assert clean_snippet("go to https://example.com/x now") == "go to now"


def test_clean_snippet_replaces_a_table_header_with_the_description():
    """The reference pages are tables, so a title-only match centres the snippet
    on the header row — `| Flag | Description | Example |` answers nothing, and
    it happens on exactly the pages people search for most."""
    header = "| Flag | Description | Example |\n| :--- | :--- | :--- |\n| `--x` | does x |"
    assert clean_snippet(header, "Complete reference for the CLI.") == (
        "Complete reference for the CLI."
    )
    # A real body match is kept even when the chunk happens to be a table.
    hit = "| `--«flag»` | turns it on |"
    assert clean_snippet(hit, "unused").startswith("| `--«flag»`")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # The index is English; these words reach nothing and must not be dropped
        # in silence — "claude code 훅 이벤트" would quietly become "claude code".
        ("설정 우선순위 알려줘", ["설정", "우선순위", "알려줘"]),
        ("claude code 훅 이벤트", ["훅", "이벤트"]),
        ("フック イベント", ["フック", "イベント"]),
        ("hook events list", []),
        ("--dangerously-skip-permissions", []),
        ("??? 🤔", []),  # punctuation and emoji carry no meaning; nothing to warn about
    ],
)
def test_dropped_terms_flags_unsearchable_words(query, expected):
    assert dropped_terms(query) == expected


def test_absent_terms_names_words_the_corpus_never_uses():
    """OR matching always finds *something*: a TensorFlow question returns
    confident-looking hits off `model` and `loop` alone. Naming the absent word
    is what turns that into "these docs don't discuss TensorFlow"."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO chunks(source,path,anchor,breadcrumb,title,heading,body) "
        "VALUES ('s','p','','b','Hooks','Hook events','the model runs a hook loop')"
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid,title,heading,body) "
        "SELECT id,title,heading,body FROM chunks"
    )

    assert absent_terms(conn, "tensorflow model loop") == ["tensorflow"]
    assert absent_terms(conn, "model hook") == []
    assert absent_terms(conn, "hok modle") == ["hok", "modle"]  # typos surface too


def test_unknown_source_is_refused_not_silently_empty(monkeypatch):
    """Filtering to a source that does not exist used to return "no matches",
    which reads as "the docs don't cover this" — so a model that guessed
    `claude` for `claude-code` would confidently report hooks are undocumented.
    """
    from anydocs import server

    monkeypatch.setattr(server, "known_sources", lambda: ["claude-code", "codex"])
    monkeypatch.setattr(server, "enabled_sources", lambda: [])

    assert server.scope_for("claude-code") == ["claude-code"]
    assert server.scope_for(None) is None
    with pytest.raises(ValueError, match="unknown source 'claude'.*claude-code, codex"):
        server.scope_for("claude")


def test_chunker_ignores_headings_inside_code_fences():
    page = Page(
        source="s", path="p", url="u", title="T", description="",
        body=(
            "## Real heading\n" + "body text. " * 12 + "\n\n"
            "```bash\n## not a heading\necho hi\n```\n" + "more body. " * 12 + "\n"
        ),
    )
    chunks = chunk_page(page)
    assert [c.heading for c in chunks] == ["Real heading"]
    assert "not a heading" in chunks[0].body


def test_chunker_splits_a_giant_table():
    """A markdown table has no blank lines, so paragraph splitting cannot touch
    it — Claude Code's settings page shipped as one 148 KB chunk until this."""
    rows = "\n".join(f"| `key_{i}` | description number {i} |" for i in range(400))
    page = Page(
        source="s", path="p", url="u", title="T", description="",
        body=f"## Available settings\n\n| Key | Description |\n| --- | --- |\n{rows}\n",
    )
    chunks = chunk_page(page)
    assert len(chunks) > 1
    assert max(len(c.body) for c in chunks) <= 4000
    # Every part repeats the header, or a fragment is nameless columns of values.
    assert all("| Key | Description |" in c.body for c in chunks)
    assert "`key_399`" in chunks[-1].body


def test_chunker_builds_breadcrumbs():
    page = Page(
        source="s", path="p", url="u", title="Hooks reference", description="",
        body="## Hook events\n" + "x " * 40 + "\n### PreToolUse\n" + "y " * 40 + "\n",
    )
    crumbs = {c.heading: c.breadcrumb for c in chunk_page(page)}
    assert crumbs["PreToolUse"] == "Hooks reference › Hook events › PreToolUse"
