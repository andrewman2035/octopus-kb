"""P25 Part B: English command words and management phrases work in every language setting, mirroring the Chinese
ones; every English word the help and the hints tell people to type does what they say (review m1)."""
import json
import re
from datetime import date

import pytest
from fakes import FakeChannel, FakeEmbedder, FakeProvider
from test_app_media import make_app as media_app
from test_app_media import m, staged

from octopus import i18n
from octopus.app import App, about_files_sent, done_word, typed_answer
from octopus.config import ChannelConfig, make_test_config
from octopus.db import connect
from octopus.fetchers import FetcherRegistry
from octopus.i18n import en
from octopus.index import Index
from octopus.library import Library
from octopus.llm.router import LLMRouter
from octopus.manage import wants_manage
from octopus.models import IncomingMessage
from octopus.router import RouteKind, bare_word, classify
from octopus.settings import Settings


def route(text, attachments=None):
    return classify(IncomingMessage(channel="tg", user_id="me", text=text, attachments=attachments or []))


# ---- the router (mirrors test_router) ----

@pytest.mark.parametrize("text,content", [("save: buy milk tomorrow", "buy milk tomorrow"),
                                          ("save: buy milk", "buy milk"), ("Save, buy milk", "buy milk"),
                                          ("SAVE:buy milk", "buy milk"), ("save ,  two spaces", "two spaces")])
def test_save_prefix_makes_a_note(text, content):
    r = route(text)
    assert r.kind is RouteKind.SAVE_NOTE and r.text == content and not r.pasted


@pytest.mark.parametrize("text", ["save:", "Save :"])
def test_an_empty_save_prefix_is_the_save_command(text):
    r = route(text)
    assert r.kind is RouteKind.COMMAND and r.command == "save"


def test_save_prefix_with_only_a_link_reads_it():
    r = route("save: https://a.com/x")
    assert r.kind is RouteKind.SAVE_URLS and r.urls == ["https://a.com/x"]


@pytest.mark.parametrize("text", ["ask: what is compounding", "Ask:what is compounding", "ASK : what is compounding"])
def test_ask_prefix_is_a_question(text):
    r = route(text)
    assert r.kind is RouteKind.QUESTION and r.text == "what is compounding"


@pytest.mark.parametrize("text,word", [("save", "存"), ("Save.", "存"), ("SAVE!", "存"), ("ask", "问"), ("Ask?", "问")])
def test_bare_save_and_ask_are_corrections(text, word):
    assert bare_word(text) == word
    r = route(text)
    assert r.kind is RouteKind.CORRECTION and r.text == word


@pytest.mark.parametrize("text", ["/save", "save:", "save it", "ask me", "saves", "asked"])
def test_not_bare_corrections(text):
    assert bare_word(text) is None


@pytest.mark.parametrize("text", ["new", "New!", "new one", "new entry", "New note."])
def test_new_words(text):
    r = route(text)
    assert r.kind is RouteKind.COMMAND and r.command == "new"


@pytest.mark.parametrize("text", ["separate", "Separate.", "save separately", "split"])
def test_split_words(text):
    r = route(text)
    assert r.kind is RouteKind.COMMAND and r.command == "split"


def test_new_word_with_a_file_is_a_file_and_longer_text_is_chat(tmp_path):
    assert route("new", [staged(tmp_path, "a.png")]).kind is RouteKind.SAVE_ATTACHMENTS
    assert route("new ideas for the garden").kind is RouteKind.QUESTION


def test_add_alias():
    r = route("add 6e43 the second half")
    assert r.kind is RouteKind.COMMAND and r.command == "add" and r.args == ["6e43", "the second half"]
    assert route("add more salt").kind is RouteKind.QUESTION  # no id-shaped token: chat
    assert classify(IncomingMessage(channel="tg", user_id="me", text="add 123 to it"), alias=False).kind \
        is RouteKind.QUESTION


@pytest.mark.parametrize("text", ["save them", "Save these.", "keep these screenshots as one note", "save the images above",
                                  "please save it all", "combine them into one"])
def test_save_them_is_about_the_files_not_a_note(text):
    assert route(text).kind is RouteKind.QUESTION


@pytest.mark.parametrize("text", ["hi", "Hello Octopus", "hey!", "what is this", "hello world"])
def test_greetings_and_chat_stay_as_they_were(text):
    assert route(text).kind is (RouteKind.GREETING if text in ("hi", "Hello Octopus", "hey!") else RouteKind.QUESTION)


