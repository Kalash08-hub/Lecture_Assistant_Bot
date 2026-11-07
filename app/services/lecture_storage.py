"""Per-user upload serialization and deferred deletion for in-flight lectures."""
import asyncio
import logging
from collections import Counter, defaultdict
from pathlib import Path

locks = defaultdict(asyncio.Lock)
_in_use = Counter()
_retired = set()


def retain(path):
    _in_use[path] += 1


def retire(path):
    if path:
        _retired.add(path)
        _cleanup(path)


def release(path):
    _in_use[path] -= 1
    if not _in_use[path]:
        del _in_use[path]
    _cleanup(path)


def _cleanup(path):
    if path in _retired and not _in_use[path]:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logging.getLogger(__name__).warning('Could not remove retired lecture')
        else:
            _retired.discard(path)
