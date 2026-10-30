ALTER TABLE oauth_states ADD COLUMN cf_user_id TEXT;

CREATE TABLE IF NOT EXISTS cf_user_connections (
    cf_user_id TEXT PRIMARY KEY,
    ig_user_id TEXT NOT NULL UNIQUE
);
