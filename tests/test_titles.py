"""P38: a 小红书 note's page title is its whole text plus the site's name; it is saved as a short title instead."""
import pytest
from test_ext_platforms import XHS_CN
from test_extension_saves import build, page

from octopus.fetchers.base import FetchResult
from octopus.models import IncomingMessage
from octopus.pipeline import body_sha
from octopus.titles import post_title

XHS = "https://www.xiaohongshu.com/explore/0123456789abcdef01234567"
ARTICLE = "https://example.com/blog/post"


# a note's text goes on long after its first sentence (over 40 characters in all)
MORE = " 后面还有很多很多想说的话，就不一一写在标题里了，欢迎大家一起来交流"


def test_a_notes_page_title_becomes_its_first_sentence():
    title = ("#蔬菜清单 推荐 南瓜、山药、豆角、茄子、白菜、生菜都是厨房里的常客✅ 配上豆腐、牛肉、鱼片等食材，更容易吃饱。"
             " 每天换着做，简单又不重样 #家常菜 #快手菜 - 小红书")
    assert post_title(title, [XHS]) == "推荐 南瓜、山药、豆角、茄子、白菜、生菜都是厨房里的常客"


def test_the_site_suffix_goes():
    assert post_title("周末去爬山 - 小红书", [XHS]) == "周末去爬山"


def test_a_text_of_40_characters_or_less_is_kept_whole():
    # M1: an author's own title (小红书 titles are short) or a short note: no first-sentence cut
    assert post_title("上海这家咖啡店绝了！人均三十还能坐一下午 - 小红书", [XHS]) == "上海这家咖啡店绝了！人均三十还能坐一下午"
    assert post_title("上海周末去哪儿🌸 赏花攻略合集 - 小红书", [XHS]) == "上海周末去哪儿🌸 赏花攻略合集"
    forty = "一二三四五六七八九十" * 3 + "一二三四五六七八。十"
    assert post_title(f"{forty} - 小红书", [XHS]) == forty
    assert post_title(f"{forty}多 - 小红书", [XHS]) == forty[:38]  # 41: the first sentence


def test_the_apps_topic_form_goes_too():
    assert post_title("#旅行[话题]# #周末[话题]# 周末去了郊外的湖边露营 - 小红书", [XHS]) == "周末去了郊外的湖边露营"
    assert post_title("#旅行[话题]#周末去了郊外的湖边露营 - 小红书", [XHS]) == "周末去了郊外的湖边露营"


@pytest.mark.parametrize("title,short", [
    ("#露营#周末 今天去了湖边，风景很好 - 小红书", "今天去了湖边，风景很好"),  # glued tags
    ("今天去了湖边#露营#周末 - 小红书", "今天去了湖边"),
    ("＃露营 ＃周末 今天去了湖边 - 小红书", "今天去了湖边"),  # the full-width ＃ too
    # a "#" whose words run on into the text: only the "#" goes
    ("#露营好开心今天去了湖边，风景很好 #周末 下次还来 - 小红书", "露营好开心今天去了湖边，风景很好 下次还来"),
    ("周末 #露营好开心今天天气很好。下次还来 - 小红书", "周末 露营好开心今天天气很好。下次还来"),
    ("＃露营好开心今天天气很好。下次还来 - 小红书", "露营好开心今天天气很好。下次还来"),
    ("#露营今天去了湖边这里的风景真的是太好了下次一定还来 - 小红书", "露营今天去了湖边这里的风景真的是太好了下次一定还来"),  # over 20
    ("C#入门教程分享，零基础也能学会 - 小红书", "C#入门教程分享，零基础也能学会"),  # after a letter: a word
    ("第1名！#1 推荐这家店的招牌菜 - 小红书", "第1名！ 推荐这家店的招牌菜"),
])
def test_hashtags(title, short):
    assert post_title(title, [XHS]) == short


def test_a_running_on_hash_in_a_long_text_keeps_its_first_sentence():
    title = f"#露营好开心今天去了湖边，风景很好。{MORE} - 小红书"
    assert post_title(title, [XHS]) == "露营好开心今天去了湖边，风景很好"


def test_the_apps_stickers_go():
    assert post_title("今天好开心[哈哈R] 去了新开的咖啡店[笑哭R] - 小红书", [XHS]) == "今天好开心 去了新开的咖啡店"
    assert post_title("[哈哈R] - 小红书", [XHS]) == "[哈哈R] - 小红书"  # nothing else: as before
    assert post_title("今天[新店]开业 - 小红书", [XHS]) == "今天[新店]开业"  # brackets of the text stay


@pytest.mark.parametrize("title,short", [
    ("#露营 #周末 - 小红书", "露营 周末"),  # only hashtags: their words
    ("#露营[话题]# - 小红书", "露营"),
    ("小红书", "小红书"),  # the site's own title (a page it would not show): as before
])
def test_a_title_of_hashtags_only_keeps_their_words_never_nothing(title, short):
    assert post_title(title, [XHS]) == short


def test_an_emoji_before_a_space_ends_the_first_phrase():
    assert post_title(f"今天去外滩看夜景🌃 真的太美了推荐大家去{MORE} - 小红书", [XHS]) == "今天去外滩看夜景"
    assert post_title(f"今天去外滩看夜景 🌃🌃 真的太美了{MORE} - 小红书", [XHS]) == "今天去外滩看夜景"
    glued = post_title(f"今天的晚餐🍜超级好吃{MORE} - 小红书", [XHS])
    assert glued.startswith("今天的晚餐🍜超级好吃")  # inside the phrase: stays


