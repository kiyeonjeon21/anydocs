from __future__ import annotations

import sqlite3

import pytest

from anydocs.chunk import anchor_slug, chunk_page
from anydocs.ingest.fetch import SoftNotFound, fetch_text, validate_markdown
from anydocs.ingest.filters import extract_title
from anydocs.ingest.llms_txt import page_fetch_url
from anydocs.links import build_links
from anydocs.models import Page, Source, slug_path
from anydocs.index import SCHEMA
from anydocs.query import (
    clean_snippet,
    compile_query,
    dropped_terms,
    outlinks,
    query_units,
    rescue_term,
    unmatched_terms,
)


class FakeResponse:
    def __init__(self, text: str, ctype: str = "text/markdown") -> None:
        self.text = text
        self.headers = {"content-type": ctype}
        self.url = "https://example.com/x.md"

    def raise_for_status(self):
        return None


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


def test_transient_fetch_failures_are_retried(monkeypatch):
    import asyncio
    import httpx

    calls = 0

    class Client:
        async def get(self, url):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise httpx.ReadTimeout("slow")
            return FakeResponse("# Recovered")

    async def no_wait(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_wait)
    assert asyncio.run(fetch_text(Client(), "https://example.com/x.md")) == "# Recovered"
    assert calls == 3


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
    # opencode (github/Starlight): the dot is DROPPED, where Mintlify makes it a dash
    assert anchor_slug("Avante.nvim", "github") == "avantenvim"
    assert anchor_slug("JetBrains IDEs", "github") == "jetbrains-ides"


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


