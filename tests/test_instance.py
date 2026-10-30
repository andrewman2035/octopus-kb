from octopus.instance import InstanceLock


def test_second_lock_fails_until_released(tmp_path):
    a, b = InstanceLock(tmp_path / "octopus.lock"), InstanceLock(tmp_path / "octopus.lock")
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_a_lock_that_fails_otherwise_closes_its_file_and_stays_free(tmp_path, monkeypatch):
    # P3 deferred minor: an OSError other than "held by another process" leaked the open file (and the lock)
    import errno
    import os

    import pytest

    from octopus import instance

    opened = []
    real_open = os.open

    def spy_open(*a, **kw):
        opened.append(real_open(*a, **kw))
        return opened[-1]

    def no_write(fd, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(instance.os, "open", spy_open)
    monkeypatch.setattr(instance.os, "write", no_write)
    lock = InstanceLock(tmp_path / "octopus.lock")
    with pytest.raises(OSError):
        lock.acquire()
    with pytest.raises(OSError):
        os.fstat(opened[0])  # closed
    monkeypatch.undo()
    other = InstanceLock(tmp_path / "octopus.lock")
    assert other.acquire() is True  # not left locked
    other.release()
