"""Additive v15: immutable dispatch routing proof, without raw phone/account IDs."""

WHATSAPP_ROUTING_SCHEMA = """
CREATE TABLE whatsapp_dispatch_routes (
    outbox_id        INTEGER PRIMARY KEY REFERENCES notification_outbox (id) ON DELETE CASCADE,
    sender_scope     TEXT NOT NULL CHECK (length(sender_scope) = 64 AND sender_scope NOT GLOB '*[^0-9a-f]*'),
    recipient_digest TEXT NOT NULL CHECK (length(recipient_digest) = 64 AND recipient_digest NOT GLOB '*[^0-9a-f]*'),
    chat_id          INTEGER NOT NULL CHECK (typeof(chat_id) = 'integer' AND chat_id != 0),
    endpoint_version INTEGER NOT NULL CHECK (typeof(endpoint_version) = 'integer' AND endpoint_version >= 1),
    created_at       TEXT NOT NULL
);
"""
