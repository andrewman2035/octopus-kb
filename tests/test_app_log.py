"""P16: one INFO line per incoming message and per reply sent, so "did it arrive?" can be answered from the log."""
import logging

import pytest
from fakes import FakeChannel
from test_app import make_app

from octopus.app import ERROR_REPLY
from octopus.channels.base import SendResult, log_preview, user_tag
from octopus.models import Attachment, IncomingMessage

USER = "wx-owner-7f3a9c@im.wechat"  # never in the log as it is
TAG = user_tag(USER)


@pytest.fixture
def app(tmp_path):
    app, ch, _ = make_app(tmp_path)
    app.cfg.channels["tg"].allowed_users.append(USER)
    return app


def msg(text="", attachments=None, callback=None, reply_to=None):
    return IncomingMessage(channel="tg", user_id=USER, text=text, attachments=attachments or [], callback=callback,
                           reply_to_id=reply_to)


def lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.name == "octopus.app" and r.getMessage().startswith(prefix)]


@pytest.fixture(autouse=True)
def info(caplog):
    caplog.set_level(logging.INFO)


def test_user_tag_is_short_stable_and_not_the_id():
    assert len(TAG) == 4 and TAG == user_tag(USER) and TAG not in USER and user_tag("other") != TAG


def test_preview_is_one_line_capped_and_shows_only_the_host_of_a_link():
    assert log_preview("第一行\n第二行") == "第一行 第二行"
    assert log_preview("一" * 30) == "一" * 20 + "…"
    shown = log_preview("看 https://github.com/owner/secret-repo?token=x")
    assert shown == "看 链接 github.com" and "secret" not in shown
    assert log_preview("mp.weixin.qq.com/s/abcdef") == "链接 mp.weixin.qq.com"


async def test_a_question_logs_one_in_line_and_one_out_line(app, caplog):
    await app.handle(msg("复利有什么用？\n说说看"))
    assert lines(caplog, "in ") == [f'in tg {TAG} question 11字 "复利有什么用？ 说说看"']
    out = lines(caplog, "out ")
    assert len(out) == 1 and out[0].startswith(f"out tg {TAG} ") and out[0].endswith(" ok")
    assert USER not in caplog.text


async def test_a_link_logs_its_host_only(app, caplog):
    text = "看看 https://a.com/private/path?utm_source=wx"
    await app.handle(msg(text))
    assert lines(caplog, "in ") == [f'in tg {TAG} save_urls {len(text)}字 "看看 链接 a.com"']
    assert "private" not in caplog.text


async def test_commands_files_quotes_and_windows(app, caplog, tmp_path):
    await app.handle(msg("/new"))
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    await app.handle(msg("", [Attachment(kind="image", path=img), Attachment(kind="image", path=img)]))
    await app.handle(msg("/s 一条笔记", reply_to="m9"))
    assert lines(caplog, "in ") == [f'in tg {TAG} command/new 4字 "/new"',
                                    f"in tg {TAG} save_attachments 0字 +2图 window=new",
                                    f'in tg {TAG} save_note 7字 quote window=new "/s 一条笔记"']


async def test_first_text_and_done_close_the_window(app, caplog):
    await app.handle(msg("/s 一条已有的笔记"))
    await app.process_due_jobs()
    x = app.focus.get("tg", USER)
    caplog.clear()
    await app.handle(msg(f"/add {x}"))
    await app.handle(msg("补充一段文字"))
    await app.handle(msg("好了"))
    await app.handle(msg("好了"))
    add = f"/add {x}"
    assert lines(caplog, "in ") == [f'in tg {TAG} command/add {len(add)}字 "{log_preview(add)}"',
                                    f'in tg {TAG} question 6字 window=add -> first_text "补充一段文字"',
                                    f'in tg {TAG} command/done 2字 window=add "好了"',
                                    f"in tg {TAG} -> window add closed by done",
                                    f'in tg {TAG} question 2字 "好了"']  # no window open: chat


async def test_correction_with_nothing_to_correct(app, caplog):
    await app.handle(msg("存"))
    assert lines(caplog, "in ") == [f'in tg {TAG} correction/存 1字 "存"',
                                    f"in tg {TAG} -> nothing to correct (no-latest)"]


async def test_taps_are_logged(app, caplog):
    await app.handle(msg(callback="fix:99"))
    assert lines(caplog, "in ") == [f"in tg {TAG} tap/fix:99", f"in tg {TAG} -> nothing to correct (stale-button)"]


async def test_at_id_unresolved(app, caplog):
    await app.handle(msg("@b305ad 讲了什么"))
    got = lines(caplog, "in ")
    assert got[0].startswith(f"in tg {TAG} question/@id ") and got[1] == f"in tg {TAG} -> @id unresolved (1)"


