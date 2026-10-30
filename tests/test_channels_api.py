from octopus.channels.base import Button, Reply, SendResult
from octopus.models import IncomingMessage
from octopus.router import RouteKind, classify


def test_reply_defaults():
    assert Reply("x").buttons == []
    r = Reply("x", [Button("改成提问", "fix:1", "回复“问”改成提问")])
    assert r.buttons[0].fallback == "回复“问”改成提问"
    assert SendResult(True).message_id is None


def test_bare_correction_words_route():
    for word in ("存", "问", " 问 "):
        r = classify(IncomingMessage(channel="tg", user_id="u", text=word))
        assert r.kind is RouteKind.CORRECTION and r.text == word.strip()
    assert classify(IncomingMessage(channel="tg", user_id="u", text="存一下")).kind is RouteKind.QUESTION


def test_attachment_voice_flag_round_trips():
    from pathlib import Path

    from octopus.models import Attachment, attachment_from_dict, attachment_to_dict

    a = Attachment(kind="audio", path=Path("/x/v.ogg"), filename="v.ogg", voice=True)
    assert attachment_to_dict(a)["voice"] is True
    assert attachment_from_dict(attachment_to_dict(a)) == a
    old = attachment_to_dict(Attachment(kind="audio"))
    del old["voice"]  # a job queued before P5
    assert attachment_from_dict(old).voice is False


def test_incoming_callback_field():
    assert IncomingMessage(channel="tg", user_id="u", callback="fix:3").callback == "fix:3"


# ---- final review M1: one safe_filename for every channel ----

def test_safe_filename_bare_clean_and_bounded():
    from octopus.channels.base import safe_filename

    assert safe_filename("../../evil.txt", "file") == "evil.txt"
    assert safe_filename("..\\..\\win.txt", "file") == "win.txt"
    for bad in (None, "", ".", "..", "/", "a/..", "\x00\x01"):
        assert safe_filename(bad, "file") == "file"
    assert safe_filename("a\x00b\nc\x7f d.pdf", "file") == "abcd.pdf"
    long = safe_filename("长" * 100 + ".pdf", "file")
    assert len(long.encode("utf-8")) <= 200 and long.endswith(".pdf") and long.startswith("长")
    assert safe_filename("x" * 300 + "." + "y" * 30, "file") == "x" * 200  # absurd suffix: not kept
