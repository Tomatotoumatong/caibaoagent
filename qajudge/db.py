"""qa.sqlite — this project's own store. The earnings_radar database is only ever read."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS http_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL, path TEXT NOT NULL, params TEXT, status INTEGER, latency_ms INTEGER,
    rl_limit TEXT, rl_remaining TEXT, rl_reset TEXT, retry_after TEXT, error_type TEXT, error_code TEXT
);
CREATE TABLE IF NOT EXISTS roic_events (          -- one row per (symbol, ROIC event)
    symbol TEXT NOT NULL, event_id TEXT NOT NULL, identifier TEXT NOT NULL,
    fiscal_year INTEGER, fiscal_quarter INTEGER, period_end_date TEXT, release_date TEXT,
    raw_transcript_id TEXT, listed_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (symbol, event_id)
);
CREATE TABLE IF NOT EXISTS transcripts (
    transcript_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, event_id TEXT,
    fiscal_year INTEGER, fiscal_quarter INTEGER, period_end_date TEXT,
    provider_created_at TEXT, fetched_at TEXT NOT NULL, n_chars INTEGER, text_sha256 TEXT,
    raw_path TEXT NOT NULL,
    sealed INTEGER NOT NULL DEFAULT 1             -- 1 = clean sample: stored, never read
);
CREATE TABLE IF NOT EXISTS questions (            -- analyst questions extracted from one transcript
    transcript_id TEXT NOT NULL, qid TEXT NOT NULL, exchange_idx INTEGER NOT NULL,
    analyst TEXT, firm TEXT, text TEXT NOT NULL, quote TEXT, quote_ok INTEGER NOT NULL,
    subject TEXT, extract_hash TEXT NOT NULL,
    PRIMARY KEY (transcript_id, qid)
);
CREATE TABLE IF NOT EXISTS matches (              -- judge output; unlisted (item, question) pairs = level 0
    run TEXT NOT NULL, event_key TEXT NOT NULL, arm TEXT NOT NULL, item_id TEXT NOT NULL,
    item_text TEXT NOT NULL, item_subject TEXT, qid TEXT, level INTEGER, why TEXT,
    transcript_id TEXT NOT NULL, prompt_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS matches_ev ON matches (run, event_key, arm);
CREATE TABLE IF NOT EXISTS llm_cache (
    request_hash TEXT PRIMARY KEY, model TEXT, task TEXT, created_at TEXT, response TEXT NOT NULL,
    prompt_tokens INTEGER, completion_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def kv_get(con: sqlite3.Connection, k: str, default=None):
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(r[0]) if r else default


def kv_set(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v, ensure_ascii=False)))
    con.commit()
