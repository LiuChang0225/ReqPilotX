"""Optional host trust-store initialization for API and worker processes."""

from __future__ import annotations


def configure_host_truststore() -> None:
    try:
        import truststore
    except Exception:
        return
    truststore.inject_into_ssl()
