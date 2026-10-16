"""SQLite persistence for Instagram connections and per-post rules."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "./social_automation.db"))


@contextmanager
def connection():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                ig_user_id TEXT,
                username TEXT,
                access_token TEXT,
                token_expires_at TEXT,
                connected_at TEXT
            );
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS automations (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                post_id TEXT NOT NULL,
                keyword TEXT NOT NULL,
                text TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, post_id)
            );
            CREATE TABLE IF NOT EXISTS automation_drafts (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                config_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_comments (
                comment_id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_messages (
                message_id TEXT PRIMARY KEY,
                automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS processed_draft_events (
                event_id TEXT NOT NULL,
                draft_id TEXT NOT NULL REFERENCES automation_drafts(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (event_id, draft_id)
            );
            CREATE TABLE IF NOT EXISTS pending_subscription_checks (
                ig_user_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                draft_id TEXT NOT NULL REFERENCES automation_drafts(id) ON DELETE CASCADE,
                session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                PRIMARY KEY (ig_user_id, sender_id, draft_id)
            );
        """)

        # Keep existing local databases compatible with the activation fields.
        for table in ("automations", "automation_drafts"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "enabled" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
