ALTER TABLE sor_documents
    ADD COLUMN IF NOT EXISTS publication_currentness_generation BIGINT
    NOT NULL DEFAULT 0;

ALTER TABLE sor_documents
    DROP CONSTRAINT IF EXISTS sor_documents_publication_currentness_nonnegative;

ALTER TABLE sor_documents
    ADD CONSTRAINT sor_documents_publication_currentness_nonnegative
    CHECK (publication_currentness_generation >= 0);

CREATE OR REPLACE FUNCTION reqpilotx_advance_publication_currentness()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE sor_documents
    SET publication_currentness_generation =
            publication_currentness_generation + 1
    WHERE id = NEW.sor_document_id;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_table_projection_publication_currentness
    ON table_projection_revisions;

CREATE TRIGGER trg_table_projection_publication_currentness
AFTER INSERT ON table_projection_revisions
FOR EACH ROW
EXECUTE FUNCTION reqpilotx_advance_publication_currentness();

CREATE OR REPLACE FUNCTION reqpilotx_advance_extraction_publication_currentness()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE sor_documents
    SET publication_currentness_generation =
            publication_currentness_generation + 1
    WHERE id = NEW.aggregate_id;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_extraction_result_publication_currentness
    ON runtime_jobs;

CREATE TRIGGER trg_extraction_result_publication_currentness
AFTER UPDATE OF result ON runtime_jobs
FOR EACH ROW
WHEN (
    NEW.kind = 'document.extract'
    AND NEW.aggregate_type = 'sor-document'
    AND NEW.result IS DISTINCT FROM OLD.result
)
EXECUTE FUNCTION reqpilotx_advance_extraction_publication_currentness();
