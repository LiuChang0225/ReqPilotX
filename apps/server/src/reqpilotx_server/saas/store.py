from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid

from reqpilotx_server import db

# Bound concurrent memory use on the 2 GiB pilot host. Passwords are never logged.
_password_slots = threading.BoundedSemaphore(1)
SCHEMA = """
CREATE TABLE IF NOT EXISTS saas_accounts (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
    password_digest TEXT NOT NULL, created_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_sessions (
    token_sha256 TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    csrf_token TEXT NOT NULL, expires_at BIGINT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES saas_accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS saas_sessions_account ON saas_sessions(account_id);
CREATE TABLE IF NOT EXISTS saas_project_owners (
    project_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY(account_id) REFERENCES saas_accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS saas_project_accounts ON saas_project_owners(account_id);
CREATE TABLE IF NOT EXISTS saas_auth_attempts (
    bucket TEXT PRIMARY KEY, window_start BIGINT NOT NULL, attempts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS saas_invitations (
    token_sha256 TEXT PRIMARY KEY, created_at BIGINT NOT NULL,
    expires_at BIGINT NOT NULL, used_account_id TEXT,
    FOREIGN KEY(used_account_id) REFERENCES saas_accounts(id)
);
"""


def ensure_schema() -> None:
    with db.get_conn() as conn:
        for statement in SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)
    from reqpilotx_server.saas.config import SaaSSettings
    settings = SaaSSettings.from_environment()
    if settings.enabled:
        # Existing/consumed invitations are never renewed on application restart.
        add_invitation(settings.invitation, seconds=86400)


class InvitationRejected(ValueError):
    pass


def add_invitation(token: str, *, seconds: int = 86400) -> None:
    """Trusted local administration only. This operation has no HTTP endpoint."""
    if not 32 <= len(token) <= 512 or not 300 <= seconds <= 604800:
        raise ValueError("Invalid invitation lifetime or token length")
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute("""INSERT INTO saas_invitations(token_sha256,created_at,expires_at)
            VALUES(?,?,?) ON CONFLICT(token_sha256) DO NOTHING""", (token_hash(token), now, now + seconds))


def invitation_valid(token: str) -> bool:
    with db.get_conn() as conn:
        return conn.execute("""SELECT 1 FROM saas_invitations WHERE token_sha256=?
            AND used_account_id IS NULL AND expires_at>?""", (token_hash(token), int(time.time()))).fetchone() is not None


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def password_digest(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    with _password_slots:
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**17, r=8, p=1,
                                maxmem=256 * 1024 * 1024, dklen=32)
    return "scrypt:131072:8:1:" + salt.hex() + ":" + digest.hex()


def check_password(password: str, value: str) -> bool:
    try:
        prefix, n, r, p, salt, _ = value.split(":")
        if (prefix, n, r, p) != ("scrypt", "131072", "8", "1"):
            return False
        return hmac.compare_digest(password_digest(password, bytes.fromhex(salt)), value)
    except (ValueError, TypeError):
        return False


def throttle(action: str, peer: str, email: str) -> bool:
    """Durable fixed-window throttles; trust the socket peer, never client headers."""
    now = int(time.time())
    window = now // 900 * 900
    allowed = True
    with db.get_conn() as conn:
        for label, value, limit in (("peer", peer, 30), ("email", email, 10)):
            bucket = token_hash(action + ":" + label + ":" + value)
            conn.execute(
                """INSERT INTO saas_auth_attempts(bucket, window_start, attempts)
                VALUES (?, ?, 1) ON CONFLICT(bucket) DO UPDATE SET
                attempts=CASE WHEN saas_auth_attempts.window_start = ?
                    THEN saas_auth_attempts.attempts + 1 ELSE 1 END,
                window_start=?""", (bucket, window, window, window),
            )
            row = conn.execute("SELECT attempts FROM saas_auth_attempts WHERE bucket=?", (bucket,)).fetchone()
            allowed = allowed and int(row["attempts"]) <= limit
        conn.execute("DELETE FROM saas_auth_attempts WHERE window_start < ?", (window - 900,))
    return allowed


def create_account(email: str, password: str, invitation: str) -> dict:
    account_id = str(uuid.uuid4())
    digest = password_digest(password)
    with db.get_conn() as conn:
        conn.execute("INSERT INTO saas_accounts(id,email,password_digest,created_at) VALUES(?,?,?,?)",
                     (account_id, email, digest, int(time.time())))
        claimed = conn.execute("""UPDATE saas_invitations SET used_account_id=?
            WHERE token_sha256=? AND used_account_id IS NULL AND expires_at>?""",
            (account_id, token_hash(invitation), int(time.time())))
        if claimed.rowcount != 1:
            raise InvitationRejected("saas_invitation_rejected")
    return {"id": account_id, "email": email}


def authenticate(email: str, password: str) -> dict | None:
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM saas_accounts WHERE email=?", (email,)).fetchone()
    # Unknown accounts perform the same expensive KDF to reduce timing leakage.
    digest = str(row["password_digest"]) if row else "scrypt:131072:8:1:" + "00" * 16 + ":" + "00" * 32
    valid = check_password(password, digest)
    return {"id": row["id"], "email": row["email"]} if row and valid else None


def issue_session(account_id: str, seconds: int, previous: str = "") -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    with db.get_conn() as conn:
        conn.execute("DELETE FROM saas_sessions WHERE expires_at <= ?", (int(time.time()),))
        if previous:
            conn.execute("DELETE FROM saas_sessions WHERE token_sha256=?", (token_hash(previous),))
        conn.execute("INSERT INTO saas_sessions VALUES(?,?,?,?)",
                     (token_hash(token), account_id, csrf, int(time.time()) + seconds))
    return token, csrf


def read_session(token: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    with db.get_conn() as conn:
        row = conn.execute("""SELECT a.id,a.email,s.csrf_token FROM saas_sessions s
            JOIN saas_accounts a ON a.id=s.account_id
            WHERE s.token_sha256=? AND s.expires_at>?""", (token_hash(token), int(time.time()))).fetchone()
    return dict(row) if row else None


def revoke_session(token: str) -> None:
    with db.get_conn() as conn:
        conn.execute("DELETE FROM saas_sessions WHERE token_sha256=?", (token_hash(token),))


def owns_project(account_id: str, project_id: str) -> bool:
    with db.get_conn() as conn:
        row = conn.execute("SELECT 1 FROM saas_project_owners WHERE account_id=? AND project_id=?",
                           (account_id, project_id)).fetchone()
    return row is not None
