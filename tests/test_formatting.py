from datetime import datetime, timezone

from octopus.formatting import format_answer, format_duplicate, format_saved
from octopus.library import Entry
from octopus.pipeline import SaveOutcome
from octopus.qa import Answer, Source


def entry(**kw):
    base = dict(id="20261013-abc123", title="复利", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
                channel="cli", url="https://a.com", summary="摘要。", tags=["投资", "AI"])
    base.update(kw)
    return Entry(**base)


def test_saved_ok():
    assert format_saved(SaveOutcome(entry(), local_fallback=False)) == "✅ 已保存：复利\n摘要。\n#投资 #AI\nid：20261013-abc123"


def test_saved_local_note():
    assert format_saved(SaveOutcome(entry(), local_fallback=True)).endswith("（本次摘要由本地模型生成）")


def test_saved_partial():
    text = format_saved(SaveOutcome(entry(status="partial"), False, note="只拿到了标题"))
    assert text.startswith("⚠️ 已保存（内容不完整）：复利")
    assert "只拿到了标题" in text and "截图或录屏" in text


def test_saved_needs_manual():
    text = format_saved(SaveOutcome(entry(status="needs_manual", summary="", tags=[]), False, note="没有正文"))
    assert text.startswith("⚠️ 没能获取内容：https://a.com")
    assert "原因：没有正文" in text and "截图、录屏或粘贴原文" in text and "id：20261013-abc123" in text


def test_duplicate():
    assert format_duplicate("20261013-abc123", "复利") == "这个链接已经存过了：复利\nid：20261013-abc123"


def test_answer():
    a = Answer("答案 [1]", [Source(1, "20261013-aaaaaa", "复利", "https://a.com"), Source(2, "20261013-bbbbbb", "笔记", None)],
               "local", True)
    assert format_answer(a) == ("答案 [1]\n\n来源：\n[1] 复利（id：20261013-aaaaaa） https://a.com\n"
                                "[2] 笔记（id：20261013-bbbbbb）\n\n（本次由本地模型回答）")
    assert format_answer(Answer("没有", [], "claude:sonnet", False)) == "没有"


def test_saved_ok_with_summary_failed_shows_note():
    text = format_saved(SaveOutcome(entry(), False, note="稍后发 /resummarize x", summary_failed=True))
    assert "稍后发 /resummarize x" in text


def test_saved_merged_head():
    assert format_saved(SaveOutcome(entry(), local_fallback=False, merged=True)).startswith("✅ 已补充到：复利")