async def test_voice_has_no_preview_its_words_get_their_own_line(app, caplog):
    await app.handle(msg("", [Attachment(kind="audio", filename="voice.silk", voice=True, text="存")]))
    assert lines(caplog, "in ")[:2] == [f"in tg {TAG} voice 0字 +1文件", f'in tg {TAG} correction/存 1字 "存"']


class Holding(FakeChannel):
    def __init__(self, outcome):
        super().__init__("tg")
        self.outcome = outcome

    async def send(self, user_id, reply):
        if self.outcome == "raise":
            raise RuntimeError("down")
        return SendResult(False)


@pytest.mark.parametrize("outcome, status", [("held", "held"), ("raise", "failed")])
async def test_out_lines_say_when_a_reply_did_not_go_out(tmp_path, caplog, outcome, status):
    app, ch, _ = make_app(tmp_path, channel=Holding(outcome))
    app.cfg.channels["tg"].allowed_users.append(USER)
    await app.handle(msg("/help"))
    out = lines(caplog, "out ")
    assert len(out) == 1 and out[0].startswith(f"out tg {TAG} ") and " buttons=" in out[0]
    assert out[0].endswith(f" {status}")
    ch.outcome = None
    app.channels["tg"] = FakeChannel("tg")  # reachable again: the held reply goes out first, marked as a resend
    await app.handle(msg("/help"))
    assert lines(caplog, "out ")[1].endswith(" outbox ok")


async def test_ignored_and_failed_messages_log_no_raw_id(tmp_path, caplog):
    app, ch, _ = make_app(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id=USER, text="hi"))
    assert lines(caplog, "in ") == [f"in tg {TAG} ignored: not allowed"]
    assert USER not in caplog.text


# ---- fix round 1 ----

BAD_LINK = "http://[服务器IP]:8080/admin"  # a placeholder in pasted tutorials: urlsplit raises on it


def test_preview_never_raises_on_a_link_urlsplit_rejects():
    assert log_preview("http://[abc/def") == "链接"
    assert log_preview(f"见 {BAD_LINK}") == "见 链接"
    assert log_preview("a\x1b[31mb\x00c") == "a [31mb c"  # no control characters reach the log


async def test_a_note_with_such_a_link_is_saved(app, caplog):
    await app.handle(msg(f"/s 笔记 {BAD_LINK} 很长的一段说明文字"))
    await app.process_due_jobs()
    sent = [t for _, t in app.channels["tg"].sent]
    assert ERROR_REPLY not in sent and sent[-1].startswith("✅ 已保存")


async def test_an_answer_with_such_a_link_is_sent_once(tmp_path, caplog):
    app, ch, _ = make_app(tmp_path, answer_reply=f"打开 {BAD_LINK} [1]。")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="后台地址是什么"))
    assert len(ch.sent) == 1 and ch.sent[0][1].startswith("打开 http://[服务器IP]")
    assert lines(caplog, "out ")[0].endswith(" ok")


async def test_a_held_reply_with_such_a_link_stays_in_the_outbox(tmp_path, caplog):
    app, ch, _ = make_app(tmp_path, channel=Holding("held"), answer_reply=f"打开 {BAD_LINK} [1]。")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="后台地址是什么"))
    assert [t for _, t, _ in app.outbox.queued("tg", "me")][0].startswith("打开 http://[服务器IP]")


async def test_a_failing_log_line_never_changes_what_happens(app, caplog, monkeypatch):
    def broken(text):
        raise RuntimeError("log helper bug")

    monkeypatch.setattr("octopus.app.log_preview", broken)
    monkeypatch.setattr("octopus.app.out_line", broken)
    await app.handle(msg("/help"))
    await app.handle(msg("复利有什么用？"))
    sent = [t for _, t in app.channels["tg"].sent]
    assert len(sent) == 2 and ERROR_REPLY not in sent and app.outbox.queued("tg", USER) == []


@pytest.mark.parametrize("text", ["/s复利需要时间，我的银行卡密码是123456", "/https://a.com/p?token=secret",
                                  "/users/me/私人/日记2026.txt"])
async def test_an_unknown_command_logs_no_name(app, caplog, text):
    await app.handle(msg(text))
    [line] = lines(caplog, "in ")
    assert line.startswith(f"in tg {TAG} command/unknown {len(text)}字 ")
    assert text not in caplog.text and "123456" not in caplog.text and "secret" not in caplog.text


async def test_known_commands_and_aliases_keep_their_name(app, caplog):
    await app.handle(msg("/recent"))
    await app.handle(msg("/del"))
    await app.handle(msg("/S 一条"))
    assert [line.split()[3] for line in lines(caplog, "in ")] == ["command/recent", "command/del", "save_note"]


@pytest.mark.parametrize("reason, status", [("budget", "held(budget)"), ("no-context", "held(no-context)"),
                                            ("error", "failed(error)"), ("", "held")])
