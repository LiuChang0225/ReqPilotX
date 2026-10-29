"""Expose app factories lazily so local tools do not initialize DB settings."""

__all__ = ["create_app", "create_http_app"]


def __getattr__(name):
    if name in __all__:
        from . import app_factory
        return getattr(app_factory, name)
    raise AttributeError(name)
