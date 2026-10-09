"""Additive v14: hashed verification challenges and durable abuse limits."""

EMAIL_VERIFICATION_SCHEMA = """
CREATE TABLE email_verifications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER REFERENCES bot_users (chat_id) ON DELETE SET NULL,
    endpoint_id      INTEGER REFERENCES notification_endpoints (id) ON DELETE SET NULL,
    endpoint_version INTEGER NOT NULL CHECK (endpoint_version >= 1),
    address_digest   TEXT NOT NULL CHECK (length(address_digest) = 64),
    token_digest     TEXT NOT NULL UNIQUE CHECK (length(token_digest) = 64),
    issued_at        TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    consumed_at      TEXT,
    invalidated_at   TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5)
);
CREATE INDEX ix_email_verification_endpoint ON email_verifications (endpoint_id, id DESC);
CREATE INDEX ix_email_verification_owner ON email_verifications (chat_id, issued_at);
CREATE INDEX ix_email_verification_address ON email_verifications (address_digest, issued_at);
CREATE INDEX ix_email_verification_issued ON email_verifications (issued_at);
"""
