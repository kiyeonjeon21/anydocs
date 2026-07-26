## What and why

<!-- What does this change, and what problem does it solve? Link an issue if there is one. -->

## Checklist

- [ ] `uv run pytest -q` passes
- [ ] `uv run anydocs-build` succeeds
- [ ] `uv run python scripts/eval_search.py --verbose` shows no regression
- [ ] `uv run python scripts/verify_anchors.py` passes (only relevant if a `sources/*.yaml` changed)
- [ ] If this touches search/ranking/chunking: I checked the "Do not retry these" table in `AGENTS.md` and this isn't one of them
- [ ] If this adds a source: locale globs use one glob per locale (no `{a,b}` brace expansion) and `expect_pages` is set

## For a retrieval/ranking change

<!-- Which of the four eval sets (hand, auto, anchor, rescue) did you run, and what moved? A ranking change with no numbers attached is hard to review. Delete this section if not applicable. -->

## For a new source

<!-- Which strategy (llms-txt, sitemap, llms-full)? Confirm `scripts/verify_anchors.py` passed against the live site. Delete this section if not applicable. -->
