import asyncio
import threading

import pytest
from fakes import FakeEmbedder, StubFetcher
from test_app import ARTICLE, make_app

from octopus.bootstrap import build_app
from octopus.channels.base import Reply
from octopus.channels.cli import CLIChannel
from octopus.cli import main, run_chat
from octopus.config import make_test_config
from octopus.db import connect
from octopus.embed import EmbedError
from octopus.fetchers.base import FetchError
from octopus.jobs import JobQueue
from octopus.library import Entry, Library


async def test_cli_channel_reads_until_eof():
    lines = iter(["你好", "", "/quit", "never"])
    got, printed = [], []

    def fake_input(prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    ch = CLIChannel(input_fn=fake_input, output=printed.append)

    async def on_message(msg):
        got.append((msg.channel, msg.user_id, msg.text))

    await asyncio.wait_for(ch.start(on_message), 3)
    assert got == [("cli", "local", "你好")]
    assert (await ch.send("local", Reply("hi"))).ok is True
    assert printed[-1] == "\nOctopus> hi\n"


async def test_build_app_wires_everything(tmp_path):
    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {"cli": CLIChannel()}, embedder=FakeEmbedder())
    assert cfg.library_dir.is_dir() and cfg.db_path.exists()
    assert set(app.llm.providers) == {"claude:haiku", "claude:sonnet", "local"}
    assert app.cfg is cfg


def test_init_writes_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # the default config's data_dir is ~/Octopus: never the real one
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    assert path.exists()
    assert (tmp_path / "Octopus").stat().st_mode & 0o777 == 0o700  # P4: private data dir from the first run
    assert main(["--config", str(path), "init"]) == 0
    assert "已存在" in capsys.readouterr().out


def test_reindex(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    import octopus.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "OllamaEmbedder", lambda *a, **k: FakeEmbedder())
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    Library(tmp_path / "library", tmp_path / "trash").save(
        Entry(id="20261013-aaaaaa", title="复利", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
              channel="cli", body="复利需要时间"))
    assert main(["--config", str(path), "reindex"]) == 0
    assert "已重建索引：1 条" in capsys.readouterr().out