def test_unmatched_terms_names_words_that_missed_the_results():
    """OR matching always finds something, off the query's *least* interesting
    words. Asking Claude Code about `cursorrules composer tab autocomplete`
    returns keyboard-shortcut pages, because `tab` is everywhere while
    `cursorrules` is mentioned twice in the whole corpus and never ranks.

    Corpus presence is the wrong test — a word in 1 chunk of 4,000 is present
    and useless. What matters is whether it reached the results being read.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO chunks(source,path,anchor,breadcrumb,title,heading,body) VALUES (?,?,?,?,?,?,?)",
        [
            ("s", "keys", "", "b", "Keys", "Autocomplete", "press tab for autocomplete"),
            ("s", "migrate", "", "b", "Migrate", "From Cursor", "cursorrules is not read"),
        ],
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid,title,heading,body) "
        "SELECT id,title,heading,body FROM chunks"
    )
    rows = conn.execute("SELECT id AS chunk_id FROM chunks WHERE path='keys'").fetchall()

    # `cursorrules` exists in the corpus but is absent from what was returned.
    assert unmatched_terms(conn, "cursorrules tab autocomplete", rows) == ["cursorrules"]
    assert unmatched_terms(conn, "tab autocomplete", rows) == []
    assert unmatched_terms(conn, "anything", []) == []


def test_unknown_source_is_refused_not_silently_empty(monkeypatch):
    """Filtering to a source that does not exist used to return "no matches",
    which reads as "the docs don't cover this" — so a model that guessed
    `claude` for `claude-code` would confidently report hooks are undocumented.
    """
    from anydocs import server

    monkeypatch.setattr(server, "known_sources", lambda: ["claude-code", "codex"])
    monkeypatch.setattr(server, "indexed_sources", lambda: ["claude-code", "codex"])
    monkeypatch.setattr(server, "enabled_sources", lambda: [])

    assert server.scope_for("claude-code") == ["claude-code"]
    assert server.scope_for(None) is None
    with pytest.raises(ValueError, match="unknown source 'claude'.*claude-code, codex"):
        server.scope_for("claude")


def test_title_is_not_lifted_from_a_shell_comment():
    """`#` inside a fence is a comment, not a heading. opencode's pages mostly
    have no H1, so the first `# ...` line in the file was a bash comment:
    `troubleshooting` was titled `or`, `rules` was titled `SST v3 Monorepo
    Project`. Titles carry 10x weight in the ranking.
    """
    body = "Some prose.\n\n```bash\n# or\nopencode --help\n```\n\n## Real section\n"
    assert extract_title(body, "troubleshooting") == "Troubleshooting"
    # A real H1 still wins over the path.
    assert extract_title("# Actual Title\n\ntext", "some/path") == "Actual Title"


def test_docs_root_gets_a_readable_path():
    """`opencode.ai/docs/` reduces to the empty string. Search returned it as
    `opencode/`, a path read_doc then refused — an 8 KB page, visible and
    unreadable."""
    assert slug_path("https://opencode.ai/docs/", "https://opencode.ai/docs/") == "index"
    assert slug_path("https://opencode.ai/docs/cli.md", "https://opencode.ai/docs/") == "cli"


def test_llms_txt_can_fetch_markdown_from_a_cross_host_alias():
    source = Source(
        id="codex",
        title="Codex",
        strategy="llms-txt",
        entry="https://learn.chatgpt.com/llms.txt",
        base_url="https://developers.openai.com/codex/",
        fetch_base_url="https://learn.chatgpt.com/docs/",
    )
    assert page_fetch_url(
        "https://developers.openai.com/codex/guides/best-practices.md", source
    ) == "https://learn.chatgpt.com/docs/guides/best-practices.md"


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


def _page(source, path, url, body, title="T"):
    return Page(source=source, path=path, url=url, title=title, description="", body=body)


def test_links_resolve_every_shape_the_sites_actually_use():
    """The five sites write the same internal link four different ways, and a
    resolver that handles only one of them silently produces an empty graph —
    which is how Codex's 600 cross-references went missing.
    """
    src = Source(id="s", title="S", strategy="llms-txt", entry="e",
                 link_bases=["https://alias.example.com/docs/"])
    pages = [
        _page("s", "en/hooks", "https://x.example.com/docs/en/hooks", ""),
        _page("s", "en/sandboxing", "https://x.example.com/docs/en/sandboxing", ""),
        _page("s", "guide", "https://x.example.com/docs/guide", ""),
        _page("s", "start", "https://x.example.com/docs/start", body="\n".join([
            "[a](https://x.example.com/docs/en/hooks)",   # absolute, canonical host
            "[b](https://alias.example.com/docs/guide)",  # absolute, second host
            "[c](/en/sandboxing#modes)",                  # site-absolute, docs-rooted
            "[d](/docs/guide)",                           # site-absolute, host-rooted
            "[e](https://github.com/o/r)",                # external
            "[f](mailto:x@y.z)",
        ])),
    ]
    rows = build_links(src, pages)
    got = {to for _, frm, to, _, _ in rows if frm == "start"}
    assert got == {"en/hooks", "guide", "en/sandboxing"}


def test_links_ignore_code_samples_and_flag_see_also():
    """A URL inside a fenced block is an example, not a cross-reference. And the
    links an author files under "See also" are the ones they chose deliberately.
    """
    src = Source(id="s", title="S", strategy="llms-txt", entry="e")
    body = "\n".join([
        "# Permission modes",
        "```bash",
        "curl [x](/en/decoy)",
        "```",
        "Prose mentions [settings](/en/settings).",
        "## See also",
        "* [Sandboxing](/en/sandboxing): filesystem and network isolation",
    ])
    pages = [
        _page("s", "en/decoy", "https://x.example.com/docs/en/decoy", ""),
        _page("s", "en/settings", "https://x.example.com/docs/en/settings", ""),
        _page("s", "en/sandboxing", "https://x.example.com/docs/en/sandboxing", ""),
        _page("s", "en/permission-modes", "https://x.example.com/docs/en/permission-modes", body),
    ]
    graph = {to: seealso for _, frm, to, seealso, _ in build_links(src, pages)
             if frm == "en/permission-modes"}
    assert "en/decoy" not in graph  # fenced
    assert graph == {"en/settings": 0, "en/sandboxing": 1}


def test_rescue_names_the_page_a_missed_word_actually_lives_on():
    """The failure this exists for: asked `headless -p allowedTools sandbox flag`,
    OR matching ranked the headless pages and `sandbox` reached none of them —
    while `en/sandboxing` sat in the index. Saying "no result contains sandbox"
    was not enough; the caller answered anyway, and answered wrong. Name the page.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO chunks(source,path,anchor,breadcrumb,title,heading,body) VALUES (?,?,?,?,?,?,?)",
        [
            ("cc", "en/headless", "", "b", "Headless", "Usage", "run headless with allowedTools"),
            ("cc", "en/sandboxing", "", "b", "Sandboxing", "Modes", "the sandbox isolates bash"),
            ("other", "s", "", "b", "Other", "H", "a sandbox lives here too"),
        ],
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid,title,heading,body) SELECT id,title,heading,body FROM chunks"
    )
    pages, total = rescue_term(conn, "sandbox", ["cc"], limit=3)
    assert pages == ["cc/en/sandboxing"]  # scoped: the other source is not offered
    assert total == 1
    assert rescue_term(conn, "kubernetes", ["cc"], limit=3) == ([], 0)


def test_outlinks_put_see_also_first():
    """A "See also" link is a deliberate pointer; one in the prose is incidental.
    Within a group, keep the order the author wrote them — the only ordering here
    that is not our guess.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pages VALUES (?,?,?,?,?,?)",
        [("s", p, "u", p, "", "") for p in ("from", "prose-a", "prose-b", "seealso")],
    )
    conn.executemany(
        "INSERT INTO links VALUES (?,?,?,?,?)",
        [("s", "from", "prose-a", 0, 0), ("s", "from", "prose-b", 0, 1),
         ("s", "from", "seealso", 1, 2)],
    )
    rows, total = outlinks(conn, "s", "from", limit=8)
    assert [r["path"] for r in rows] == ["seealso", "prose-a", "prose-b"]
    assert total == 3

    # The total is counted separately, and exactly, so a LIMIT cannot pass itself
    # off as the whole story: read_doc says "8 of 51", not "8".
    rows, total = outlinks(conn, "s", "from", limit=2)
    assert [r["path"] for r in rows] == ["seealso", "prose-a"]
    assert total == 3
