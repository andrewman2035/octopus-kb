import pytest

from octopus.models import Attachment, IncomingMessage
from octopus.router import LONG_PASTE_CHARS, RouteKind, classify


def msg(text="", attachments=None):
    return IncomingMessage(channel="tg", user_id="me", text=text, attachments=attachments or [])


@pytest.mark.parametrize("text,content", [
    ("/s 明天给老王回电话", "明天给老王回电话"),
    ("/S 明天", "明天"),
    ("/save 一个想法", "一个想法"),
    ("/s\n多行\n内容", "多行\n内容"),
    ("存 明天开会", "明天开会"),
    ("存：明天开会", "明天开会"),
    ("存:明天开会", "明天开会"),
    ("存 : 明天开会", "明天开会"),
    ("存，明天开会", "明天开会"),
    ("保存 明天开会", "明天开会"),
    ("存\n明天开会", "明天开会"),
])
def test_save_triggers_make_a_note(text, content):
    r = classify(msg(text))
    assert r.kind is RouteKind.SAVE_NOTE and r.text == content


def test_save_trigger_with_link_saves_the_link():
    r = classify(msg("/s https://example.com/a"))
    assert r.kind is RouteKind.SAVE_URLS and r.urls == ["https://example.com/a"]


@pytest.mark.parametrize("text", ["/s", "/save", "/s   ", "存：", "保存 :"])
def test_empty_save_is_the_save_command(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.COMMAND and r.command == "save" and r.args == []


@pytest.mark.parametrize("text", ["/status", "/search x", "/saved"])
def test_other_slash_words_stay_commands(text):
    assert classify(msg(text)).kind is RouteKind.COMMAND
    assert classify(msg(text)).command != "save"


@pytest.mark.parametrize("text", ["复利需要时间", "存款利率是多少", "明天给老王回电话", "what is this"])
def test_plain_text_is_chat(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.QUESTION and r.text == text


def test_ask_prefix_still_chat():
    r = classify(msg("问：存 这个算什么"))
    assert r.kind is RouteKind.QUESTION and r.text == "存 这个算什么"


def test_bare_words_are_corrections():
    assert classify(msg("存")).kind is RouteKind.CORRECTION
    assert classify(msg("问")).kind is RouteKind.CORRECTION


def test_voice_only_message_is_voice():
    v = Attachment(kind="audio", filename="voice.ogg", voice=True)
    assert classify(msg("", [v])).kind is RouteKind.VOICE


def test_voice_with_a_file_is_saved():
    v = Attachment(kind="audio", filename="voice.ogg", voice=True)
    assert classify(msg("", [v, Attachment(kind="image")])).kind is RouteKind.SAVE_ATTACHMENTS


def test_audio_file_is_still_saved():
    a = Attachment(kind="audio", filename="talk.mp3")
    assert classify(msg("", [a])).kind is RouteKind.SAVE_ATTACHMENTS


def test_photo_still_saved():
    assert classify(msg("", [Attachment(kind="image")])).kind is RouteKind.SAVE_ATTACHMENTS


def test_urls_saved_even_with_question_words():
    r = classify(msg("这个怎么样？https://a.com/x 和 https://b.com/y。"))
    assert r.kind is RouteKind.SAVE_URLS
    assert r.urls == ["https://a.com/x", "https://b.com/y"]


def test_save_prefix_forces_note():
    r = classify(msg("存：为什么天空是蓝的？"))
    assert r.kind is RouteKind.SAVE_NOTE
    assert r.text == "为什么天空是蓝的？"


def test_save_prefix_with_url():
    r = classify(msg("存: https://a.com"))
    assert r.kind is RouteKind.SAVE_URLS
    assert r.urls == ["https://a.com"]


def test_ask_prefix_forces_question():
    r = classify(msg("问:https://a.com 讲了什么"))
    assert r.kind is RouteKind.QUESTION
    assert r.text == "https://a.com 讲了什么"


def test_command():
    r = classify(msg("/model local"))
    assert r.kind is RouteKind.COMMAND
    assert (r.command, r.args) == ("model", ["local"])
    assert classify(msg("/HELP")).command == "help"


def test_empty_is_ignored():
    assert classify(msg("   ")) is None


@pytest.mark.parametrize("caption", ["存", "问"])
def test_captioned_attachment_is_not_a_correction(caption):
    r = classify(msg(caption, [Attachment(kind="image")]))
    assert r.kind is RouteKind.SAVE_ATTACHMENTS


@pytest.mark.parametrize("text", ["hi", "Hi!", "hi oct", "Hello Octopus", "你好", "你好！", "在吗", "早上好～",
                                  "HEY", "你好呀", "在吗？", "嗨 章鱼", "hi 👋", "  hello octo。 "])
def test_greetings(text):
    assert classify(msg(text)).kind is RouteKind.GREETING


@pytest.mark.parametrize("text", ["hi, what is 复利", "你好，帮我查一下", "hello world", "/s 你好", "存 你好",
                                  "问：你好", "hiking", "早上好看的书"])
def test_not_greetings(text):
    assert classify(msg(text)).kind is not RouteKind.GREETING


def test_greeting_with_a_photo_is_still_saved():
    a = Attachment(kind="image", filename="p.jpg")
    assert classify(msg("hi", [a])).kind is RouteKind.SAVE_ATTACHMENTS


@pytest.mark.parametrize("text", ["存。", "问！", "存～", "问。", "存!", "存吧", " 存 。"])
def test_bare_words_with_punctuation_are_corrections(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.CORRECTION and r.text == text.strip()[0]


@pytest.mark.parametrize("text", ["存：", "存:", "存存", "存一下", "问问"])
def test_not_bare_words(text):
    assert classify(msg(text)).kind is not RouteKind.CORRECTION


@pytest.mark.parametrize("text", ["/new", "新的", "新的一条", "单独存下一条", "新的。", "新的一条！", "新的吧",
                                  "新的一条吧！"])
def test_new_words_route_to_the_new_command(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.COMMAND and r.command == "new"


def test_new_word_with_a_file_is_a_file_and_longer_text_is_chat():
    assert classify(msg("新的", [Attachment(kind="image", path=None, filename="a.png")])).kind is RouteKind.SAVE_ATTACHMENTS
    assert classify(msg("新的一条消息怎么样")).kind is RouteKind.QUESTION


@pytest.mark.parametrize("text", ["/split", "单独存", "单独保存", "分开存", "另存", "单独存。", "另存！", "单独存吧",
                                  "单独存啊。"])
def test_split_words_route_to_the_split_command(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.COMMAND and r.command == "split"


def test_split_word_with_a_file_is_a_file_and_longer_text_is_chat():
    assert classify(msg("单独存", [Attachment(kind="image", path=None, filename="a.png")])).kind is RouteKind.SAVE_ATTACHMENTS
    assert classify(msg("另存为什么不行")).kind is RouteKind.QUESTION


def test_alias_routes():
    r = classify(msg("补充 6e43 文字  x"))
    assert r.kind is RouteKind.COMMAND and r.command == "add" and r.args == ["6e43", "文字  x"]
    r = classify(msg("补充：20260101-abcdef"))
    assert r.command == "add" and r.args == ["20260101-abcdef"]
    assert classify(msg("补充6e43")).args == ["6e43"]
    for t in ["补充一下", "补充说明：这个不对", "补充 add 一点", "补充 abcdef 一点", "补充 6e43x"]:
        assert classify(msg(t)).kind is RouteKind.QUESTION, t


def test_bare_wechat_link_is_saved_as_a_link():
    r = classify(msg("mp.weixin.qq.com/s/AbCd-EfGh_IjKlMnOpQrSt"))
    assert r.kind is RouteKind.SAVE_URLS and r.urls == ["https://mp.weixin.qq.com/s/AbCd-EfGh_IjKlMnOpQrSt"]


def test_bare_unknown_host_in_a_question_stays_a_question():
    assert classify(msg("python.org/downloads 上哪个版本适合 Mac")).kind is RouteKind.QUESTION


# P14: a link inside pasted text or a /s note is part of the text
ARTICLE = ("短视频七步法：第一步，找到你的定位，想清楚你要服务谁、解决什么问题。第二步，拆解对标账号，"
           "看他们的选题、开头三秒、节奏和结尾的引导。第三步，建立自己的选题库，每天记录十个灵感。") * 3
LINK = "https://github.com/example-org/sample-repo"
DOUYIN = ("7.43 复制打开抖音，看看【小王的作品】短视频七步法，从零开始做账号 # 短视频 # 运营 "
          "https://v.douyin.com/iRNBxyz1/ Kqm:/ 07/25 T@p.Rw")
XHS = ("小王 发布了一篇小红书笔记，快来看吧！😆 7wGcKq1Ab2Cd3Ef 😆 http://xhslink.com/a/AbCdEf123 "
       "复制本条信息，打开【小红书】App查看精彩内容！")


@pytest.mark.parametrize("prefix", ["/s ", "/save ", "存 ", "存：", "保存 "])
def test_save_prefix_with_text_and_a_link_is_a_note(prefix):
    r = classify(msg(f"{prefix}这个项目不错，回头试试 {LINK}"))
    assert r.kind is RouteKind.SAVE_NOTE and r.text == f"这个项目不错，回头试试 {LINK}"
    assert r.urls == [LINK] and not r.pasted


@pytest.mark.parametrize("text", [f"/s {LINK}", f"/s  {LINK} 。", f"存：{LINK}！", f"/s {LINK} https://a.com/b"])
def test_save_prefix_with_only_links_still_reads_them(text):
    assert classify(msg(text)).kind is RouteKind.SAVE_URLS


def test_save_prefix_with_text_a_link_and_a_photo_saves_the_photo_with_the_text():
    r = classify(msg(f"/s 看这个 {LINK}", [Attachment(kind="image")]))
    assert r.kind is RouteKind.SAVE_ATTACHMENTS and r.text == f"看这个 {LINK}" and r.urls == [LINK]


def test_long_paste_with_a_link_is_a_note():
    text = f"{ARTICLE}\n项目地址：{LINK}’\n{ARTICLE}"
    r = classify(msg(text))
    assert r.kind is RouteKind.SAVE_NOTE and r.text == text and r.urls == [LINK] and r.pasted


def test_the_paste_rule_counts_the_text_without_the_links():
    body = "字" * (LONG_PASTE_CHARS - 1)
    assert classify(msg(f"{body} {LINK}/{'x' * 100}")).kind is RouteKind.SAVE_URLS
    assert classify(msg(f"  {body}字 {LINK}  ")).kind is RouteKind.SAVE_NOTE


@pytest.mark.parametrize("text", [DOUYIN, XHS, f"看看这个 {LINK}", "这个怎么样？https://a.com/x 和 https://b.com/y。"])
def test_share_blurbs_are_still_links(text):
    r = classify(msg(text))
    assert r.kind is RouteKind.SAVE_URLS and r.urls


def test_long_paste_with_a_link_and_a_photo_keeps_the_text():
    r = classify(msg(f"{ARTICLE}{ARTICLE} {LINK}", [Attachment(kind="image")]))
    assert r.kind is RouteKind.SAVE_ATTACHMENTS and r.urls == [LINK]


@pytest.mark.parametrize("text", ["看看 http://[abc/def", "http://[::1/x", "存 http://a.com:99999/x",
                                  "http://:80/x 是什么", "http://"])
def test_text_with_a_link_urllib_cannot_parse_routes_without_raising(text):
    route = classify(msg(text))
    assert route is not None and route.kind is not RouteKind.SAVE_URLS and route.urls == []