# ---- yes / no, done, "save the images above" (mirrors test_app_manage / test_app_add_text / test_app_auto) ----

@pytest.mark.parametrize("text,answer", [("yes", (True, False)), ("Yes!", (True, False)), ("confirm", (True, False)),
                                         ("OK", (True, False)), ("sure", (True, False)), ("yep.", (True, False)),
                                         ("delete", (True, True)), ("yes, delete", (True, True)),
                                         ("no", (False, False)), ("Cancel", (False, False)), ("nope", (False, False))])
def test_typed_answers(text, answer):
    assert typed_answer(text) == answer


@pytest.mark.parametrize("text", ["yes no", "yes please do it now", "confirmed the delivery date"])
def test_not_typed_answers(text):
    assert typed_answer(text) is None


@pytest.mark.parametrize("text", ["done", "Done.", "DONE!", "finished", "all done", "that's it", "done done"])
def test_done_words(text):
    assert done_word(text)


@pytest.mark.parametrize("text", ["done with the report?", "well done", "not done"])
def test_not_done_words(text):
    assert not done_word(text)


SAVE_THESE = ["save these images as one note", "keep the screenshots above as one entry", "combine these pictures",
              "save them", "merge the photos into one article", "Save the screenshots, please"]


@pytest.mark.parametrize("text", SAVE_THESE)
def test_about_files_sent_in_english(text):
    assert about_files_sent(text)


@pytest.mark.parametrize("text", ["what is in these images?", "summarize the screenshots", "translate these photos",
                                  "save money on photos https://a.com/x", "what should I name these pictures"])
def test_not_about_files_sent_in_english(text):
    assert not about_files_sent(text)


@pytest.mark.parametrize("text", SAVE_THESE[:3])
async def test_save_these_images_makes_no_entry(tmp_path, text):
    app, ch, _ = media_app(tmp_path)
    i18n.set_language("en")
    for i in range(3):
        await app.handle(m("", [staged(tmp_path, f"s{i}.png")]))
    await app.handle(m(text))
    assert ch.sent[-1][1] == i18n.t("app.files_saving")
    await app.process_due_jobs()
    assert app.index.count() == 1


# ---- management phrases ----

@pytest.mark.parametrize("text", ["what did I save last week?", "what have I saved about AI?", "delete the one above",
                                  "delete 20261013-73b48c", "rename the one above to Compounding",
                                  "add a comment to this one: read again in May", "summarize that one again",
                                  "find my notes about Reddit", "remove that entry"])
def test_management_phrases_ask_the_intent_model(text):
    assert wants_manage(text)


@pytest.mark.parametrize("text", ["what is compounding?", "how do I bake bread", "b305ad what is it about"])
def test_plain_questions_do_not(text):
    assert not wants_manage(text, lambda token: ("20261013-b305ad", []))



# P26 (review p25b R2): the English "about" words count only in a message with no Chinese character, so Chinese
# traffic decides exactly as it did before P25
PRE_P25_ABOUT = re.compile(r"讲|说|要点|内容|观点|介绍|意思|什么|啥|怎么|为什么|哪|谁|吗|[？?]")


@pytest.mark.parametrize("text", ["20261013-b305ad 的 points", "b305ad 的 explain", "20261013-b305ad 和 about 页面",
                                  "b305ad 和 how 的区别", "b305ad tell 老王"])
def test_english_about_words_do_not_change_a_message_with_chinese(text):
    resolve = lambda token: ("20261013-b305ad", [])  # noqa: E731
    assert not PRE_P25_ABOUT.search(text)
    assert wants_manage(text, resolve)  # an id with no Chinese "about" word: the intent call decides, as before P25


@pytest.mark.parametrize("text", ["20261013-b305ad 讲了什么 points", "b305ad 的要点 what", "b305ad？"])
def test_chinese_about_words_still_make_it_a_question(text):
    assert not wants_manage(text, lambda token: ("20261013-b305ad", []))


# ---- review m1: every word the English help and hints name works ----

QUOTED = re.compile(r'"([^"]+)"')
BUTTONS = {"Save this", "Make it a question", "Confirm", "Not this one, save separately",
           "Continue"}  # button labels, not typed
HINTS = ("commands.help", "app.fix_to_question_hint", "app.save_chat_hint", "app.split_hint", "app.confirm_hint",
         "app.cancel_hint", "app.answer_please", "app.add_open", "app.new_done", "app.add_usage", "commands.save_usage")


