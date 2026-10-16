CREATE TABLE IF NOT EXISTS pending_subscription_checks (
    ig_user_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    draft_id TEXT NOT NULL REFERENCES automation_drafts(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ig_user_id, sender_id, draft_id)
);
