-- Opt-in SaaS identities. Unowned legacy projects are never adopted implicitly.
CREATE TABLE saas_accounts (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
    password_digest TEXT NOT NULL, created_at BIGINT NOT NULL
);
CREATE TABLE saas_sessions (
    token_sha256 TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    csrf_token TEXT NOT NULL, expires_at BIGINT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES saas_accounts(id) ON DELETE CASCADE
);
CREATE INDEX saas_sessions_account ON saas_sessions(account_id);
CREATE TABLE saas_project_owners (
    project_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES saas_accounts(id) ON DELETE CASCADE
);
CREATE INDEX saas_project_accounts ON saas_project_owners(account_id);
CREATE TABLE saas_auth_attempts (
    bucket TEXT PRIMARY KEY, window_start BIGINT NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE saas_invitations (
    token_sha256 TEXT PRIMARY KEY, created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL, used_account_id TEXT,
    FOREIGN KEY(used_account_id) REFERENCES saas_accounts(id)
);
