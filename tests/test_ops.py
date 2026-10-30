from octopus.db import connect
from octopus.ops import OP_TTL_SECONDS, Ops


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path):
    clock = Clock()
    return Ops(connect(tmp_path / "o.db", embed_dim=8), clock=clock), clock


def test_add_and_take_once(tmp_path):
    ops, _ = make(tmp_path)
    op_id = ops.add("tg", "me", "delete", ["20261013-aaaaaa", "20261013-bbbbbb"], "")
    op = ops.take(op_id, "tg", "me")
    assert op["kind"] == "delete" and op["entry_ids"] == ["20261013-aaaaaa", "20261013-bbbbbb"]
    assert op["arg"] == "" and op["id"] == op_id
    assert ops.take(op_id, "tg", "me") is None  # used up


def test_take_by_another_user_or_channel(tmp_path):
    ops, _ = make(tmp_path)
    op_id = ops.add("tg", "me", "retitle", ["20261013-aaaaaa"], "新标题")
    assert ops.take(op_id, "tg", "other") is None
    assert ops.take(op_id, "fs", "me") is None
    assert ops.take(op_id, "tg", "me")["arg"] == "新标题"  # still there for its owner


def test_expired(tmp_path):
    ops, clock = make(tmp_path)
    op_id = ops.add("tg", "me", "delete", ["20261013-aaaaaa"], "")
    clock.now += OP_TTL_SECONDS + 1
    assert ops.latest("tg", "me") is None
    assert ops.take(op_id, "tg", "me") is None


def test_latest_and_drop(tmp_path):
    ops, clock = make(tmp_path)
    assert ops.latest("tg", "me") is None
    first = ops.add("tg", "me", "delete", ["20261013-aaaaaa"], "")
    clock.now += 1
    second = ops.add("tg", "me", "annotate", ["20261013-bbbbbb"], "备注")
    ops.add("tg", "other", "delete", ["20261013-cccccc"], "")
    latest = ops.latest("tg", "me")
    assert latest["id"] == second and latest["entry_ids"] == ["20261013-bbbbbb"]
    ops.drop(second)
    assert ops.latest("tg", "me")["id"] == first


def test_ids_are_never_reused(tmp_path):
    ops, _ = make(tmp_path)
    first = ops.add("tg", "me", "delete", ["20261013-aaaaaa"], "")
    ops.drop(first)
    assert ops.add("tg", "me", "delete", ["20261013-aaaaaa"], "") > first
