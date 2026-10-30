from octopus.db import connect
from octopus.focus import FOCUS_TTL_SECONDS, Focus


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path):
    clock = Clock()
    return Focus(connect(tmp_path / "f.db", embed_dim=8), clock=clock), clock


def test_set_get_overwrite(tmp_path):
    f, _ = make(tmp_path)
    assert f.get("tg", "u") is None
    f.set("tg", "u", "20261013-aaaaaa")
    assert f.get("tg", "u") == "20261013-aaaaaa"
    f.set("tg", "u", "20261013-bbbbbb")
    assert f.get("tg", "u") == "20261013-bbbbbb"


def test_per_channel_and_user(tmp_path):
    f, _ = make(tmp_path)
    f.set("tg", "u", "20261013-aaaaaa")
    assert f.get("fs", "u") is None and f.get("tg", "v") is None


def test_ttl(tmp_path):
    f, clock = make(tmp_path)
    f.set("tg", "u", "20261013-aaaaaa")
    assert FOCUS_TTL_SECONDS == 86400
    clock.now += FOCUS_TTL_SECONDS - 1
    assert f.get("tg", "u") == "20261013-aaaaaa"
    clock.now += 2
    assert f.get("tg", "u") is None


def test_clear_only_matching(tmp_path):
    f, _ = make(tmp_path)
    f.set("tg", "u", "20261013-aaaaaa")
    f.clear("tg", "u", "20261013-other0")
    assert f.get("tg", "u") == "20261013-aaaaaa"
    f.clear("tg", "u", "20261013-aaaaaa")
    assert f.get("tg", "u") is None
    f.set("tg", "u", "20261013-aaaaaa")
    f.clear("tg", "u")
    assert f.get("tg", "u") is None
