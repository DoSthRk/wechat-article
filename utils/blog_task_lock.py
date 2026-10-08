"""Cross-process, reentrant serialization of a Blog article/language pair."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import hashlib
from pathlib import Path
import threading

LOCK_DIR = Path(__file__).resolve().parent.parent / "runtime" / "blog_version_locks"
_local = threading.local()


@contextmanager
def version_lock(job_id, lang):
    key = hashlib.sha256(f"{job_id}\0{lang}".encode()).hexdigest()
    held = getattr(_local, "held", None)
    if held is None:
        held = _local.held = set()
    if key in held:
        yield
        return
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    with (LOCK_DIR / f"{key}.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            fcntl.flock(handle, fcntl.LOCK_UN)


def serialized_version(function):
    @wraps(function)
    def wrapped(self, job_id, lang="en", *args, **kwargs):
        with version_lock(job_id, lang):
            return function(self, job_id, lang, *args, **kwargs)
    return wrapped