def scripted(*lines):
    it = iter(lines)

    def fake_input(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    return fake_input


async def test_quit_waits_for_running_handler():
    done = []

    async def slow(msg):
        await asyncio.sleep(0.2)
        done.append(msg.text)

    ch = CLIChannel(input_fn=scripted("hello", "/quit"), output=lambda s: None)
    await asyncio.wait_for(ch.start(slow), 3)
    assert done == ["hello"]


async def test_run_chat_saves_link_then_exits_on_eof(tmp_path):
    printed = []
    ch = CLIChannel(input_fn=scripted("https://a.com/x"), output=printed.append)
    app, _, _ = make_app(tmp_path, StubFetcher(ARTICLE), channel=ch)
    await asyncio.wait_for(run_chat(app, ch), 10)
    assert [e.title for _, e in app.library.iter_entries()] == ["复利的力量"]
    assert any("✅ 已保存：复利的力量" in s for s in printed)
    assert not any("没完成" in s for s in printed)


async def test_run_chat_exits_promptly_with_retrying_job(tmp_path):
    printed = []
    ch = CLIChannel(input_fn=scripted("https://a.com/x"), output=printed.append)
    app, _, _ = make_app(tmp_path, StubFetcher(FetchError("boom", retryable=True)), channel=ch)
    await asyncio.wait_for(run_chat(app, ch), 5)
    assert any("还有 1 个任务没完成，下次启动会继续。" in s for s in printed)
    conn = connect(app.cfg.db_path, embed_dim=FakeEmbedder.dim)
    assert JobQueue(conn).counts() == {"queued": 1}


def test_reindex_reports_embedder_failure(tmp_path, monkeypatch, capsys):
    import octopus.bootstrap as bootstrap

    class DownEmbedder(FakeEmbedder):
        async def embed(self, texts):
            raise EmbedError("connection refused")

    monkeypatch.setattr(bootstrap, "OllamaEmbedder", lambda *a, **k: DownEmbedder())
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    from datetime import datetime, timezone

    Library(tmp_path / "library", tmp_path / "trash").save(
        Entry(id="20261013-aaaaaa", title="复利", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
              channel="cli", body="复利需要时间"))
    assert main(["--config", str(path), "reindex"]) == 1
    out = capsys.readouterr().out
    assert "重建索引失败：" in out and "connection refused" in out and "请确认 Ollama 正在运行" in out


class GatedFetcher(StubFetcher):
    """fetch() signals `entered`, then blocks until `release` is set."""

    def __init__(self, result):
        super().__init__(result)
        self.entered = threading.Event()
        self.release = threading.Event()

    def fetch(self, url):
        self.entered.set()
        assert self.release.wait(5)
        return super().fetch(url)


def gated_input(fetcher, url="https://a.com/x"):
    """Yields one url, then EOF only once the worker is inside fetch (no sleeps)."""
    sent = []

    def input_fn(prompt):
        if not sent:
            sent.append(url)
            return url
        assert fetcher.entered.wait(5)
        raise EOFError

    return input_fn


async def test_run_chat_waits_for_job_held_by_worker(tmp_path):
    fetcher = GatedFetcher(ARTICLE)
    printed = []
    ch = CLIChannel(input_fn=gated_input(fetcher), output=printed.append)
    app, _, _ = make_app(tmp_path, fetcher, channel=ch)
    threading.Timer(0.3, fetcher.release.set).start()
    await asyncio.wait_for(run_chat(app, ch), 10)
    assert any("✅ 已保存：复利的力量" in s for s in printed)
    assert not any("没完成" in s for s in printed)


async def test_cancelled_run_chat_lets_worker_job_finish_and_closes(tmp_path):
    fetcher = GatedFetcher(ARTICLE)
    printed = []
    ch = CLIChannel(input_fn=gated_input(fetcher), output=printed.append)
    app, _, _ = make_app(tmp_path, fetcher, channel=ch)
    closed = []
    real_close = app.close
    app.close = lambda: (closed.append(True), real_close())

    task = asyncio.create_task(run_chat(app, ch))
    for _ in range(200):  # wait until run_chat is draining: EOF seen, wait notice printed
        if any("等待" in s for s in printed):
            break
        await asyncio.sleep(0.025)
    else:
        raise AssertionError("run_chat never reached the drain step")
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.sleep(0.1)
    fetcher.release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert closed == [True]
    assert any("✅ 已保存：复利的力量" in s for s in printed)
    conn = connect(app.cfg.db_path, embed_dim=FakeEmbedder.dim)
    assert JobQueue(conn).counts() == {}


async def test_cli_turns_dragged_file_into_attachment(tmp_path):
    src = tmp_path / "my shot.png"
    src.write_bytes(b"png")
    lines = iter([f"'{src}'", str(src).replace(" ", "\\ "), "plain text"])

    def fake_input(prompt):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    got = []

    async def on_message(msg):
        got.append(msg)

    ch = CLIChannel(input_fn=fake_input, output=lambda s: None, incoming_dir=tmp_path / "incoming")
    await asyncio.wait_for(ch.start(on_message), 3)
    assert [len(m.attachments) for m in got] == [1, 1, 0]
    a = got[0].attachments[0]
    assert a.kind == "image" and a.filename == "my shot.png"
    assert a.path.parent.parent == tmp_path / "incoming" and a.path.read_bytes() == b"png"
    assert src.exists()  # original untouched
    assert got[0].text == "" and got[2].text == "plain text"


async def _run_lines(tmp_path, lines, **kw):
    it = iter(lines)

    def fake_input(prompt):
        try:
            return next(it)
        except StopIteration:
            raise EOFError

    got, printed = [], []

    async def on_message(msg):
        got.append(msg)

    ch = CLIChannel(input_fn=fake_input, output=printed.append, incoming_dir=tmp_path / "incoming", **kw)
    await asyncio.wait_for(ch.start(on_message), 3)
    return got, printed


async def test_cli_text_that_merely_looks_like_a_path_is_text(tmp_path):
    long_path = "/" + "a" * 300 + "/x.png"
    lines = ["~ 看看", "~~删除线~~", "~", "~nosuchuser_zzz/x.png", long_path, "/etc/not-a-file-zzz", "/status"]
    got, printed = await _run_lines(tmp_path, lines)
    assert [m.text for m in got] == lines
    assert all(not m.attachments for m in got)


async def test_cli_unescapes_terminal_backslashes(tmp_path):
    src = tmp_path / "Screenshot (1).png"
    src.write_bytes(b"png")
    escaped = str(src).replace(" ", "\\ ").replace("(", "\\(").replace(")", "\\)")
    assert "\\(" in escaped
    got, _ = await _run_lines(tmp_path, [escaped])
    assert got[0].attachments[0].filename == "Screenshot (1).png"


async def test_cli_expands_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "pic.jpg").write_bytes(b"jpg")
    got, _ = await _run_lines(tmp_path, ["~/pic.jpg"])
    assert got[0].attachments[0].kind == "image"


