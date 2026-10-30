"""P13: a short id also matches the start of the 6-char part; "@id question" asks about that entry only."""
import pytest
from test_app_manage import _indexed, _put, answer_prompts, m, make_app, two_articles

from octopus.app import AT_DEFAULT_QUESTION, AT_TOO_MANY, MISSING_ID, SAVE_CHAT, UNREADABLE
from octopus.commands import AMBIGUOUS_ID, HELP
from octopus.manage import at_ids
from octopus.models import IncomingMessage
from octopus.pending import NEW
from octopus.qa import PINNED_MARK

GIT = "20261015-b305ad"
GIT_HEAD = f"[1] 《Acme宣布全面迁移Rust》 id：{GIT}\n{PINNED_MARK}\n"


# ---- R1: the first 4-6 characters after "-" name an entry too ----

async def test_the_start_of_an_id_works_in_every_command(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("/read b305"))
    assert ch.sent[0][1].startswith("（1/3）\n《Acme宣布全面迁移Rust》\nid：20261015-b305ad")
    await app.handle(m("/add B305 一段补充"))
    [job] = app.jobs.claim_due(10)
    assert job.kind == "merge" and job.payload["entry_id"] == GIT and job.payload["text"] == "一段补充"
    await app.handle(m("/resummarize b305a"))
    assert f"id：{GIT}" in ch.sent[-1][1]
    await app.handle(m("/delete b305"))
    assert ch.sent[-1][1].startswith(f"已删除：「Acme宣布全面迁移Rust」 {GIT}")
    assert app.library.find(GIT) is None


