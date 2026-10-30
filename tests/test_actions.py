from octopus.db import connect
from octopus.actions import Actions


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_actions_record_get_latest_consume(tmp_path):
    clock = Clock()
    a = Actions(connect(tmp_path / "a.db", embed_dim=8), clock=clock)
    first = a.record("tg", "u", "note", "复利笔记", "20261013-aaaaaa")
    clock.now += 5
    second = a.record("tg", "u", "question", "什么是复利？")
    assert a.get(first, "tg", "u")["entry_id"] == "20261013-aaaaaa"
    assert a.get(first, "tg", "other") is None  # other users can't use it
    assert a.latest("tg", "u")["id"] == second
    a.consume(second)
    assert a.latest("tg", "u")["id"] == first
    clock.now += 601
    assert a.latest("tg", "u") is None


def test_actions_expire_after_seven_days(tmp_path):
    clock = Clock()
    conn = connect(tmp_path / "a.db", embed_dim=8)
    a = Actions(conn, clock=clock)
    old = a.record("tg", "u", "note", "旧笔记", "20261013-aaaaaa")
    clock.now += 7 * 86400 + 1
    assert a.get(old, "tg", "u") is None  # too old to flip
    a.record("tg", "u", "question", "新问题")
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 1  # record pruned the old one
