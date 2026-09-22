from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


# The desktop/local runtime already enforces one active storage owner. Keeping
# one native document build in flight prevents the legacy preview and the new
# page-addressable Shadow run from doubling PDF memory and CPU pressure.
_DOCUMENT_PARSE_SLOT = threading.BoundedSemaphore(value=1)


@contextmanager
def document_parse_slot() -> Iterator[float]:
    started = time.perf_counter()
    _DOCUMENT_PARSE_SLOT.acquire()
    wait_ms = round((time.perf_counter() - started) * 1000, 1)
    try:
        yield wait_ms
    finally:
        _DOCUMENT_PARSE_SLOT.release()


__all__ = ["document_parse_slot"]
