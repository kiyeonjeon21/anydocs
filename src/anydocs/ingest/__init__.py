from __future__ import annotations

from anydocs.ingest import llms_full, llms_txt, sitemap
from anydocs.models import Page, Source

STRATEGIES = {
    "llms-txt": llms_txt.ingest,
    "sitemap": sitemap.ingest,
    "llms-full": llms_full.ingest,
}


async def ingest_source(source: Source) -> tuple[list[Page], list[str]]:
    try:
        adapter = STRATEGIES[source.strategy]
    except KeyError:
        raise ValueError(
            f"{source.id}: unknown strategy {source.strategy!r} (have {', '.join(STRATEGIES)})"
        ) from None
    return await adapter(source)
