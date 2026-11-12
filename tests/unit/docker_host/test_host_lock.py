import errno
import fcntl
import os

import pytest

from harbor_research_runner.docker_host.host_lock import (
    HostLock,
    HostLockUnavailable,
    HostLockUnsupported,
)


def test_second_acquire_refuses_while_first_is_held(tmp_path):
    path = tmp_path / "host.lock"
    first = HostLock(path)
    second = HostLock(path)
    first.acquire()

    with pytest.raises(HostLockUnavailable):
        second.acquire()

    first.release()
    second.acquire()
    second.release()


def test_acquire_is_idempotent_for_the_holder(tmp_path):
    lock = HostLock(tmp_path / "host.lock")

    lock.acquire()
    first_held = lock.held
    lock.acquire()

    assert first_held and lock.held
    lock.release()


def test_lock_inode_survives_release(tmp_path):
    path = tmp_path / "host.lock"
    lock = HostLock(path)
    lock.acquire()
    inode = path.stat().st_ino
    lock.release()

    assert path.exists()
    assert path.stat().st_ino == inode
    lock.acquire()
    assert path.stat().st_ino == inode
    lock.release()


def test_unsupported_flock_is_refused_and_descriptor_is_closed(tmp_path):
    descriptors = []

    def unsupported(fd, operation):
        descriptors.append(fd)
        raise OSError(errno.EOPNOTSUPP, "unsupported")

    lock = HostLock(tmp_path / "host.lock", flock=unsupported)

    with pytest.raises(HostLockUnsupported):
        lock.acquire()

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_probe_detects_a_filesystem_without_advisory_locks(tmp_path):
    lock = HostLock(tmp_path / "host.lock", flock=lambda fd, operation: None)

    with pytest.raises(HostLockUnsupported, match="does not enforce advisory locks"):
        lock.acquire()

    assert not lock.held


def test_descriptor_is_not_inheritable(tmp_path):
    inheritable = []

    def recording_flock(fd, operation):
        inheritable.append(os.get_inheritable(fd))
        fcntl.flock(fd, operation)

    lock = HostLock(tmp_path / "host.lock", flock=recording_flock)
    lock.acquire()

    assert inheritable and not any(inheritable)
    lock.release()
