"""P25: with language = "en" the flows people meet first say everything in English (no CJK anywhere), from the
chat replies and buttons to the CLI; the Chinese default is covered by every other test."""
import json
import re

import pytest
from fakes import StubFetcher
from test_app import m
from test_app import make_app as _make_app

from octopus import host, i18n
from octopus.cli import main
from octopus.config import ChannelConfig, load_config, make_test_config
from octopus.fetchers.base import FetchResult
from octopus.models import IncomingMessage

CJK = re.compile("[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")
ARTICLE = FetchResult(url="https://a.com/x", source="web", title="The power of compounding",
                      text="Long-term investors believe in compounding. " * 30)


def english(text: str) -> str:
    assert not CJK.search(text), text
    return text


def make_app(tmp_path, **kw):
    app, ch, providers = _make_app(tmp_path, answer_reply="Compounding takes time [1].", **kw)
    providers["claude:haiku"].respond = json.dumps({
        "title": "The power of compounding", "summary": "Compounding rewards patience.",
        "points": ["Start early", "Stay invested"], "tags": ["investing"], "language": "en"})
    i18n.set_language("en")  # what load_config does for language = "en"
    return app, ch, providers


def sent(ch):
    return [english(text) for _, text in ch.sent]


def tap(data, user="me"):
    return IncomingMessage(channel="tg", user_id=user, text="", callback=data)


async def save_note(app, text="Compounding needs patience"):
    await app.handle(m(f"/s {text}"))
    await app.process_due_jobs()
    return app.index.recent(1)[0]["id"]


def test_language_comes_from_the_config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('language = "en"\n', encoding="utf-8")
    assert load_config(path).language == "en" and i18n.get_language() == "en"
    path.write_text("max_concurrent_jobs = 2\n", encoding="utf-8")
    assert load_config(path).language == "zh" and i18n.get_language() == "zh"  # the default