async def test_cli_rejects_oversized_file(tmp_path, monkeypatch):
    import octopus.channels.cli as cli_mod

    monkeypatch.setattr(cli_mod, "MAX_FILE_BYTES", 2)
    src = tmp_path / "big.bin"
    src.write_bytes(b"123")
    got, printed = await _run_lines(tmp_path, [str(src), "next"])
    assert [m.text for m in got] == ["next"]
    assert any("文件太大（超过 2 GB），没有保存" in p for p in printed)
    assert not (tmp_path / "incoming").exists() or not any((tmp_path / "incoming").iterdir())


async def test_cli_copy_failure_is_reported_and_loop_continues(tmp_path, monkeypatch):
    import octopus.channels.cli as cli_mod

    src = tmp_path / "a.png"
    src.write_bytes(b"png")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cli_mod.shutil, "copy2", boom)
    got, printed = await _run_lines(tmp_path, [str(src), "next"])
    assert [m.text for m in got] == ["next"]
    assert any("复制文件失败：" in p and "disk full" in p for p in printed)


async def test_cli_copy_runs_off_the_event_loop(tmp_path, monkeypatch):
    import threading

    import octopus.channels.cli as cli_mod

    src = tmp_path / "a.png"
    src.write_bytes(b"png")
    real = cli_mod.shutil.copy2
    threads = []

    def spy(*a, **k):
        threads.append(threading.current_thread())
        return real(*a, **k)

    monkeypatch.setattr(cli_mod.shutil, "copy2", spy)
    await _run_lines(tmp_path, [str(src)])
    assert threads and threads[0] is not threading.main_thread()


def test_aiff_voice_files_are_audio(tmp_path):
    # macOS `say` / QuickTime produce .aiff; it must be transcribed, not stored as an unreadable "file"
    from octopus.channels.cli import _kind
    for name in ("voice.aiff", "voice.AIF"):
        assert _kind(tmp_path / name) == "audio"


def test_cli_uses_shared_media_suffixes(tmp_path):
    from octopus.channels.cli import _kind
    from octopus.media import AUDIO_SUFFIXES, IMAGE_SUFFIXES, VIDEO_SUFFIXES
    assert ".aiff" in AUDIO_SUFFIXES and ".aif" in AUDIO_SUFFIXES
    for suffixes, kind in ((IMAGE_SUFFIXES, "image"), (AUDIO_SUFFIXES, "audio"), (VIDEO_SUFFIXES, "video")):
        for ext in suffixes:
            assert _kind(tmp_path / f"x{ext}") == kind


async def test_build_app_restricts_attachments_to_incoming_dir(tmp_path):
    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {"cli": CLIChannel()}, embedder=FakeEmbedder())
    try:
        assert app.pipeline.media.incoming_dir == cfg.incoming_dir
    finally:
        app.close()


async def test_cli_prints_button_fallbacks():
    from octopus.channels.base import Button

    printed = []
    ch = CLIChannel(output=printed.append)
    await ch.send("local", Reply("✅ 已保存", [Button("改成提问", "fix:1", "回复“问”改成提问")]))
    assert printed[-1] == "\nOctopus> ✅ 已保存\n回复“问”改成提问\n"
    await ch.send("local", Reply("说明", [Button("最近存的", "cmd:recent", ""), Button("x", "del:1", "用法")]))
    assert printed[-1] == "\nOctopus> 说明\n用法\n"
    await ch.send("local", Reply("说明", [Button("最近存的", "cmd:recent", "")]))
    assert printed[-1] == "\nOctopus> 说明\n"


