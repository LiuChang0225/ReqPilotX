UPDATE knowledge_document_syncs
SET payload_storage_key = NULL
WHERE payload_storage_key = '';

ALTER TABLE knowledge_document_syncs
    ALTER COLUMN payload_storage_key DROP NOT NULL,
    ALTER COLUMN payload_storage_key DROP DEFAULT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_knowledge_sync_payload_storage_key'
    ) THEN
        ALTER TABLE knowledge_document_syncs
            ADD CONSTRAINT fk_knowledge_sync_payload_storage_key
            FOREIGN KEY (payload_storage_key)
            REFERENCES stored_objects(storage_key)
            ON DELETE RESTRICT;
    END IF;
END
$$;
