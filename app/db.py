"""SQLite storage layer.

One connection per operation (cheap with WAL), safe across FastAPI's
threadpool and the asyncio runner.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "veille.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS category_sets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS domain_categories (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id    INTEGER NOT NULL REFERENCES category_sets(id) ON DELETE CASCADE,
    domaine   TEXT NOT NULL,
    categorie TEXT NOT NULL,
    UNIQUE(set_id, domaine)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    models          TEXT NOT NULL DEFAULT '[]',   -- JSON list: openai/gemini/anthropic/xai
    schedule_time   TEXT,                          -- "HH:MM" local time, NULL = manual only
    interval_days   INTEGER NOT NULL DEFAULT 1,
    start_date      TEXT,                          -- "YYYY-MM-DD"
    end_date        TEXT,                          -- "YYYY-MM-DD" inclusive, NULL = forever
    status          TEXT NOT NULL DEFAULT 'active',-- active | paused | archived
    category_set_id INTEGER REFERENCES category_sets(id) ON DELETE SET NULL,
    share_token     TEXT                           -- secret token for the read-only visitor URL
);

CREATE TABLE IF NOT EXISTS prompts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    categorie   TEXT,
    prompt      TEXT NOT NULL,
    langue      TEXT,
    proxy       TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running',  -- running | done | failed | cancelled
    trigger     TEXT NOT NULL DEFAULT 'manual',   -- manual | schedule
    total_tasks INTEGER NOT NULL DEFAULT 0,
    ok_tasks    INTEGER NOT NULL DEFAULT 0,
    err_tasks   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    campaign_id      INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    date             TEXT NOT NULL,
    modele           TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    prompt_categorie TEXT,
    langue           TEXT,
    reponse          TEXT,
    url              TEXT,
    url_originale    TEXT,
    domaine          TEXT
);
CREATE INDEX IF NOT EXISTS idx_results_campaign ON results(campaign_id);
CREATE INDEX IF NOT EXISTS idx_results_run      ON results(run_id);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    campaign_id INTEGER NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,                 -- openai | gemini | anthropic | xai
    ref         TEXT NOT NULL,                 -- JSON: provider-side identifiers
    payload     TEXT NOT NULL,                 -- JSON: custom_id -> {prompt, categorie, langue}
    status      TEXT NOT NULL DEFAULT 'submitted',  -- submitted | done | failed | cancelled
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_batches_run ON batches(run_id);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE CASCADE,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    level       TEXT NOT NULL DEFAULT 'info',     -- info | warning | error
    source      TEXT,                              -- openai | gemini | anthropic | xai | runner | scheduler
    message     TEXT NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON events(campaign_id);
"""

DEFAULT_SETTINGS = {
    "admin_user": "admin",
    "admin_password": "admin",
    "openai_api_key": "",
    "gemini_api_key": "",
    "anthropic_api_key": "",
    "xai_api_key": "",
    "openai_model": "gpt-5-search-api",
    "gemini_model": "gemini-3-flash-preview",
    "anthropic_model": "claude-sonnet-4-6",
    "xai_model": "grok-4-1-fast",
    "concurrency": "4",          # parallel requests per provider
    "request_timeout": "180",    # seconds per API call
    "resolve_timeout": "8",      # seconds per redirect resolution
    "max_retries": "2",
    "batch_mode": "on",          # on: prompts without a proxy go through Batch APIs
    "batch_poll_interval": "60", # seconds between batch status checks
}

SECRET_KEYS = {"openai_api_key", "gemini_api_key", "anthropic_api_key", "xai_api_key",
               "admin_password"}


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        # Migration: per-campaign visitor share token.
        camp_columns = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
        if "share_token" not in camp_columns:
            import secrets
            conn.execute("ALTER TABLE campaigns ADD COLUMN share_token TEXT")
            for row in conn.execute("SELECT id FROM campaigns").fetchall():
                conn.execute("UPDATE campaigns SET share_token=? WHERE id=?",
                             (secrets.token_urlsafe(16), row["id"]))
        # Migration for databases created before prompt_categorie existed:
        # add the column, then backfill it from the campaign's prompt list.
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(results)")}
        if "prompt_categorie" not in columns:
            conn.execute("ALTER TABLE results ADD COLUMN prompt_categorie TEXT")
            conn.execute(
                "UPDATE results SET prompt_categorie = ("
                "  SELECT p.categorie FROM prompts p"
                "  WHERE p.campaign_id = results.campaign_id AND p.prompt = results.prompt"
                "  LIMIT 1)"
            )


# ---------------------------------------------------------------- settings

def get_settings() -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    settings = dict(DEFAULT_SETTINGS)
    settings.update({r["key"]: r["value"] for r in rows})
    return settings


def set_settings(values: dict[str, str]) -> None:
    with connect() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )


# ---------------------------------------------------------------- helpers

def rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


def log_event(campaign_id, run_id, level, source, message, detail=None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)[:20000]
    with connect() as conn:
        conn.execute(
            "INSERT INTO events(campaign_id, run_id, level, source, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (campaign_id, run_id, level, source, (message or "")[:2000], detail),
        )


def create_batch(run_id: int, campaign_id: int, provider: str,
                 ref: dict, payload: dict) -> int:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO batches(run_id, campaign_id, provider, ref, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, campaign_id, provider, json.dumps(ref),
             json.dumps(payload, ensure_ascii=False)),
        )
        return cursor.lastrowid


def set_batch_status(batch_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))


def pending_batches(run_id: int | None = None) -> list[dict]:
    query = "SELECT * FROM batches WHERE status='submitted'"
    args: list = []
    if run_id is not None:
        query += " AND run_id=?"
        args.append(run_id)
    with connect() as conn:
        rows = rows_to_dicts(conn.execute(query, args).fetchall())
    for row in rows:
        row["ref"] = json.loads(row["ref"])
        row["payload"] = json.loads(row["payload"])
    return rows


def insert_results(rows: list[dict]) -> None:
    if not rows:
        return
    # Retry briefly on lock contention: many tasks write concurrently.
    for attempt in range(5):
        try:
            with connect() as conn:
                conn.executemany(
                    "INSERT INTO results(run_id, campaign_id, date, modele, prompt, "
                    "prompt_categorie, langue, reponse, url, url_originale, domaine) "
                    "VALUES (:run_id, :campaign_id, :date, :modele, :prompt, "
                    ":prompt_categorie, :langue, :reponse, :url, :url_originale, :domaine)",
                    rows,
                )
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))
