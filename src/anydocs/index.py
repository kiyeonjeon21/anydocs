from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from anydocs.chunk import chunk_page
from anydocs.models import Page, Source

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE sources(
  id TEXT PRIMARY KEY, title TEXT, tags TEXT, base_url TEXT,
  page_count INTEGER, synced_at TEXT);

CREATE TABLE pages(
  source TEXT, path TEXT, url TEXT, title TEXT, description TEXT, body TEXT,
  PRIMARY KEY(source, path));

CREATE TABLE chunks(
  id INTEGER PRIMARY KEY, source TEXT, path TEXT, anchor TEXT,
  breadcrumb TEXT, title TEXT, heading TEXT, body TEXT);

CREATE INDEX chunks_source ON chunks(source);

-- External-content FTS: the index points back at `chunks` instead of storing a
-- second copy of every body. unicode61, not porter: stemming mangles the exact
-- symbols people actually search for (PreToolUse, spec_version).
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  title, heading, body,
  content='chunks', content_rowid='id',
  tokenize="unicode61 remove_diacritics 2");
"""

def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    # immutable=1, not mode=ro: the index never changes under us, so SQLite can
    # skip locking entirely and no -wal/-shm files appear next to the artifact.
    uri = f"file:{db_path}?immutable=1" if read_only else str(db_path)
    conn = sqlite3.connect(uri, uri=read_only, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def build(db_path: Path, ingested: Iterable[tuple[Source, list[Page]]], synced_at: str) -> dict:
    db_path.unlink(missing_ok=True)
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
    conn.execute("INSERT INTO meta VALUES ('synced_at', ?)", (synced_at,))
    stats = {}

    for source, pages in ingested:
        chunks = [c for page in pages for c in chunk_page(page, source.slug_style)]
        conn.execute(
            "INSERT INTO sources VALUES (?,?,?,?,?,?)",
            (
                source.id,
                source.title,
                json.dumps(source.tags),
                source.base_url,
                len(pages),
                synced_at,
            ),
        )
        conn.executemany(
            "INSERT INTO pages VALUES (?,?,?,?,?,?)",
            [(p.source, p.path, p.url, p.title, p.description, p.body) for p in pages],
        )
        conn.executemany(
            "INSERT INTO chunks(source,path,anchor,breadcrumb,title,heading,body) VALUES (?,?,?,?,?,?,?)",
            [
                (c.source, c.path, c.anchor, c.breadcrumb, c.title, c.heading, c.body)
                for c in chunks
            ],
        )
        stats[source.id] = {"pages": len(pages), "chunks": len(chunks)}

    conn.execute(
        "INSERT INTO chunks_fts(rowid, title, heading, body) "
        "SELECT id, title, heading, body FROM chunks"
    )
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return stats
