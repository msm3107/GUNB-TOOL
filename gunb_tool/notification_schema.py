"""Addytywny schemat nowych kanałów; bez nadawców i bez wpływu na doręczenia Telegrama.

Treść outbox jest migawką wiadomości, którą przyszły worker zachowuje przy ponowieniach.
Czasy zapisujemy jak w pozostałej bazie: ISO 8601 w UTC. Nie używamy rozszerzenia JSON1.
"""

NOTIFICATION_SCHEMA = """
CREATE TABLE notification_endpoints (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id            INTEGER NOT NULL REFERENCES bot_users (chat_id) ON DELETE CASCADE,
    channel            TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    address            TEXT NOT NULL CHECK (length(trim(address)) > 0),
    enabled            INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    mode               TEXT NOT NULL DEFAULT 'rano' CHECK (mode IN ('rano', 'wieczor', 'natychmiast')),
    verified_at        TEXT,
    consent_at         TEXT,
    consent_source     TEXT,
    consent_revoked_at TEXT,
    activated_at       TEXT,
    version            INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (chat_id, channel, address),
    CHECK (enabled = 0 OR (verified_at IS NOT NULL AND consent_at IS NOT NULL
                          AND consent_revoked_at IS NULL))
);

CREATE TABLE notification_outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id         INTEGER NOT NULL REFERENCES notification_endpoints (id) ON DELETE CASCADE,
    event_key           TEXT NOT NULL CHECK (length(trim(event_key)) > 0),
    part                INTEGER NOT NULL DEFAULT 0 CHECK (part >= 0),
    payload             TEXT NOT NULL,
    state               TEXT NOT NULL DEFAULT 'queued' CHECK (state IN (
                            'queued', 'sending', 'accepted', 'delivered', 'read', 'retry',
                            'failed', 'unknown', 'cancelled', 'expired')),
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at     TEXT NOT NULL,
    expires_at          TEXT,
    claimed_at          TEXT,
    claim_owner         TEXT,
    provider_message_id TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (endpoint_id, event_key, part)
);
CREATE INDEX ix_notification_outbox_due ON notification_outbox (state, next_attempt_at);
CREATE INDEX ix_notification_outbox_provider ON notification_outbox (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE TABLE notification_deliveries (
    endpoint_id  INTEGER NOT NULL REFERENCES notification_endpoints (id) ON DELETE CASCADE,
    id_sprawy    TEXT NOT NULL REFERENCES investments (id_sprawy) ON DELETE CASCADE,
    revision     TEXT NOT NULL,
    kind         TEXT NOT NULL,
    outcome      TEXT NOT NULL CHECK (outcome IN ('accepted', 'delivered', 'read', 'skipped')),
    processed_at TEXT NOT NULL,
    outbox_id    INTEGER REFERENCES notification_outbox (id) ON DELETE SET NULL,
    PRIMARY KEY (endpoint_id, id_sprawy, revision)
);
CREATE INDEX ix_notification_deliveries_history ON notification_deliveries (endpoint_id, processed_at);

CREATE TABLE notification_webhook_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    channel             TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    event_key           TEXT NOT NULL CHECK (length(trim(event_key)) > 0),
    endpoint_id         INTEGER REFERENCES notification_endpoints (id) ON DELETE CASCADE,
    provider_message_id TEXT,
    payload             TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    processed_at        TEXT,
    UNIQUE (channel, event_key)
);
CREATE INDEX ix_notification_webhook_pending ON notification_webhook_events (received_at)
    WHERE processed_at IS NULL;
CREATE INDEX ix_notification_webhook_endpoint ON notification_webhook_events (endpoint_id);
"""
