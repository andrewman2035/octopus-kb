from octopus.db import connect
from octopus.outbox import Outbox


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_outbox_take_in_order_and_expiry(tmp_path):
    clock = Clock()
    o = Outbox(connect(tmp_path / "o.db", embed_dim=8), clock=clock)
    o.add("clawbot", "u", "一")
    clock.now += 1
    o.add("clawbot", "u", "二")
    o.add("clawbot", "other", "x")
    assert o.count() == 3
    assert o.take("clawbot", "u") == ["一", "二"]
    assert o.take("clawbot", "u") == []
    clock.now += 8 * 86400
    assert o.take("clawbot", "other") == []  # older than 7 days: dropped
    assert o.count() == 0


def test_outbox_add_prunes_and_keeps_given_created_at(tmp_path):
    clock = Clock()
    o = Outbox(connect(tmp_path / "o.db", embed_dim=8), clock=clock)
    o.add("clawbot", "u", "旧")
    clock.now += 8 * 86400
    o.add("clawbot", "other", "新")
    assert o.count() == 1  # the expired row was pruned by add
    o.add("clawbot", "u", "重发", created_at=clock.now - 7 * 86400 + 10)  # keeps the original age
    assert o.take_entries("clawbot", "u") == [("重发", clock.now - 7 * 86400 + 10)]
    o.add("clawbot", "u", "重发", created_at=clock.now - 7 * 86400 + 10)
    clock.now += 11
    assert o.take("clawbot", "u") == []  # its TTL did not restart


def test_outbox_pending_users(tmp_path):
    clock = Clock()
    o = Outbox(connect(tmp_path / "o.db", embed_dim=8), clock=clock)
    assert o.pending_users() == []
    o.add("clawbot", "u", "一")
    o.add("tg", "me", "x")
    o.add("clawbot", "u", "二")
    assert sorted(o.pending_users()) == [("clawbot", "u"), ("tg", "me")]
    clock.now += 8 * 86400
    assert o.pending_users() == []  # expired rows are nobody's