async def test_out_line_says_why_a_reply_did_not_go_out(tmp_path, caplog, reason, status):
    class Refusing(FakeChannel):
        async def send(self, user_id, reply):
            return SendResult(False, reason=reason)

    app, ch, _ = make_app(tmp_path, channel=Refusing("tg"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="/recent"))
    assert lines(caplog, "out ")[0].endswith(f" {status}")


async def test_nothing_to_correct_says_why(tmp_path, caplog):
    from test_app_manage import make_app as manage_app
    from test_app_manage import save

    app, ch, state = manage_app(tmp_path)
    me = user_tag("me")
    eid = await save(app, "复利需要时间")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="存"))  # the last one is a note
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="复利是什么"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="", callback=f"del:{eid}"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="存"))  # the answer is not the last turn
    outcomes = [line for line in lines(caplog, "in ") if " -> nothing" in line]
    assert outcomes == [f"in tg {me} -> nothing to correct (kind-mismatch)",
                        f"in tg {me} -> nothing to correct (not-last-turn)"]


async def test_confirmation_answers_are_logged(tmp_path, caplog):
    import time

    from test_app_manage import ask_delete
    from test_app_manage import make_app as manage_app

    from octopus.ops import OP_TTL_SECONDS

    me = user_tag("me")
    (tmp_path / "a").mkdir()
    app, ch, state = manage_app(tmp_path / "a")
    await ask_delete(app, ch, state)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="嗯哼"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="确认"))
    (tmp_path / "b").mkdir()
    app, ch, state = manage_app(tmp_path / "b")
    await ask_delete(app, ch, state)
    app.jobs.enqueue("note", {"text": "苹果很好吃"}, "tg", "me")  # a save finishes after the question
    await app.process_due_jobs()
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="好的。"))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="取消"))
    (tmp_path / "c").mkdir()
    app, ch, state = manage_app(tmp_path / "c")
    await ask_delete(app, ch, state)
    later = time.time() + OP_TTL_SECONDS + 1
    app.ops.clock = lambda: later
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="确认。"))
    got = [line for line in lines(caplog, "in ") if line.split()[3].startswith("answer/")]
    assert got == [f'in tg {me} answer/unclear 2字 "嗯哼"', f'in tg {me} answer/确认 2字 "确认"',
                   f'in tg {me} answer/确认 3字 -> asked again "好的。"', f'in tg {me} answer/取消 2字 "取消"',
                   f'in tg {me} answer/确认 3字 -> expired "确认。"']


async def test_empty_message_unresolved_ids_and_too_many(tmp_path, caplog):
    from test_app_manage import make_app as manage_app
    from test_app_manage import save

    me = user_tag("me")
    app, ch, state = manage_app(tmp_path)
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=""))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="20261011-b305ae 讲了什么"))
    ids = [await save(app, f"第{i}条笔记") for i in range(4)]
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=" ".join(f"@{i}" for i in ids) + " 有什么不同"))
    got = lines(caplog, "in ")
    assert got[0] == f"in tg {me} nothing 0字"
    assert f"in tg {me} -> ids unresolved, not answered" in got and f"in tg {me} -> @id too many" in got


async def test_files_already_saved_and_paste_material(tmp_path, caplog):
    from test_app_media import m as media_m
    from test_app_media import make_app as media_app
    from test_app_media import staged
    from test_app_paste import EXCERPT, _failed_link

    me = user_tag("me")
    (tmp_path / "a").mkdir()
    app, ch, _ = media_app(tmp_path / "a")
    await app.handle(media_m("/new"))
    await app.handle(media_m("", [staged(tmp_path / "a", "a.png")]))
    await app.process_due_jobs()
    await app.handle(media_m("把上面的图存成一篇"))
    assert lines(caplog, "in ")[-1] == f'in tg {me} question 9字 window=new -> files already saved "把上面的图存成一篇"'
    (tmp_path / "b").mkdir()
    app, ch, _ = media_app(tmp_path / "b")
    await _failed_link(app, ch)
    await app.handle(media_m(EXCERPT))
    assert lines(caplog, "in ")[-1] == f"in tg {me} -> material for the window's entry, not a question"


async def test_tap_data_cannot_break_the_log_line(app, caplog):
    await app.handle(msg(callback="fix:1\nINFO fake line"))
    assert lines(caplog, "in ")[0] == f"in tg {TAG} tap/fix:1 INFO fake line"


async def test_a_fetch_failure_logs_the_host_only(tmp_path, caplog):
    from fakes import StubFetcher

    from octopus.fetchers.base import FetchError

    app, ch, _ = make_app(tmp_path, StubFetcher(FetchError("网络错误", retryable=True)))
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="https://a.com/private?token=s"))
    await app.process_due_jobs()
    assert "could not fetch a.com" in caplog.text and "private" not in caplog.text