def _works(phrase: str) -> bool:
    if phrase in ("save", "ask"):
        return route(phrase).kind is RouteKind.CORRECTION
    if phrase in ("yes", "confirm"):
        return typed_answer(phrase) == (True, False)
    if phrase in ("no", "cancel"):
        return typed_answer(phrase) == (False, False)
    if phrase == "done":
        return done_word(phrase)
    if phrase in ("new", "separate"):
        return route(phrase).command == {"new": "new", "separate": "split"}[phrase]
    if phrase in ("save: text", "save, text", "save, …"):
        r = route(phrase.replace("…", "buy milk"))
        return r.kind is RouteKind.SAVE_NOTE and r.text in ("text", "buy milk")
    if phrase == "-":  # "the 6 characters after the "-""
        return True
    return False


def test_every_word_the_english_help_names_works():
    named = {q for key in HINTS for q in QUOTED.findall(en.MESSAGES[key])} - BUTTONS
    assert named >= {"save", "ask", "yes", "done", "new", "separate", "save: text", "save, text"}
    assert {phrase: _works(phrase) for phrase in sorted(named)} == {phrase: True for phrase in sorted(named)}


# ---- end to end in English (the confirmations, the corrections, the windows) ----

def make_app(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels["tg"] = ChannelConfig(enabled=True, allowed_users=["me"])
    conn = connect(cfg.db_path, embed_dim=FakeEmbedder.dim)
    state = {"intent": {"intent": "answer"}}

    def haiku(prompt):
        if "User says:" in prompt or "用户说：" in prompt:
            return json.dumps(state["intent"])
        return json.dumps({"title": "Compounding", "summary": "Patience pays.", "points": ["Start early"],
                           "tags": ["money"], "language": "en"})

    providers = {"claude:haiku": FakeProvider("claude:haiku", haiku),
                 "claude:sonnet": FakeProvider("claude:sonnet", "Patience pays [1]."),
                 "local": FakeProvider("local", "local")}
    llm = LLMRouter(providers, {"summarize": cfg.summarize, "answer": cfg.answer}, Settings(conn))
    ch = FakeChannel("tg")
    app = App(cfg, conn=conn, library=Library(cfg.library_dir, cfg.trash_dir), index=Index(conn, FakeEmbedder()),
              llm=llm, registry=FetcherRegistry([]), channels={ch.name: ch})
    app.today = lambda: date(2026, 10, 13)
    i18n.set_language("en")
    return app, ch, state


def say(text):
    return IncomingMessage(channel="tg", user_id="me", text=text)


async def test_delete_the_one_above_then_yes(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(say("save: compounding needs time"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    state["intent"] = {"intent": "delete", "ids": [], "target": "focus"}
    await app.handle(say("delete the one above"))
    assert ch.sent[-1][1] == f'Delete this entry?\n"Compounding" {eid}'
    await app.handle(say("yes"))
    assert ch.sent[-1][1].startswith('Deleted: "Compounding"') and not app.library.find(eid)


async def test_rename_then_cancel(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(say("save: compounding needs time"))
    await app.process_due_jobs()
    state["intent"] = {"intent": "retitle", "ids": [], "target": "focus", "title": "Patience"}
    await app.handle(say("rename the one above to Patience"))
    assert ch.sent[-1][1] == 'Rename "Compounding" to "Patience"?'
    await app.handle(say("cancel"))
    assert ch.sent[-1][1] == "OK, cancelled."


async def test_save_after_an_answer_and_ask_after_a_note(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(say("why does patience matter"))
    await app.handle(say("save"))
    await app.process_due_jobs()
    assert ch.sent[-1][1].startswith("✅ Saved:")
    await app.handle(say("save: buy milk"))
    await app.process_due_jobs()
    await app.handle(say("ask"))
    assert ch.sent[-1][1].startswith("Patience pays [1].")


async def test_new_then_done(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(say("new"))
    assert ch.sent[-1][1] == i18n.t("app.new_done") and app.pending.origin("tg", "me") == "new"
    await app.handle(say("done"))
    assert ch.sent[-1][1] == "OK, done. What you send next is a chat with me." and app.pending.origin("tg", "me") is None


async def test_what_did_i_save_lately_lists_the_latest(tmp_path):
    app, ch, state = make_app(tmp_path)
    await app.handle(say("save: compounding needs time"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    state["intent"] = {"intent": "find", "query": "things I saved recently"}
    await app.handle(say("what did I save recently?"))
    assert ch.sent[-1][1] == f"Found 1:\n{eid}  Compounding"
