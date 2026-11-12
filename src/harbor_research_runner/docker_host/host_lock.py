import contextlib
import errno
import logging
import os
from collections.abc import Callable
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None

if fcntl is None:
    _LOCK_EX = 0
    _LOCK_NB = 0
    _LOCK_UN = 0
else:
    _LOCK_EX = fcntl.LOCK_EX
    _LOCK_NB = fcntl.LOCK_NB
    _LOCK_UN = fcntl.LOCK_UN


HOST_LOCK_PATH = Path("/var/tmp/hrr-host.lock")
_UNAVAILABLE_ERRNOS = {errno.EAGAIN, errno.EACCES}
_UNSUPPORTED_ERRNOS = {
    errno.ENOSYS,
    errno.EOPNOTSUPP,
    errno.ENOTSUP,
    errno.ENOLCK,
}
logger = logging.getLogger(__name__)


class HostLockUnavailable(Exception):
    pass


class HostLockUnsupported(Exception):
    pass


class HostLock:
    def __init__(
        self,
        path: Path = HOST_LOCK_PATH,
        *,
        flock: Callable[[int, int], None] | None = None,
    ) -> None:
        if fcntl is None:
            raise HostLockUnsupported("advisory locks are unavailable on this platform")
        self.path = path
        selected_flock = flock if flock is not None else fcntl.flock
        self._flock = selected_flock
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self.held:
            return
        fd = os.open(self.path, os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            os.set_inheritable(fd, False)
            with contextlib.suppress(PermissionError):
                os.utime(self.path)
            self._lock(fd)
            self._probe()
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        logger.debug("Acquired host lock at %s", self.path)

    def _lock(self, fd: int) -> None:
        try:
            self._flock(fd, _LOCK_EX | _LOCK_NB)
        except BlockingIOError as exc:
            raise HostLockUnavailable(f"host lock is held: {self.path}") from exc
        except OSError as exc:
            if exc.errno in _UNAVAILABLE_ERRNOS:
                raise HostLockUnavailable(f"host lock is held: {self.path}") from exc
            if exc.errno in _UNSUPPORTED_ERRNOS:
                raise HostLockUnsupported(f"advisory locks are unsupported: {self.path}") from exc
            raise

    def _probe(self) -> None:
        fd = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.set_inheritable(fd, False)
            try:
                self._flock(fd, _LOCK_EX | _LOCK_NB)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in _UNAVAILABLE_ERRNOS:
                    return
                if exc.errno in _UNSUPPORTED_ERRNOS:
                    raise HostLockUnsupported(
                        f"advisory locks are unsupported: {self.path}"
                    ) from exc
                raise
            raise HostLockUnsupported(f"filesystem does not enforce advisory locks: {self.path}")
        finally:
            os.close(fd)

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            self._flock(fd, _LOCK_UN)
        finally:
            os.close(fd)
        logger.debug("Released host lock at %s", self.path)

    def __enter__(self) -> "HostLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