def test_secret_commands(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    store = {}

    class FakeSecrets:
        def set(self, name, value):
            store[name] = value

        def status(self):
            return {"telegram_token": ("telegram_token" in store, "钥匙串"), "feishu_app_id": (False, None)}

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "secret", "set", "telegram_token"]) == 0
    assert store == {"telegram_token": "123:abc"}
    assert main(["--config", str(cfg), "secret", "list"]) == 0
    out = capsys.readouterr().out
    assert "telegram_token：已设置" in out and "feishu_app_id：未设置" in out
    assert main(["--config", str(cfg), "secret", "set", "nope"]) == 2


async def test_chat_refuses_when_another_instance_runs(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.instance import InstanceLock

    cfg = make_test_config(tmp_path)
    held = InstanceLock(cfg.data_dir / "octopus.lock")
    assert held.acquire()
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: (_ for _ in ()).throw(AssertionError("keychain touched")))
    try:
        await cli_mod._chat(cfg)
    finally:
        held.release()
    assert "已经在运行" in capsys.readouterr().out


# ---- final review ----

async def test_chat_lock_conflict_exits_1(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    from octopus.instance import InstanceLock

    cfg = make_test_config(tmp_path)
    held = InstanceLock(cfg.data_dir / "octopus.lock")
    assert held.acquire()
    try:
        assert await cli_mod._chat(cfg) == 1  # M2
    finally:
        held.release()


def test_reindex_refuses_while_service_runs(tmp_path, monkeypatch, capsys):
    import octopus.bootstrap as bootstrap
    from octopus.instance import InstanceLock

    monkeypatch.setattr(bootstrap, "OllamaEmbedder", lambda *a, **k: (_ for _ in ()).throw(AssertionError("built")))
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    held = InstanceLock(tmp_path / "octopus.lock")
    assert held.acquire()
    try:
        assert main(["--config", str(path), "reindex"]) == 1  # M9
    finally:
        held.release()
    assert "Octopus 正在运行（另一个窗口或后台服务），先关掉它再重建索引。" in capsys.readouterr().out


def test_backup_command(tmp_path, capsys):
    from datetime import date

    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    assert main(["--config", str(path), "backup"]) == 0
    target = tmp_path / "backups" / f"octopus-{date.today():%Y%m%d}.db"
    assert target.exists()
    assert f"已备份：{target}" in capsys.readouterr().out
    assert main(["--config", str(path), "backup"]) == 0
    assert f"今天已经备份过了：{target}" in capsys.readouterr().out


@pytest.mark.parametrize("cmd", ["chat", "run", "reindex", "backup", "clawbot-login"])
def test_bad_config_exits_2_with_message(tmp_path, capsys, cmd):
    bad = tmp_path / "config.toml"
    bad.write_text("data_dir = \n", encoding="utf-8")
    assert main(["--config", str(bad), cmd]) == 2
    out = capsys.readouterr().out
    assert "配置文件格式有误" in out and "Traceback" not in out


def test_unknown_config_key_exits_2(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.write_text('colour = "red"\n', encoding="utf-8")
    assert main(["--config", str(bad), "chat"]) == 2
    assert "配置文件里有不认识的设置：colour" in capsys.readouterr().out


# ---- P4 Task 5: carry-overs ----

def _private(path):
    return path.is_dir() and path.stat().st_mode & 0o777 == 0o700


def test_init_makes_the_configs_data_dir_private(tmp_path, capsys):
    data = tmp_path / "data"
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{data}"\n', encoding="utf-8")
    assert main(["--config", str(path), "init"]) == 0
    assert "已存在" in capsys.readouterr().out
    assert _private(data) and _private(data / "logs")


def test_setup_logging_makes_the_data_dir_private(tmp_path):
    import logging

    from octopus import bootstrap

    cfg = make_test_config(tmp_path / "Octopus")
    bootstrap.setup_logging(cfg)
    try:
        assert _private(cfg.data_dir) and _private(cfg.logs_dir)
    finally:
        root = logging.getLogger()
        for h in [h for h in root.handlers if getattr(h, bootstrap._HANDLER_TAG, False)]:
            root.removeHandler(h)
            h.close()


async def test_clawbot_login_makes_the_data_dir_private(tmp_path, monkeypatch):
    import octopus.channels.clawbot as clawbot_mod
    import octopus.cli as cli_mod
    from octopus.channels.ilink import LoginResult

    async def fake_login(secrets, state_path, qr_path):
        return LoginResult(bot_token="tok", base_url="https://x", bot_id="bot", user_id="wx_owner")

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: object())
    monkeypatch.setattr(clawbot_mod, "login_interactive", fake_login)
    cfg = make_test_config(tmp_path / "Octopus")
    assert await cli_mod._clawbot_login(cfg) == 0
    assert _private(cfg.data_dir)


def test_backup_command_makes_the_data_dir_private(tmp_path, monkeypatch):
    import octopus.cli as cli_mod

    data = tmp_path / "Octopus"
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{data}"\n[embed]\ndim = 16\n', encoding="utf-8")
    seen = {}
    real = cli_mod._backup

    def spy(cfg):
        seen["private"] = _private(cfg.data_dir)
        return real(cfg)

    monkeypatch.setattr(cli_mod, "_backup", spy)
    assert main(["--config", str(path), "backup"]) == 0
    assert seen["private"] is True  # before any backup work starts
    assert _private(data / "backups")


@pytest.mark.parametrize("text", ['max_concurrent_jobs = "lots"\n', "[channels.telegram]\nallowed_users = 5\n"])
def test_config_with_wrong_value_types_exits_2(tmp_path, capsys, text):
    bad = tmp_path / "config.toml"
    bad.write_text(text, encoding="utf-8")
    assert main(["--config", str(bad), "chat"]) == 2
    out = capsys.readouterr().out
    assert out.startswith("配置文件有误：") and "Traceback" not in out


def test_config_path_that_cannot_be_read_exits_2(tmp_path, capsys):
    bad = tmp_path / "config.toml"
    bad.mkdir()  # IsADirectoryError
    assert main(["--config", str(bad), "backup"]) == 2
    assert capsys.readouterr().out.startswith("配置文件有误：")


class _Secrets:
    def __init__(self, status=None, set_error=None):
        self.stored, self._status, self.set_error = {}, status or {}, set_error

    def set(self, name, value):
        if self.set_error:
            raise self.set_error
        self.stored[name] = value

    def status(self):
        return self._status


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_secret_set_cancelled_at_the_prompt(tmp_path, monkeypatch, capsys, exc):
    import octopus.cli as cli_mod

    secrets = _Secrets()
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: secrets)

    def prompt(text):
        raise exc

    monkeypatch.setattr(cli_mod.getpass, "getpass", prompt)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "secret", "set", "telegram_token"]) == 1
    assert capsys.readouterr().out.strip().endswith("已取消。")
    assert secrets.stored == {}


