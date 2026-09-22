"""Persistence adapter package.

Concrete adapters are imported from their explicit modules so selecting a
production repository never loads SQLite-only test adapters as a side effect.
"""

__all__: list[str] = []
