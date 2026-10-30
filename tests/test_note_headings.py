"""P25 Part B: a note written with language = "en" has English headings and lines; every reader takes both sets,
and a note keeps the set it was written with (existing Chinese notes keep working, whatever the language)."""
from datetime import datetime, timedelta, timezone

from test_app_media import make_app, m, staged

from octopus import i18n
from octopus.commands import read_parts
from octopus.library import Entry, attachment_links, parse, render

CST = timezone(timedelta(hours=8))
ZH_NOTE = """---
id: 20261013-abc123
title: 复利
source: manual
saved_at: '2026-10-13T09:30:00+08:00'
channel: tg
tags: []
status: ok
---

## 摘要

复利需要时间。

## 要点

- 早点开始

## 我的备注

（2026-10-13）重读

## 原文或转写稿

正文
附件：attachments/20261013-abc123/a.png

## 附件

%% octopus:附件（自动生成） %%
![[attachments/20261013-abc123/a.png]]
%% /octopus:附件 %%
"""
EN_NOTE = ZH_NOTE.replace("## 摘要", "## Summary").replace("## 要点", "## Key points").replace(
    "## 我的备注", "## My comments").replace("## 原文或转写稿", "## Original text").replace(
    "附件：attachments", "Attachment: attachments").replace("## 附件", "## Attachments").replace(
    "%% octopus:附件（自动生成） %%", "%% octopus:attachments (generated) %%").replace(
    "%% /octopus:附件 %%", "%% /octopus:attachments %%")


def entry(**kw):
    base = dict(id="20261013-abc123", title="Compounding", source="manual",
                saved_at=datetime(2026, 10, 13, 9, 30, tzinfo=CST), channel="tg", summary="It takes time.",
                points=["Start early"], notes="(2026-10-13) read again", body="Text\nAttachment: attachments/20261013-abc123/a.png")
    base.update(kw)
    return Entry(**base)


def test_both_heading_sets_parse_to_the_same_entry():
    zh, en = parse(ZH_NOTE), parse(EN_NOTE)
    for e in (zh, en):
        assert (e.summary, e.points, e.title) == ("复利需要时间。", ["早点开始"], "复利")
        assert e.body.startswith("正文\n") and "octopus:" not in e.body  # the links block is dropped
    assert zh.notes == "（2026-10-13）重读" and en.notes == zh.notes
    assert (zh.heading_lang, en.heading_lang) == ("zh", "en")


def test_a_new_note_gets_the_configured_languages_headings():
    i18n.set_language("en")
    text = render(entry())
    assert "\n## Summary\n\nIt takes time.\n\n## Key points\n\n- Start early\n\n## My comments\n\n" in text
    assert "\n## Original text\n\nText\n" in text and "\n## Attachments\n\n%% octopus:attachments (generated) %%\n" in text
    assert parse(text) == entry()
    i18n.set_language("zh")
    assert "\n## 摘要\n" in render(entry()) and "## Summary" not in render(entry())


def test_a_note_keeps_its_own_headings_whatever_the_language():
    i18n.set_language("en")
    assert "\n## 摘要\n" in render(parse(ZH_NOTE)) and "## Summary" not in render(parse(ZH_NOTE))
    i18n.set_language("zh")
    assert "\n## Summary\n" in render(parse(EN_NOTE)) and "## 摘要" not in render(parse(EN_NOTE))
    assert render(parse(EN_NOTE)) == EN_NOTE and render(parse(ZH_NOTE)) == ZH_NOTE


def test_attachment_lines_of_either_language_get_links():
    assert "![[attachments/x/a.png]]" in attachment_links("附件：attachments/x/a.png", "en")
    assert "![[attachments/x/a.png]]" in attachment_links("Attachment: attachments/x/a.png", "zh")


def test_read_shows_no_attachment_lines_of_either_language():
    i18n.set_language("en")
    text = "\n".join(read_parts(parse(EN_NOTE)))
    assert "Attachment:" not in text and "attachments/" not in text


async def test_an_english_image_save_writes_english_lines_and_title(tmp_path):
    app, ch, media = make_app(tmp_path)
    media.ocr = lambda path: ""  # nothing recognized
    for provider in app.llm.providers.values():
        provider.error = "down"  # no summary: the title stays the fallback
    i18n.set_language("en")
    await app.handle(m("", [staged(tmp_path, "a.png")]))
    await app.process_due_jobs()
    [row] = app.index.recent(1)
    saved = app.library.get(row["id"])
    assert "### Image 1 (a.png)" in saved.body and f"Attachment: attachments/{row['id']}/a.png" in saved.body
    text = app.library.find(row["id"]).read_text(encoding="utf-8")
    assert "## Original text" in text and "## 摘要" not in text and ch.sent[-1][1].startswith("⚠️ Saved (incomplete)")


async def test_the_title_fallback_is_english(tmp_path):
    from octopus.fetchers.base import FetchResult

    app, ch, _ = make_app(tmp_path)
    for provider in app.llm.providers.values():
        provider.error = "down"
    i18n.set_language("en")
    out = await app.pipeline.store(FetchResult(url=None, source="manual", text=""), "tg")
    assert out.entry.title == "Note"


async def test_merges_and_split_work_on_english_notes(tmp_path):
    app, ch, _ = make_app(tmp_path)
    i18n.set_language("en")
    await app.handle(m("/s first part"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    await app.handle(m(f"/add {eid}"))
    await app.handle(m("", [staged(tmp_path, "b.png")]))
    await app.process_due_jobs()
    body = app.library.get(eid).body
    assert "\n## Added (" in body and "### Image 1 (b.png)" in body
    await app.handle(m("separate"))
    new_id = app.focus.get("tg", "me")
    assert new_id != eid and "b.png" in app.library.get(new_id).body and "b.png" not in app.library.get(eid).body
    assert "## Added (" not in app.library.get(new_id).body


async def test_comments_and_find_snippets_are_english(tmp_path):
    from octopus.pipeline import SavePipeline

    app, ch, _ = make_app(tmp_path)
    i18n.set_language("en")
    await app.handle(m("/s compounding takes patience"))
    await app.process_due_jobs()
    eid = app.focus.get("tg", "me")
    assert isinstance(app.pipeline, SavePipeline)
    await app.pipeline.annotate(eid, "zebra stripes")
    assert app.library.get(eid).notes.endswith(") zebra stripes") and "（" not in app.library.get(eid).notes
    await app.handle(m("/find zebra"))
    assert "zebra stripes" in ch.sent[-1][1] and "我的备注" not in ch.sent[-1][1]
    assert any("My comments: (2026" in chunk for row in app.index.texts() for chunk in row["chunks"])