def test_secret_set_reports_keychain_errors(tmp_path, monkeypatch, capsys):
    import keyring.errors

    import octopus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _Secrets(set_error=keyring.errors.KeyringLocked("locked")))
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: "123:abc")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "secret", "set", "telegram_token"]) == 1
    out = capsys.readouterr().out
    assert "保存失败：钥匙串不可用（" in out and "Traceback" not in out and "123:abc" not in out


def test_secret_list_tells_unreadable_from_unset(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    status = {"telegram_token": (True, "钥匙串"), "feishu_app_id": (False, None), "clawbot_token": (None, None)}
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _Secrets(status=status))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "secret", "list"]) == 0
    out = capsys.readouterr().out
    assert "telegram_token：已设置（钥匙串）" in out and "feishu_app_id：未设置" in out and "clawbot_token：读取失败" in out


def test_extension_token_is_made_once_and_printed(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    store = {}

    class FakeSecrets:
        def get(self, name):
            return store.get(name)

        def set(self, name, value):
            store[name] = value

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token"]) == 0
    token = store["extension_token"]
    assert len(token) >= 43  # token_urlsafe(32): 32 random bytes
    out = capsys.readouterr().out
    assert token in out and "enabled = true" in out  # not enabled yet: says how
    assert main(["--config", str(cfg), "extension", "token"]) == 0
    assert store["extension_token"] == token and token in capsys.readouterr().out


def test_extension_token_when_the_keychain_refuses(tmp_path, monkeypatch, capsys):
    import keyring.errors

    import octopus.cli as cli_mod

    class FakeSecrets:
        def get(self, name):
            return None

        def set(self, name, value):
            raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n[channels.extension]\nenabled = true\n', encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token"]) == 1
    out = capsys.readouterr().out
    assert "钥匙串" in out and "enabled = true" not in out


# ---- P20: API keys, `octopus models`, service status ----

API_CONFIG = ('[models.summarize]\nprimary = "anthropic:haiku"\nfallback = "openai:deepseek"\n'
              '[models.answer]\nprimary = "claude:sonnet"\nfallback = "openai:lm"\n'
              '[providers.deepseek]\nbase_url = "https://api.deepseek.com/v1"\nmodel = "deepseek-chat"\n'
              '[providers.lm]\nbase_url = "http://127.0.0.1:1234/v1"\nmodel = "qwen"\n')


class _KeySecrets:
    def __init__(self, **stored):
        self.stored = stored

    def get(self, name):
        return self.stored.get(name)

    def find(self, name, env=True):
        return (self.stored[name], f"钥匙串 {name}") if name in self.stored else (None, None)


def _api_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n' + API_CONFIG, encoding="utf-8")
    return cfg


def test_secret_set_accepts_provider_api_keys(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    secrets = _Secrets()
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: secrets)
    prompts = []
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt: prompts.append(prompt) or "sk-1")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "secret", "set", "deepseek_api_key"]) == 0
    assert main(["--config", str(cfg), "secret", "set", "anthropic_api_key"]) == 0
    assert secrets.stored == {"deepseek_api_key": "sk-1", "anthropic_api_key": "sk-1"}
    assert "deepseek" in prompts[0] and "Anthropic" in prompts[1]
    assert main(["--config", str(cfg), "secret", "set", "Bad Name"]) == 2
    assert "<名字>_api_key" in capsys.readouterr().out