def test_config_errors_are_in_the_configs_language(tmp_path):
    from octopus.config import ConfigError

    path = tmp_path / "config.toml"
    path.write_text('language = "en"\nmax_concurrent_jobs = 0\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert english(str(e.value)) == "Config error: max_concurrent_jobs should be a whole number of at least 1, not 0"
    path.write_text('language = "fr"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "language" in str(e.value) and '"en"' in str(e.value)


async def test_help_and_its_buttons(tmp_path):
    app, ch, _ = make_app(tmp_path)
    await app.handle(m("/help"))
    text, buttons = ch.sent_full[-1]
    assert english(text).startswith("Hi! I'm Octopus") and "/find" in text and "/read" in text
    assert [b.label for b in buttons][:3] == ["Recent", "Saved today", "Status"]
    assert all(english(b.label) for b in buttons)
    await app.handle(m("hi"))
    assert ch.sent_full[-1][0] == text


async def test_saving_a_link_and_a_note(tmp_path):
    app, ch, _ = make_app(tmp_path, fetcher=StubFetcher(ARTICLE))
    await app.handle(m("look https://a.com/x"))
    assert sent(ch) == ["Got it, working on it…"]
    await app.process_due_jobs()
    eid = app.index.recent(1)[0]["id"]
    assert sent(ch)[-1].startswith("✅ Saved: The power of compounding") and sent(ch)[-1].endswith(f"id: {eid}")
    await app.handle(m("https://a.com/x"))
    assert sent(ch)[-1] == f"This link is already saved: The power of compounding\nid: {eid}"
    await save_note(app)
    text, buttons = ch.sent_full[-1]
    assert english(text).startswith("✅ Saved:") and [b.label for b in buttons] == ["Make it a question"]
    assert buttons[0].fallback == 'Reply "ask" to make it a question'


async def test_an_answer_and_its_sources(tmp_path):
    app, ch, _ = make_app(tmp_path, fetcher=StubFetcher(ARTICLE))
    await app.handle(m("https://a.com/x"))
    await app.process_due_jobs()
    eid = app.index.recent(1)[0]["id"]
    await app.handle(m("What does compounding do?"))
    text, buttons = ch.sent_full[-1]
    assert english(text) == f"Compounding takes time [1].\n\nSources:\n[1] The power of compounding (id: {eid}) https://a.com/x"
    assert [b.label for b in buttons] == ["Save this"]


async def test_delete_asks_first_then_deletes(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await save_note(app)
    await app.handle(m("/delete"))
    text, buttons = ch.sent_full[-1]
    assert english(text).startswith("Pick the entry to delete:") and buttons[0].label.startswith("Delete ")
    await app.handle(tap(f"del:{eid}"))
    text, buttons = ch.sent_full[-1]
    assert english(text).startswith("Delete this entry?\n") and [b.label for b in buttons] == ["Confirm", "Cancel"]
    await app.handle(tap(buttons[0].data))
    assert english(ch.sent[-1][1]).startswith("Deleted: ") and "trash" in ch.sent[-1][1]
    await app.handle(m(f"/delete {eid}"))
    assert sent(ch)[-1] == f"No entry {eid}."


async def test_recent_list_find_read_status_and_model(tmp_path):
    app, ch, _ = make_app(tmp_path)
    eid = await save_note(app, "Compounding needs patience and time")
    for command in ("/recent", "/list", "/find patience", "/find nothing-like-this", f"/read {eid}", "/status",
                    "/model", "/model local", "/model default", "/recent zero", "/read a b c", "/find"):
        await app.handle(m(command))
    out = sent(ch)[-12:]
    assert out[0].startswith("Recently saved:\n") and eid in out[0]
    assert out[1].startswith("Saved on ") and eid in out[1]
    assert out[2].startswith('Entries containing "patience" (1):') and out[2].endswith("Read one: /read id")
    assert out[3] == 'No entry contains "nothing-like-this".'
    assert out[4].startswith('"') and f"id: {eid}" in out[4] and "Text:\n" in out[4]
    assert out[5].startswith("Saved: 1\nTasks: ") and "Summaries: claude:haiku" in out[5]
    assert out[6] == "Models in use:\nSummaries: claude:haiku (falls back to local)\nAnswers: claude:sonnet (falls back to local)"
    assert out[7] == "Switched to the local model.\nSummaries: local\nAnswers: local"
    assert out[8].startswith("Back to the default models.")
    assert out[9] == "Usage: /recent or /recent 30 (at most 50)"
    assert out[10].startswith("Usage: /read id") and out[11].startswith("Usage: /find words")


async def test_errors_and_usages(tmp_path):
    app, ch, providers = make_app(tmp_path)
    await app.handle(m("/nosuchcommand"))
    assert sent(ch)[-1].startswith("No such command: /nosuchcommand\n\nHi! I'm Octopus")
    await app.handle(m("/read ffff99"))
    assert sent(ch)[-1] == "No entry ffff99."
    await app.handle(m("/add"))
    assert sent(ch)[-1].startswith("Usage: /add id")
    await app.handle(m("/done"))
    assert sent(ch)[-1] == "Nothing is being added to right now."
    providers["claude:sonnet"].error = providers["local"].error = "down"
    await app.handle(m("Anything about compounding?"))
    assert sent(ch)[-1].startswith("Can't answer right now: no model is available")


async def test_a_failed_save_is_said_in_english(tmp_path):
    from octopus.formatting import format_job_failed
    from octopus.media import MediaError

    app, ch, _ = make_app(tmp_path)
    assert english(format_job_failed(str(MediaError(i18n.t("pipeline.files_gone"))))) == (
        "❌ Not saved: The attached files are gone\nTry again later, or send screenshots, a screen recording or the "
        "pasted text.")


def test_telegram_menu_in_english():
    from octopus.channels.telegram import command_menu

    i18n.set_language("en")
    menu = dict(command_menu())
    assert menu["read"] == "read a whole entry (/read id)" and all(english(d) for d in menu.values())


def test_missing_secrets_name_the_store_of_the_platform(tmp_path, monkeypatch):
    from octopus.bootstrap import build_channels

    cfg = make_test_config(tmp_path)
    cfg.channels = {"telegram": ChannelConfig(True, ["1"])}

    class NoSecrets:
        def get(self, name):
            return None

    i18n.set_language("en")
    assert build_channels(cfg, NoSecrets(), notify=None)[1] == [
        "Telegram is enabled, but the Keychain has no telegram_token (run octopus secret set telegram_token)"]
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    assert build_channels(cfg, NoSecrets(), notify=None)[1] == [
        "Telegram is enabled, but the secrets file (or the environment) has no telegram_token "
        "(run octopus secret set telegram_token)"]
    i18n.set_language("zh")  # the Chinese wording names the right store too (int2 M3)
    assert build_channels(cfg, NoSecrets(), notify=None)[1] == [
        "Telegram 已启用，但密钥文件（或环境变量）里缺少 telegram_token（运行 octopus secret set telegram_token）"]


# ---- the CLI ----

def test_init_writes_the_english_template_and_speaks_english(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init", "--language", "en"]) == 0
    text = path.read_text(encoding="utf-8")
    assert 'language = "en"' in english(text) and "[models.summarize]" in text
    assert english(capsys.readouterr().out) == f"Config file written: {path}\n"
    assert load_config(path).language == "en"
    assert main(["--config", str(path), "init"]) == 0  # the config's own language from now on
    assert english(capsys.readouterr().out) == f"Config file already exists: {path}\n"


def test_init_without_language_on_a_chinese_system_writes_chinese(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    assert 'language = "zh"' in path.read_text(encoding="utf-8") and "已写入配置文件" in capsys.readouterr().out


def test_help_is_english_when_the_config_says_so(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text(f'language = "en"\ndata_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--config", str(path), "--help"])
    out = capsys.readouterr().out
    assert "use Octopus in this terminal" in english(out) and "write the default config file" in out


def test_service_status_and_models_in_english(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    import octopus.service as svc
    from octopus.secrets import keyring_label

    class _KeySecrets:
        def __init__(self, **stored):
            self.stored = stored

        def get(self, name):
            return self.stored.get(name)

        def find(self, name, env=True):
            return (self.stored[name], f"{keyring_label()} {name}") if name in self.stored else (None, None)

    path = tmp_path / "config.toml"
    path.write_text(f'language = "en"\ndata_dir = "{tmp_path / "data"}"\n[models.summarize]\n'
                    'primary = "anthropic:haiku"\n', encoding="utf-8")
    monkeypatch.setattr(svc, "status", lambda **kw: i18n.t("service.not_running"))
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _KeySecrets(anthropic_api_key="sk-ant-secret"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--config", str(path), "service", "status"]) == 0
    out = english(capsys.readouterr().out)
    assert out.startswith("The background service is not running.\nAPI keys:\nanthropic: set (Keychain anthropic_api_key)")
    assert main(["--config", str(path), "models"]) == 0
    out = english(capsys.readouterr().out)
    assert out.startswith("Summaries: anthropic:haiku (falls back to local)\nAnswers: claude:sonnet (falls back to local)\nAPI keys:")


def test_secret_list_in_english(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from test_cli import _Secrets

    path = tmp_path / "config.toml"
    path.write_text(f'language = "en"\ndata_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    status = {"telegram_token": (True, "Keychain"), "feishu_app_id": (False, None), "clawbot_token": (None, None)}
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _Secrets(status=status))
    assert main(["--config", str(path), "secret", "list"]) == 0
    out = english(capsys.readouterr().out).splitlines()
    assert out[0] == "telegram_token: set (Keychain)  Telegram bot token (from @BotFather)"
    assert out[1].startswith("feishu_app_id: not set  ") and out[2].startswith("clawbot_token: could not be read  ")


def test_the_cli_chat_channel_in_english(tmp_path):
    from octopus.channels.cli import CLIChannel

    prompts = []
    ch = CLIChannel(input_fn=lambda p: prompts.append(p) or "/quit", output=lambda s: None)
    i18n.set_language("en")
    import asyncio

    async def nothing(msg):
        pass

    asyncio.run(ch.start(nothing))
    assert prompts and set(prompts) == {"you> "}


def test_without_a_language_everything_stays_chinese(tmp_path):
    """The other tests' world: without language = "en" nothing changed."""
    _make_app(tmp_path)
    assert i18n.get_language() == "zh" and i18n.t("commands.help").startswith("你好！我是 Octopus")


async def test_save_this_answer_and_the_local_model_footer(tmp_path):
    app, ch, providers = make_app(tmp_path)
    providers["claude:sonnet"].error = "down"
    providers["local"].respond = "Patience pays [1]."
    await save_note(app)
    await app.handle(m("Why does patience matter?"))
    text, buttons = ch.sent_full[-1]
    assert english(text).endswith("(answered by the local model this time)") and buttons[0].label == "Save this"
    await app.handle(tap(buttons[0].data))
    await app.process_due_jobs()
    assert english(ch.sent[-1][1]).startswith("✅ Saved:")
    bodies = [app.library.get(r["id"]).body for r in app.index.recent(5)]
    assert any(body.startswith("Why does patience matter?\n\nAI answer:\nPatience pays [1].") for body in bodies)


def test_extension_reasons_in_english():
    from octopus.extension import parse_request

    i18n.set_language("en")
    for body, reason in ((b"x", "Not JSON"), (b'{"kind": "nope"}', "Unknown kind of save"),
                         (b'{"kind": "selection", "url": "https://a.com", "text": " "}', "No text selected")):
        with pytest.raises(ValueError) as e:
            parse_request(body)
        assert str(e.value) == reason
