-- One owner per runtime today. scope_id is a project boundary, not a tenant ID.
-- The resource and completed receipt are written in the same transaction.
CREATE TABLE entry_command_receipts (
    kind TEXT NOT NULL CHECK(kind IN ('project.create', 'document.import')),
    scope_id TEXT NOT NULL,
    command_id TEXT NOT NULL CHECK(command_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$'),
    request_sha256 TEXT NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
    resource_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(kind, scope_id, command_id),
    CHECK((kind = 'project.create' AND scope_id = '') OR
          (kind = 'document.import' AND scope_id <> ''))
);