def test_models_shows_chains_and_where_keys_are_without_values(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _KeySecrets(deepseek_api_key="sk-ds-secret"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.delenv("OCTOPUS_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OCTOPUS_LM_API_KEY", raising=False)
    assert main(["--config", str(_api_config(tmp_path)), "models"]) == 0
    out = capsys.readouterr().out
    assert "总结：anthropic:haiku（出错时改用 openai:deepseek）" in out and "回答：claude:sonnet（出错时改用 openai:lm）" in out
    assert "anthropic：已设置（环境变量 ANTHROPIC_API_KEY）" in out
    assert "deepseek：已设置（钥匙串 deepseek_api_key）" in out
    assert "lm：未设置" in out and "OCTOPUS_LM_API_KEY" in out
    assert "sk-ant-secret" not in out and "sk-ds-secret" not in out


def test_models_with_only_the_subscription_and_local_needs_no_key(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: (_ for _ in ()).throw(AssertionError("Keychain read")))
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert main(["--config", str(cfg), "models"]) == 0
    out = capsys.readouterr().out
    assert "总结：claude:haiku（出错时改用 local）" in out and "API key" not in out


def test_models_test_calls_each_provider_once(tmp_path, monkeypatch, capsys):
    from fakes import FakeProvider

    import octopus.cli as cli_mod

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _KeySecrets())
    fakes = {"local": FakeProvider("local", "OK"), "anthropic:haiku": FakeProvider("anthropic:haiku", error="key 不对"),
             "claude:sonnet": FakeProvider("claude:sonnet", "OK")}
    monkeypatch.setattr(cli_mod, "build_providers", lambda cfg, claude_token=None: fakes)
    assert main(["--config", str(_api_config(tmp_path)), "models", "test"]) == 1
    out = capsys.readouterr().out
    assert "local：可用" in out and "claude:sonnet：可用" in out and "anthropic:haiku：不可用：key 不对" in out
    assert all(len(p.calls) == 1 for p in fakes.values())
    del fakes["anthropic:haiku"]
    assert main(["--config", str(_api_config(tmp_path)), "models", "test"]) == 0


def test_service_status_lists_api_keys(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod
    import octopus.service as svc

    monkeypatch.setattr(svc, "status", lambda **kw: "后台服务没有在运行。")
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: _KeySecrets(anthropic_api_key="sk-ant-secret"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--config", str(_api_config(tmp_path)), "service", "status"]) == 0
    out = capsys.readouterr().out
    assert "后台服务没有在运行。" in out and "anthropic：已设置（钥匙串 anthropic_api_key）" in out
    assert "sk-ant-secret" not in out
    bad = tmp_path / "bad.toml"
    bad.write_text("data_dir = \n", encoding="utf-8")
    assert main(["--config", str(bad), "service", "status"]) == 0  # a broken config does not hide the status
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "后台服务没有在运行。" and len(out) == 2 and out[1].startswith("配置文件格式有误")


def test_models_test_waits_longer_than_the_retries():
    from octopus.cli import MODELS_TEST_TIMEOUT
    from octopus.llm.api import ATTEMPTS, MAX_RETRY_AFTER

    assert MODELS_TEST_TIMEOUT > (ATTEMPTS - 1) * MAX_RETRY_AFTER + 20


# ---- P18: library_dir, Obsidian ----

def test_init_marks_the_library_folder_and_reindex_needs_the_mark(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    import octopus.bootstrap as bootstrap
    from octopus.db import connect
    from octopus.library import MARKER

    monkeypatch.setattr(bootstrap, "OllamaEmbedder", lambda *a, **k: FakeEmbedder())
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    Library(tmp_path / "library", tmp_path / "trash").save(
        Entry(id="20261013-aaaaaa", title="复利", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
              channel="cli", body="复利需要时间"))
    assert main(["--config", str(path), "reindex"]) == 0  # no entries indexed yet: the first start marks it
    (tmp_path / "library" / MARKER).unlink()  # an upgraded library: entries there, no mark yet
    conn = connect(tmp_path / "octopus.db", embed_dim=16)
    conn.execute("DELETE FROM meta WHERE key = 'library_id'")
    conn.close()
    assert main(["--config", str(path), "reindex"]) == 0  # its files are where the index says: marked again
    assert (tmp_path / "library" / MARKER).exists()
    (tmp_path / "library").rename(tmp_path / "elsewhere")  # moved away; an empty one made again
    (tmp_path / "library").mkdir()
    assert main(["--config", str(path), "reindex"]) == 1
    out = capsys.readouterr().out
    assert "library adopt" in out and "octopus init" in out
    assert main(["--config", str(path), "init"]) == 0  # the owner says this empty folder is it after all
    assert (tmp_path / "library" / MARKER).exists()
    assert main(["--config", str(path), "reindex"]) == 0


def test_reindex_names_a_marker_deleted_after_the_upgrade(tmp_path, monkeypatch, capsys):
    # P18 review R2-2: the notes are all there and only the marker is gone: not "no notes in this folder"
    from datetime import datetime, timezone

    import octopus.bootstrap as bootstrap
    from octopus.library import MARKER

    monkeypatch.setattr(bootstrap, "OllamaEmbedder", lambda *a, **k: FakeEmbedder())
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    Library(tmp_path / "library", tmp_path / "trash").save(
        Entry(id="20261013-aaaaaa", title="复利", source="web", saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
              channel="cli", body="复利需要时间"))
    assert main(["--config", str(path), "reindex"]) == 0  # marked, the identity recorded
    (tmp_path / "library" / MARKER).unlink()
    capsys.readouterr()
    assert main(["--config", str(path), "reindex"]) == 1
    out = capsys.readouterr().out
    assert "标记文件 .octopus-library 不见了" in out and "octopus init" in out and "也没有索引里的笔记" not in out
    assert main(["--config", str(path), "init"]) == 0
    assert main(["--config", str(path), "reindex"]) == 0


def test_library_sync_lists_then_applies_held_deletes(tmp_path, monkeypatch, capsys):
    import asyncio
    from datetime import datetime, timezone

    from octopus.db import connect
    from octopus.index import Index
    from octopus.instance import InstanceLock
    from octopus.sync import claim_library

    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n[embed]\ndim = 16\n', encoding="utf-8")
    lib = Library(tmp_path / "library", tmp_path / "trash")
    conn = connect(tmp_path / "octopus.db", embed_dim=16)
    idx = Index(conn, FakeEmbedder())
    files = []
    for i in range(8):
        e = Entry(id=f"20261013-aaaaa{i}", title=f"笔记{i}", source="web",
                  saved_at=datetime(2026, 10, 13, tzinfo=timezone.utc), channel="cli", body=f"正文{i}")
        files.append(lib.save(e))
        asyncio.run(idx.upsert(e, files[-1]))
    claim_library(lib.root, conn)
    conn.close()
    for f in files[:6]:
        f.unlink()
    lock = InstanceLock(tmp_path / "octopus.lock")
    assert lock.acquire()
    try:
        assert main(["--config", str(path), "library", "sync", "--apply-deletes"]) == 1
        assert "正在运行" in capsys.readouterr().out
    finally:
        lock.release()
    assert main(["--config", str(path), "library", "sync"]) == 0
    out = capsys.readouterr().out
    assert "6 条" in out and "--apply-deletes" in out
    assert main(["--config", str(path), "library", "sync", "--apply-deletes"]) == 0
    assert "已从索引里去掉 6 条" in capsys.readouterr().out
    conn = connect(tmp_path / "octopus.db", embed_dim=16)
    try:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
    finally:
        conn.close()


def test_extension_token_copy_puts_it_on_the_clipboard(tmp_path, monkeypatch, capsys):
    """P23: `octopus extension token --copy` for pasting into the Shortcuts' import question."""
    import octopus.cli as cli_mod

    store = {"extension_token": "tok-123"}
    ran = []

    class FakeSecrets:
        def get(self, name):
            return store.get(name)

        def set(self, name, value):
            store[name] = value

    def fake_run(cmd, **kw):
        ran.append((cmd, kw.get("input")))

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n[channels.extension]\nenabled = true\n', encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token", "--copy"]) == 0
    assert ran == [(["pbcopy"], b"tok-123")]
    assert cli_mod.TOKEN_COPIED in capsys.readouterr().out
    ran.clear()
    assert main(["--config", str(cfg), "extension", "token"]) == 0
    assert ran == []  # only when asked


def test_extension_token_copy_when_pbcopy_is_missing(tmp_path, monkeypatch, capsys):
    import octopus.cli as cli_mod

    class FakeSecrets:
        def get(self, name):
            return "tok-123"

    def no_pbcopy(cmd, **kw):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    monkeypatch.setattr(cli_mod.subprocess, "run", no_pbcopy)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n[channels.extension]\nenabled = true\n', encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token", "--copy"]) == 0  # the token is still printed
    out = capsys.readouterr().out
    # P32 review M5: no pbcopy (Linux) is said calmly, not as a failure
    assert "tok-123" in out and cli_mod.COPY_UNAVAILABLE in out and cli_mod.COPY_FAILED not in out


def test_extension_token_copy_when_pbcopy_fails(tmp_path, monkeypatch, capsys):
    import subprocess

    import octopus.cli as cli_mod

    class FakeSecrets:
        def get(self, name):
            return "tok-123"

    def failing_pbcopy(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: FakeSecrets())
    monkeypatch.setattr(cli_mod.subprocess, "run", failing_pbcopy)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'data_dir = "{tmp_path}"\n[channels.extension]\nenabled = true\n', encoding="utf-8")
    assert main(["--config", str(cfg), "extension", "token", "--copy"]) == 0
    out = capsys.readouterr().out
    assert "tok-123" in out and cli_mod.COPY_FAILED in out


# ---- P25 (int2 M2): a token from the environment is not what the service sees ----

def test_extension_token_from_the_environment_says_the_service_cannot_see_it(tmp_path, monkeypatch, capsys):
    from octopus import host

    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path / "data"}"\n', encoding="utf-8")
    monkeypatch.setenv("OCTOPUS_EXTENSION_TOKEN", "tok-from-env")
    assert main(["--config", str(path), "extension", "token"]) == 0
    out = capsys.readouterr().out
    assert "tok-from-env" in out
    assert "注意：这个密码来自终端里的环境变量 OCTOPUS_EXTENSION_TOKEN。后台服务（launchd）读不到终端里的环境变量" in out
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    monkeypatch.setattr(host, "in_container", lambda: False)
    assert main(["--config", str(path), "extension", "token"]) == 0
    assert "后台服务（systemd）读不到" in capsys.readouterr().out
    monkeypatch.setattr(host, "in_container", lambda: True)  # Docker: the environment is how the service gets it
    assert main(["--config", str(path), "extension", "token"]) == 0
    assert "注意" not in capsys.readouterr().out
