CREATE TABLE IF NOT EXISTS reading_conversations (
    conversation_id TEXT PRIMARY KEY,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL,
    last_message_at_utc TIMESTAMPTZ,
    UNIQUE(
        conversation_id,
        reading_session_id,
        project_id,
        sor_document_id,
        source_sha256
    ),
    FOREIGN KEY(reading_session_id, project_id, sor_document_id, source_sha256)
        REFERENCES reading_sessions(
            reading_session_id,
            project_id,
            sor_document_id,
            source_sha256
        ) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reading_conversations_session
    ON reading_conversations(
        reading_session_id,
        status,
        updated_at_utc DESC
    );

CREATE TABLE IF NOT EXISTS reading_chat_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    reading_session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    sor_document_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    sequence_number INTEGER NOT NULL CHECK(sequence_number >= 1),
    role TEXT NOT NULL CHECK(role IN ('USER', 'ASSISTANT')),
    content TEXT NOT NULL CHECK(length(content) BETWEEN 1 AND 20000),
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'STALE')),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    created_at_utc TIMESTAMPTZ NOT NULL,
    UNIQUE(conversation_id, sequence_number),
    FOREIGN KEY(
        conversation_id,
        reading_session_id,
        project_id,
        sor_document_id,
        source_sha256
    ) REFERENCES reading_conversations(
        conversation_id,
        reading_session_id,
        project_id,
        sor_document_id,
        source_sha256
    ) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_reading_chat_messages_conversation
    ON reading_chat_messages(
        conversation_id,
        status,
        sequence_number ASC
    );