def test_a_short_first_piece_takes_the_next_one_too():
    title = f"好吃！楼下新开的面馆一定要去试试。别的都一般{MORE} - 小红书"
    assert post_title(title, [XHS]) == "好吃！楼下新开的面馆一定要去试试"


def test_a_question_keeps_its_mark():
    assert post_title(f"有人知道这家店还开着吗？上次去关门了{MORE} - 小红书", [XHS]) == "有人知道这家店还开着吗？"


def test_a_first_sentence_over_40_characters_is_cut_with_an_ellipsis():
    text = "一二三四五六七八九十" * 5  # 50 characters, no end of sentence
    short = post_title(f"{text} - 小红书", [XHS])
    assert short == text[:40] + "…" and len(short) == 41


@pytest.mark.parametrize("emoji", ["👨\u200d👩\u200d👧", "👍🏻", "🇨🇳", "1\ufe0f\u20e3", "\ud83d\ude00"],
                         ids=["zwj", "skin-tone", "flag", "keycap", "surrogate-pair"])
def test_the_cut_never_splits_an_emoji(emoji):
    head = "这是一段很长的笔记正文" * 4  # 44 characters; the emoji starts at the 40th: the cut falls inside it
    text = head[:39] + emoji + "后面还有很多字"
    assert post_title(f"{text} - 小红书", [XHS]) == head[:39] + "…"


def test_english_post_text():
    title = "Tried the new ramen place downtown. The broth was incredible and the noodles were perfect - 小红书"
    assert post_title("A sunny day at the lake. So nice - 小红书", [XHS]) == "A sunny day at the lake. So nice"
    assert post_title(title, [XHS]) == "Tried the new ramen place downtown"
    long = "Weekend hiking trip to the mountains with friends and family was amazing - 小红书"
    assert post_title(long, [XHS]) == "Weekend hiking trip to the mountains…"  # not inside a word


@pytest.mark.parametrize("url", [XHS, XHS_CN, "http://xhslink.com/a/b", "https://xiaohongshu.com/discovery/item/x"])
def test_every_xiaohongshu_link_counts(url):
    assert post_title("周末去爬山 - 小红书", [url]) == "周末去爬山"


def test_a_short_link_counts_by_where_it_redirected():
    assert post_title("周末去爬山 - 小红书", ["https://xhs.example/o/AbCd", XHS]) == "周末去爬山"


@pytest.mark.parametrize("title,urls", [
    ("一篇普通文章的标题，很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长。第二句 #标签", [ARTICLE]),
    ("Python 3.13 发布！新特性一览 - 小红书", [ARTICLE]),  # the suffix alone does not make it a note
    ("视频标题 - 抖音", ["https://www.douyin.com/video/1"]),  # not in scope (see p5-notes P38)
    ("周末去爬山 - 小红书", []),
])
def test_other_titles_are_untouched(title, urls):
    assert post_title(title, urls) == title


def test_no_title_stays_none():
    assert post_title(None, [XHS]) is None and post_title("", [XHS]) == ""


# ---- where titles are set ----

NOTE_TITLE = f"#露营 周末去郊外的湖边露营了✨ 风景很好，下次还来{MORE} #周末 - 小红书"


async def test_a_chat_link_save_gets_the_short_title(tmp_path):
    app, _ = build(tmp_path)
    out = await app.pipeline.store(FetchResult(url=XHS_CN, source="web", title=NOTE_TITLE, text="x", status="partial",
                                               aliases=[XHS]), "tg")
    assert out.entry.title == "周末去郊外的湖边露营了"


async def test_an_extension_page_save_gets_the_short_title(tmp_path):
    app, intake = build(tmp_path)
    await intake.save(page(url=XHS, title=NOTE_TITLE))
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    assert row["title"] == "周末去郊外的湖边露营了"


async def test_an_extension_selection_save_gets_the_short_title(tmp_path):
    app, _ = build(tmp_path)
    out = await app.pipeline.save_note("选中的一段话", "extension", url=XHS, title=NOTE_TITLE)
    assert out.entry.title == "周末去郊外的湖边露营了"


async def test_an_article_keeps_its_long_title(tmp_path):
    app, intake = build(tmp_path)
    long = "一篇普通文章的标题很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长 - 小红书"
    await intake.save(page(title=long))
    await app.process_due_jobs()
    [row] = app.index.recent(10)
    assert row["title"] == long


async def test_a_fill_of_a_partial_note_gets_the_short_title_and_a_later_fill_keeps_the_owners(tmp_path):
    app, intake = build(tmp_path)
    app.web.result = FetchResult(url=XHS, source="web", title="小红书", text="页脚", status="partial", note="短")
    await app.handle(IncomingMessage(channel="tg", user_id="me", text=XHS))
    await app.process_due_jobs()
    first = app.index.find_by_url(XHS)
    assert app.library.get(first).title == "小红书"

    longer = ("<html><head><title>帖子</title></head><body><article><p>" + "露营的第一天风很大。" * 5
              + "</p></article></body></html>")  # still partial: a later fill may come
    await intake.save(page(url=XHS, title=NOTE_TITLE, html=longer))
    await app.process_due_jobs()
    entry = app.library.get(first)
    assert (entry.title, entry.status) == ("周末去郊外的湖边露营了", "partial") and "露营的第一天" in entry.body
    assert entry.fetched_title == body_sha("周末去郊外的湖边露营了")  # the record follows the short title

    entry.title = "我改的标题"
    app.library.save(entry)
    await intake.save(page(url=XHS, title=NOTE_TITLE))
    await app.process_due_jobs()
    assert (app.library.get(first).title, app.library.get(first).status) == ("我改的标题", "ok")
    assert len(app.index.recent(10)) == 1
