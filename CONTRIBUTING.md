# Contributing to anydocs

Thanks for looking at this.
This file is the quick-start version of how to work on anydocs.
For the deep rationale behind retrieval decisions, and a long list of ideas that were tried and rejected with numbers attached, read `AGENTS.md` first.
It is written for agents, but every rule in it applies to humans too.

## Setup

```bash
uv sync
```

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Before opening a PR

Run the same checks CI runs:

```bash
uv run pytest -q
uv run anydocs-build                      # real ingest, ~1 min
uv run python scripts/eval_search.py --verbose
uv run python scripts/verify_anchors.py   # only meaningful if a sources/*.yaml changed
```

All four must pass. `verify_anchors.py` hits the live doc sites, so it can fail on transient network issues, not just real regressions.

## Retrieval changes need evidence, not feel

This is the one rule that matters most in this codebase.
`scripts/eval_search.py` scores three gold sets (hand, auto, anchor), and `scripts/eval_rescue.py` scores the NOTE-fires-a-warning behavior separately.
Each measures a different thing, and using the wrong one has shipped a bad change before.
Read the "Retrieval changes need evidence" section of `AGENTS.md` before touching `SEARCH_SQL`, chunking, ranking, or the rescue trigger.

Before proposing a retrieval tweak, check the "Do not retry these" table in `AGENTS.md`.
It lists ideas that were measured and rejected, including dense/hybrid embeddings, AND-first matching, document-frequency stopwords, and several rescue-trigger variants.
Re-deriving one of these costs about a day; the table exists so nobody has to pay that twice.

## Adding a documentation source

One YAML file in `sources/`, three supported strategies (`llms-txt`, `sitemap`, `llms-full`).
Read the "Adding a source" section of `AGENTS.md` first: locale globs and `slug_style` both fail silently if you get them wrong, and that section explains exactly how.
Always run `scripts/verify_anchors.py` against a new source before opening a PR; it diffs your ingested anchors against the live HTML.

## Style

- No comments that just restate what the code does. If a comment exists, it should explain something non-obvious: a workaround, a constraint, a reason a simpler approach doesn't work.
- Small, focused PRs. A retrieval change and a source addition should not travel in the same PR.
- Match the existing tone in commit messages and `AGENTS.md`: state what was measured, not what was hoped.

## Reporting a security issue

Do not open a public issue. See `SECURITY.md`.