async def test_the_start_of_an_id_in_chat_management_and_questions(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 b305"))
    assert ch.sent[-1][1] == f"确定删除下面 1 条吗？\n「Acme宣布全面迁移Rust」 {GIT}"
    await app.handle(m("取消"))
    for question in ("id b305 讲了什么", "b305a 讲了什么"):  # marked 4 chars; unmarked 5 with a digit and a-f
        await app.handle(m(question))
        assert GIT_HEAD in answer_prompts(app)[-1], question


async def test_a_start_and_an_end_match_are_listed_together(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-5cc8aa", "甲")
    await _put(app, "20261012-c15cc8", "乙")
    await app.handle(m("/read 5cc8"))
    assert ch.sent[-1][1] == AMBIGUOUS_ID.format(token="5cc8") + "\n20261013-5cc8aa  甲\n20261012-c15cc8  乙"


async def test_a_marked_token_that_starts_one_id_and_ends_another_lists_both(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-5cc8aa", "甲")
    await _put(app, "20261012-c15cc8", "乙")
    await app.handle(m("id 5cc8 讲了什么"))
    assert ch.sent[-1][1] == AMBIGUOUS_ID.format(token="5cc8") + "\n20261013-5cc8aa  甲\n20261012-c15cc8  乙"
    assert answer_prompts(app) == []


async def test_a_number_naming_an_entry_by_its_start_costs_an_intent_call_only(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-aaaadd", "甲文", "正文甲。")
    await app.handle(m("2026年计划"))
    assert state["intent_calls"] == 0  # names no entry: straight to the answer
    await _indexed(app, "20261012-2026ab", "乙文", "正文乙。")
    await app.handle(m("2026年计划"))
    assert state["intent_calls"] == 1  # starts an entry's id: a candidate for the manage gate (ruling F2)
    assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1]
    await app.handle(m("2026年有什么计划"))
    assert state["intent_calls"] == 1  # only asked about: no intent call


async def test_hex_words_in_chat_are_not_ids(tmp_path):
    app, ch, state = make_app(tmp_path)
    for eid in ("20261013-cafe12", "20261012-dead01", "20261011-face00", "20261010-beef99"):
        await _indexed(app, eid, f"文{eid[-2:]}", "正文。")
    for question in ("cafe 讲了什么", "dead 怎么翻译", "face 推荐", "beef 好吃吗", "id cafe 讲了什么"):
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question
    assert state["intent_calls"] == 0


async def test_unmarked_plain_numbers_are_still_not_ids_by_their_start(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _indexed(app, "20261013-2026ab", "甲文", "正文甲。")
    await _indexed(app, "20261012-100abc", "乙文", "正文乙。")
    for question in ("2026年AI有什么趋势", "我有100块钱怎么理财"):
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答 [1]") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question


def test_help_says_how_to_name_an_entry():
    assert "· 其他文字都是和我对话，我会先查你的知识库再回答\n· @记录号 问题 → 只针对这一条提问，例：@b305ad 讲了什么\n" in HELP
    assert "· 记录号可以打全，也可以只打“-”后面的 6 位，或其中前 4 位、后 4 位，例：/delete 6e43" in HELP


# ---- R2: "@id 问题" ----

@pytest.mark.parametrize("text, parsed", [
    ("@b305ad 讲了什么", (["b305ad"], "讲了什么")),
    ("@20261015-b305ad 有哪些要点", (["20261015-b305ad"], "有哪些要点")),
    ("@B305 讲了什么", (["b305"], "讲了什么")),
    ("@05ad 第三步是什么", (["05ad"], "第三步是什么")),
    ("@b305ad @5cc8 这两篇有什么不同", (["b305ad", "5cc8"], "这两篇有什么不同")),
    ("@b305ad，@5cc8，有什么不同", (["b305ad", "5cc8"], "有什么不同")),
    ("＠b305ad讲了什么", (["b305ad"], "讲了什么")),
    ("@b305ad", (["b305ad"], "")),
    ("@b305ad @b305ad 讲了什么", (["b305ad"], "讲了什么")),
    ("@b305ad 和 @5cc8 有什么不同", (["b305ad", "5cc8"], "有什么不同")),
    ("@b305ad和@5cc8、@aaaa 比较一下", (["b305ad", "5cc8", "aaaa"], "比较一下")),
    ("@b305ad 与 @5cc8 跟 @aaaa 及 @bbbb 还有 @cccc & @dddd 呢", (["b305ad", "5cc8", "aaaa", "bbbb", "cccc", "dddd"], "呢")),
    ("@b305ad 和什么有关", (["b305ad"], "和什么有关")),  # 和 joins only another @id
    ("@b305ad？", (["b305ad"], "")), ("@b305ad。", (["b305ad"], "")), ("@b305ad!", (["b305ad"], "")),
    ("＠ｂ３０５ａｄ 讲了什么", (["b305ad"], "讲了什么")),
    ("@a1 @b2 @c3 @d4 好", None),  # 2 characters: not id-shaped
    ("@张三 你好", None), ("@all 开会了", None), ("看看 @b305ad 讲了什么", None), ("@20261015 讲了什么", None),
    ("@cafebabe 讲了什么", None), ("@b305adx 讲了什么", None), ("@ b305ad 讲了什么", None), ("", None),
])
def test_at_ids(text, parsed):
    assert at_ids(text) == parsed


async def test_at_id_answers_from_that_entry_only(tmp_path):
    for n, question in enumerate(["@b305ad 讲了什么", "@20261015-b305ad 有哪些要点", "@b305 讲了什么",
                                  "@05ad 第三步是什么", "＠B305AD讲了什么", "问：@b305ad 讲了什么"]):
        (tmp_path / str(n)).mkdir()
        app, ch, state = make_app(tmp_path / str(n))
        await two_articles(app)
        await app.handle(m(question))
        assert state["intent_calls"] == 0, question
        [prompt] = answer_prompts(app)
        assert GIT_HEAD in prompt and "开头说了原因。" in prompt and "结尾的话。" in prompt, question
        assert "家庭烘焙配方" not in prompt, question  # no search hits from other entries
        text, buttons = ch.sent_full[-1]
        assert text.startswith("回答 [1]") and f"[1] Acme宣布全面迁移Rust（id：{GIT}）" in text, question
        assert [b.label for b in buttons] == [SAVE_CHAT]
        assert app.focus.get("tg", "me") == GIT


async def test_several_at_ids_and_a_bare_one(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("@b305ad @aaaa 这两篇有什么不同"))
    prompt = answer_prompts(app)[-1]
    assert GIT_HEAD in prompt and f"[2] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in prompt
    assert app.focus.get("tg", "me") == GIT  # the first one
    await app.handle(m("@aaaaaa"))  # no question: what it is about
    prompt = answer_prompts(app)[-1]
    assert f"[1] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in prompt and "Git" not in prompt
    assert f"问题：@aaaaaa {AT_DEFAULT_QUESTION}" in prompt
    assert app.focus.get("tg", "me") == "20261013-aaaaaa"


async def test_at_ids_joined_by_he_and_bare_with_punctuation(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("@b305ad 和 @aaaa 有什么不同"))
    prompt = answer_prompts(app)[-1]
    assert GIT_HEAD in prompt and f"[2] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in prompt
    await app.handle(m("@aaaaaa？"))
    assert f"问题：@aaaaaa {AT_DEFAULT_QUESTION}" in answer_prompts(app)[-1]
    await app.handle(m("＠ｂ３０５ａｄ 讲了什么"))  # typed in full-width mode
    assert GIT_HEAD in answer_prompts(app)[-1] and "家庭烘焙配方" not in answer_prompts(app)[-1]


async def test_the_cap_counts_entries_not_tokens(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    await app.handle(m("@b305ad @b305 @b305a @aaaa 比较"))  # 4 tokens, 2 entries
    prompt = answer_prompts(app)[-1]
    assert GIT_HEAD in prompt and f"[2] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in prompt


async def test_an_at_question_with_a_link_is_answered_not_saved(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    for question in ("@b305ad 里提到的 https://x.com/a 是什么", "@b305ad 说的 github.com 是什么"):
        await app.handle(m(question))
        assert app.jobs.claim_due(10) == [], question
        assert GIT_HEAD in answer_prompts(app)[-1] and ch.sent[-1][1].startswith("回答"), question
    await app.handle(m("/s @b305ad https://x.com/a"))  # /s with words beside the link: a note (P14), not asked
    assert [j.kind for j in app.jobs.claim_due(10)] == ["note"]


async def test_at_ids_not_clear_are_reported_without_a_model_call(tmp_path):
    app, ch, state = make_app(tmp_path)
    await _put(app, "20261013-5cc8aa", "甲")
    await _put(app, "20261012-c15cc8", "乙")
    await _put(app, "20261011-b305ad", "丙")
    block = AMBIGUOUS_ID.format(token="5cc8") + "\n20261013-5cc8aa  甲\n20261012-c15cc8  乙"
    await app.handle(m("@5cc8 讲了什么"))
    assert ch.sent[-1][1] == block
    await app.handle(m("@fff 讲了什么"))
    assert ch.sent[-1][1] == MISSING_ID.format(id="fff")
    await app.handle(m("@b305ad @fff @5cc8 有什么不同"))
    assert ch.sent[-1][1] == MISSING_ID.format(id="fff") + "\n" + block
    await app.handle(m("@20261011-b305ae 讲了什么"))
    assert ch.sent[-1][1] == MISSING_ID.format(id="20261011-b305ae")
    await _put(app, "20261010-abcdef", "丁")
    await app.handle(m("@b305ad @5cc8aa @c15cc8 @abcd 有什么不同"))
    assert ch.sent[-1][1] == AT_TOO_MANY
    assert answer_prompts(app) == [] and state["intent_calls"] == 0


async def test_an_unreadable_at_id_is_said_so(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    app.library.find(GIT).write_text("---\nid: [broken\n---\n", encoding="utf-8")
    await app.handle(m("@b305ad 讲了什么"))
    assert ch.sent[-1][1] == UNREADABLE + GIT and answer_prompts(app) == []


async def test_at_id_with_a_management_word_is_still_a_question(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    state["intent"] = {"intent": "delete", "ids": [], "target": "focus"}
    await app.handle(m("@b305ad 删掉"))
    assert state["intent_calls"] == 0 and app.ops.latest("tg", "me") is None
    assert GIT_HEAD in answer_prompts(app)[-1] and ch.sent[-1][1].startswith("回答")


async def test_other_at_signs_are_plain_chat(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    for question in ("@张三 你好", "@all 讲了什么"):
        await app.handle(m(question))
        assert ch.sent[-1][1].startswith("回答") and f"{PINNED_MARK}\n" not in answer_prompts(app)[-1], question
    await app.handle(m("看看 @b305ad 讲了什么"))  # not at the start: P11 pins it, search hits come too
    assert GIT_HEAD in answer_prompts(app)[-1] and "[2] 《家庭烘焙配方》" in answer_prompts(app)[-1]


async def test_an_at_id_in_a_note_is_saved(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    for text in ("/s @b305ad 讲了什么", "存：@b305ad 讲了什么"):
        await app.handle(m(text))
        [job] = app.jobs.claim_due(10)
        assert job.payload["text"] == "@b305ad 讲了什么", text
    assert answer_prompts(app) == []


async def test_at_id_ends_a_window_and_is_never_material(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    app.pending.set("tg", "me", "20261013-aaaaaa", origin=NEW)
    await app.handle(m("@b305ad 讲了什么"))
    assert app.pending.active("tg", "me") is None and GIT_HEAD in answer_prompts(app)[-1]
    app.pending.set("tg", "me", "20261013-aaaaaa", prompt_message_id="prompt-1")  # a prompt asking for material
    await app.handle(IncomingMessage(channel="tg", user_id="me", text="@b305ad 讲了什么", reply_to_id="prompt-1"))
    assert app.jobs.claim_due(10) == [] and GIT_HEAD in answer_prompts(app)[-1]


async def test_a_short_at_id_while_a_confirmation_waits_is_answered(tmp_path):
    app, ch, state = make_app(tmp_path)
    await two_articles(app)
    state["intent"] = {"intent": "delete", "ids": []}
    await app.handle(m("删掉 b305ad"))
    assert ch.sent[-1][1].startswith("确定删除下面 1 条吗？")
    await app.handle(m("@aaaa"))  # as short as a reply to it: still the question
    assert f"[1] 《家庭烘焙配方》 id：20261013-aaaaaa\n{PINNED_MARK}\n" in answer_prompts(app)[-1]
