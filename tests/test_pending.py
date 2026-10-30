from octopus.db import connect
from octopus.pending import FRESH, MERGE_WINDOW_SECONDS, PendingManual


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_pending_window_and_reply(tmp_path):
    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    assert p.active("tg", "u") is None
    p.set("tg", "u", "20261013-aaaaaa", prompt_message_id="m1")
    assert p.active("tg", "u") == "20261013-aaaaaa"
    assert p.for_reply("tg", "u", "m1") == "20261013-aaaaaa"
    assert p.for_reply("tg", "u", "other") is None
    assert p.active("tg", "other-user") is None
    clock.now += MERGE_WINDOW_SECONDS + 1
    assert p.active("tg", "u") is None
    assert p.for_reply("tg", "u", "m1") == "20261013-aaaaaa"  # replying to the prompt works any time
    p.set("tg", "u", "20261013-bbbbbb")
    assert p.active("tg", "u") == "20261013-bbbbbb"  # latest wins
    p.clear("tg", "u")
    assert p.active("tg", "u") is None


def test_every_prompt_keeps_its_entry_for_seven_days(tmp_path):
    # final review I4: a newer prompt must not take over replies to an older one
    from octopus.pending import PROMPT_TTL_SECONDS

    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    p.set("tg", "u", "20261013-xxxxxx", prompt_message_id="m1")
    p.set("tg", "u", "20261013-yyyyyy", prompt_message_id="m2")
    assert p.active("tg", "u") == "20261013-yyyyyy"
    assert p.for_reply("tg", "u", "m1") == "20261013-xxxxxx"
    assert p.for_reply("tg", "u", "m2") == "20261013-yyyyyy"
    assert p.for_reply("tg", "other", "m1") is None and p.for_reply("fs", "u", "m1") is None
    p.clear("tg", "u")
    assert p.for_reply("tg", "u", "m1") == "20261013-xxxxxx"  # the merge window closed, the prompt stays
    assert PROMPT_TTL_SECONDS == 7 * 24 * 3600
    clock.now += PROMPT_TTL_SECONDS + 1
    assert p.for_reply("tg", "u", "m1") is None
    p.set("tg", "u", "20261013-zzzzzz", prompt_message_id="m3")  # recording a prompt drops expired ones
    assert p.conn.execute("SELECT count(*) FROM prompts").fetchone()[0] == 1


def test_fresh_window_has_no_entry_and_expires(tmp_path):
    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    assert not p.fresh("tg", "u") and p.opened_at("tg", "u") is None
    p.set("tg", "u", FRESH)
    assert p.fresh("tg", "u") and p.active("tg", "u") is None and p.opened_at("tg", "u") == clock.now
    clock.now += 5
    p.set("tg", "u", "20261013-aaaaaa")
    assert not p.fresh("tg", "u") and p.active("tg", "u") == "20261013-aaaaaa" and p.opened_at("tg", "u") == clock.now
    p.set("tg", "u", FRESH)
    clock.now += MERGE_WINDOW_SECONDS + 1
    assert not p.fresh("tg", "u") and p.opened_at("tg", "u") is None


def test_window_remembers_its_origin(tmp_path):
    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    assert p.origin("tg", "u") is None
    p.set("tg", "u", FRESH, origin="new")
    p.set("tg", "u", "20261013-aaaaaa")  # the first files after /new: still a /new window
    assert p.origin("tg", "u") == "new"
    p.set("tg", "u", "20261013-bbbbbb", origin="add")
    assert p.origin("tg", "u") == "add"
    clock.now += MERGE_WINDOW_SECONDS + 1
    assert p.origin("tg", "u") is None
    p.set("tg", "u", "20261013-cccccc", prompt_message_id="m1")  # nothing open to keep: a link's window
    assert p.origin("tg", "u") == "link"


# ---- P8: /new, /add and auto windows end after 5 idle minutes; a link's window keeps 30 ----

def test_file_windows_end_after_five_idle_minutes(tmp_path):
    from octopus.pending import AUTO, FILES_WINDOW_SECONDS

    assert FILES_WINDOW_SECONDS == 300 and MERGE_WINDOW_SECONDS == 1800
    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    for origin in ("new", "add", AUTO):
        p.set("tg", "u", "20261013-aaaaaa", origin=origin)
        clock.now += FILES_WINDOW_SECONDS - 1
        assert p.active("tg", "u") == "20261013-aaaaaa" and p.origin("tg", "u") == origin
        clock.now += 2
        assert p.active("tg", "u") is None and p.opened_at("tg", "u") is None
    p.set("tg", "u", FRESH, origin="new")
    clock.now += FILES_WINDOW_SECONDS + 1
    assert not p.fresh("tg", "u")
    p.set("tg", "u", "20261013-bbbbbb", prompt_message_id="m1", origin="link")
    clock.now += MERGE_WINDOW_SECONDS - 1
    assert p.active("tg", "u") == "20261013-bbbbbb"
    p.conn.execute("UPDATE pending_manual SET origin = NULL")  # a row from before origins: a link's 30 minutes
    assert p.active("tg", "u") == "20261013-bbbbbb"
    clock.now += 2
    assert p.active("tg", "u") is None


def test_touch_restarts_the_open_window(tmp_path):
    from octopus.pending import AUTO, FILES_WINDOW_SECONDS

    clock = Clock()
    p = PendingManual(connect(tmp_path / "p.db", embed_dim=8), clock=clock)
    p.set("tg", "u", "20261013-aaaaaa", prompt_message_id="m1", origin=AUTO)
    clock.now += FILES_WINDOW_SECONDS - 1
    p.touch("tg", "u")
    clock.now += FILES_WINDOW_SECONDS - 1
    assert p.active("tg", "u") == "20261013-aaaaaa" and p.origin("tg", "u") == AUTO
    assert p.for_reply("tg", "u", "m1") == "20261013-aaaaaa"


# ---- P15: an /add window takes one text; whether it has is kept in the row ----

def test_add_window_remembers_its_text_until_a_new_window_opens(tmp_path):
    from octopus.pending import ADD, NEW

    clock = Clock()
    conn = connect(tmp_path / "p.db", embed_dim=8)
    p = PendingManual(conn, clock=clock)
    assert not p.text_added("tg", "u")
    p.set("tg", "u", "20261013-aaaaaa", origin=ADD)
    assert not p.text_added("tg", "u")
    p.mark_text_added("tg", "u")
    assert p.text_added("tg", "u")
    p.set("tg", "u", "20261013-aaaaaa")  # a merge finishing restarts the window: same window, text still added
    assert p.text_added("tg", "u") and p.origin("tg", "u") == ADD
    assert PendingManual(conn, clock=clock).text_added("tg", "u")  # a restart reads it from the row
    p.set("tg", "u", "20261013-aaaaaa", origin=ADD)  # /add again: a new window
    assert not p.text_added("tg", "u")
    p.mark_text_added("tg", "u")
    p.set("tg", "u", FRESH, origin=NEW)
    assert not p.text_added("tg", "u")
    p.mark_text_added("tg", "u")
    p.clear("tg", "u")
    assert not p.text_added("tg", "u")
