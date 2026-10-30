import asyncio
import signal

from fakes import FakeEmbedder

import octopus.cli as cli_mod
from octopus.bootstrap import build_app, build_channels, run_service
from octopus.channels.base import Reply, SendResult
from octopus.config import ChannelConfig, make_test_config
from octopus.instance import InstanceLock


class Secrets:
    def __init__(self, **values):
        self.values = values

    def get(self, name):
        return self.values.get(name)


def test_build_channels_reports_missing_secrets(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"telegram": ChannelConfig(True, ["1"]), "feishu": ChannelConfig(True, ["ou"]),
                    "clawbot": ChannelConfig(False, [])}
    channels, problems = build_channels(cfg, Secrets(telegram_token="t"), notify=None)
    assert set(channels) == {"telegram"}
    assert problems == ["飞书已启用，但钥匙串里缺少 feishu_app_id / feishu_app_secret（运行 octopus secret set …）"]


def test_build_channels_clawbot_without_token_still_starts(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"clawbot": ChannelConfig(True, [])}
    channels, problems = build_channels(cfg, Secrets(), notify=None)
    assert set(channels) == {"clawbot"} and problems == []  # it will ask for a QR scan via notify


def test_build_channels_binds_each_channels_own_allow_list(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"telegram": ChannelConfig(True, ["111"]), "feishu": ChannelConfig(True, ["ou_me"])}
    channels, problems = build_channels(cfg, Secrets(telegram_token="t", feishu_app_id="a", feishu_app_secret="s"),
                                        notify=None)
    assert problems == []
    tg, fs = channels["telegram"], channels["feishu"]
    assert tg.allowed("111") and not tg.allowed("ou_me")
    assert fs.allowed("ou_me") and not fs.allowed("111")


async def test_build_channels_clawbot_notifies_owner_excluding_itself(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"clawbot": ChannelConfig(True, [])}
    calls = []

    async def notify(text, image):
        calls.append((text, image))

    channels, _ = build_channels(cfg, Secrets(), notify=notify)
    await channels["clawbot"].notify("qr", None)
    assert calls == [("qr", None)]


async def test_run_service_restarts_crashing_channel_and_stops(tmp_path):
    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {}, embedder=FakeEmbedder())
    starts = []

    class Crashy:
        name, acks_saves = "x", True

        async def start(self, on_message):
            starts.append(1)
            if len(starts) < 3:
                raise RuntimeError("boom")
            await asyncio.Event().wait()

        async def send(self, user_id, reply):
            raise AssertionError

    stop = asyncio.Event()
    task = asyncio.create_task(run_service(app, {"x": Crashy()}, stop, restart_delay=0.01, max_delay=0.02))
    for _ in range(100):
        if len(starts) >= 3:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 5)
    assert len(starts) == 3
    app.close()


class FakeChannel:
    acks_saves = True

    def __init__(self, name, on_start=None):
        self.name, self.on_start, self.sent = name, on_start, []

    async def start(self, on_message):
        if self.on_start:
            await self.on_start()
        await asyncio.Event().wait()

    async def send(self, user_id, reply):
        self.sent.append((user_id, reply.text))
        return SendResult(True, "m1")


async def test_run_refuses_when_another_instance_runs(tmp_path, monkeypatch, capsys):
    cfg = make_test_config(tmp_path)
    held = InstanceLock(cfg.data_dir / "octopus.lock")
    assert held.acquire()
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: (_ for _ in ()).throw(AssertionError("keychain touched")))
    try:
        assert await cli_mod._run(cfg) == 1  # launchd KeepAlive must see a failure
    finally:
        held.release()
    assert "已经在运行" in capsys.readouterr().out


async def test_run_without_usable_channels_fails_and_releases_lock(tmp_path, monkeypatch, capsys):
    cfg = make_test_config(tmp_path)
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    assert await cli_mod._run(cfg) == 1
    assert "没有可用的入口" in capsys.readouterr().out
    again = InstanceLock(cfg.data_dir / "octopus.lock")
    assert again.acquire()
    again.release()


async def test_run_serves_until_sigterm_and_clawbot_notices_skip_clawbot(tmp_path, monkeypatch):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"x": ChannelConfig(True, ["u1"]), "clawbot": ChannelConfig(True, ["owner"])}
    other, claw = FakeChannel("x"), FakeChannel("clawbot")
    delivered = []

    async def scenario():
        delivered.append(await notify("请扫码", None))
        signal.raise_signal(signal.SIGTERM)  # handled by the loop's handler, not the process default

    other.on_start = scenario
    captured = {}

    def fake_build_channels(cfg_, secrets, notify_):
        captured["notify"] = notify_
        return {"x": other, "clawbot": claw}, []

    async def notify(text, image):
        return await captured["notify"](text, image)

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", fake_build_channels)
    real_build_app = cli_mod.bootstrap.build_app
    monkeypatch.setattr(cli_mod.bootstrap, "build_app",
                        lambda c, ch, claude_token=None: real_build_app(c, ch, embedder=FakeEmbedder(),
                                                                         claude_token=claude_token))
    assert await asyncio.wait_for(cli_mod._run(cfg), 10) == 0
    assert delivered == [1]
    assert other.sent == [("u1", "请扫码")] and claw.sent == []
    again = InstanceLock(cfg.data_dir / "octopus.lock")
    assert again.acquire()
    again.release()


async def test_clawbot_login_refuses_while_service_holds_lock(tmp_path, monkeypatch, capsys):
    cfg = make_test_config(tmp_path)
    held = InstanceLock(cfg.data_dir / "octopus.lock")
    assert held.acquire()
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: (_ for _ in ()).throw(AssertionError("keychain touched")))
    try:
        assert await cli_mod._clawbot_login(cfg) == 1
    finally:
        held.release()
    out = capsys.readouterr().out
    assert "Octopus 正在运行，二维码会通过其他入口发给你；或者先停掉服务再运行 octopus clawbot-login。" in out


async def test_clawbot_login_reports_owner_and_releases_lock(tmp_path, monkeypatch, capsys):
    import octopus.channels.clawbot as clawbot_mod
    from octopus.channels.ilink import LoginResult

    cfg = make_test_config(tmp_path)
    seen = {}

    async def fake_login(secrets, state_path, qr_path):
        seen["paths"] = (state_path, qr_path)
        return LoginResult(bot_token="tok", base_url="https://x", bot_id="bot", user_id="wx_owner")

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(clawbot_mod, "login_interactive", fake_login)
    assert await cli_mod._clawbot_login(cfg) == 0
    out = capsys.readouterr().out
    assert "wx_owner" in out and "enabled = true" in out
    assert seen["paths"] == (cfg.data_dir / "clawbot-state.json", cfg.data_dir / "clawbot-qr.png")
    again = InstanceLock(cfg.data_dir / "octopus.lock")
    assert again.acquire()
    again.release()


def test_build_channels_gives_clawbot_its_own_allow_list(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"clawbot": ChannelConfig(True, ["wx_friend"]), "telegram": ChannelConfig(True, ["111"])}
    channels, _ = build_channels(cfg, Secrets(telegram_token="t"), notify=None)
    claw = channels["clawbot"]
    assert claw.allowed("wx_friend") and not claw.allowed("111")


# ---- final review: service lifecycle (I2, M8, M10, M11) ----

import logging
import time
from types import SimpleNamespace

import pytest

import octopus.bootstrap as bootstrap_mod


def fake_app(run_worker):
    async def handle(msg):
        pass

    return SimpleNamespace(run_worker=run_worker, handle=handle, wake=asyncio.Event())


class Blocking:
    acks_saves = True

    def __init__(self, name="x", log=None):
        self.name, self.log = name, log if log is not None else []

    async def start(self, on_message):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.log.append(f"{self.name} cancelled")
            raise

    async def send(self, user_id, reply):
        return SendResult(True, "m1")


async def test_worker_death_stops_the_service(caplog):
    async def dying_worker(stop):
        raise RuntimeError("db gone")

    stop = asyncio.Event()
    events = []
    ok = await asyncio.wait_for(run_service(fake_app(dying_worker), {"x": Blocking(log=events)}, stop), 5)
    assert ok is False and stop.is_set()
    assert events == ["x cancelled"]
    assert any(r.levelno == logging.ERROR and "job worker" in r.getMessage() for r in caplog.records)


async def test_shutdown_drains_the_worker_before_channels_go():
    events = []

    async def worker(stop):
        await stop.wait()
        await asyncio.sleep(0.05)  # a job finishing after stop...
        events.append("job replied")  # ...still replies through a live channel

    stop = asyncio.Event()
    task = asyncio.create_task(run_service(fake_app(worker), {"x": Blocking(log=events)}, stop))
    await asyncio.sleep(0.01)
    stop.set()
    assert await asyncio.wait_for(task, 5) is True
    assert events == ["job replied", "x cancelled"]


async def test_shutdown_is_bounded(caplog):
    events = []

    async def stuck_worker(stop):
        try:
            await asyncio.Event().wait()  # ignores stop
        except asyncio.CancelledError:
            events.append("worker cancelled")
            raise

    class Stubborn(Blocking):
        async def start(self, on_message):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()  # ignores the first cancel

    stop = asyncio.Event()
    task = asyncio.create_task(run_service(fake_app(stuck_worker), {"x": Stubborn()}, stop, shutdown_timeout=0.2))
    await asyncio.sleep(0.01)
    stop.set()
    started = time.monotonic()
    await asyncio.wait_for(task, 5)
    assert time.monotonic() - started < 1.0
    assert events == ["worker cancelled"]
    assert bootstrap_mod.SHUTDOWN_TIMEOUT == 15


async def test_channel_restart_backoff_doubles_up_to_the_cap(monkeypatch):
    delays = []
    stop = asyncio.Event()

    async def fake_wait(stop_, delay):
        delays.append(delay)
        if len(delays) == 6:
            stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(bootstrap_mod, "_wait_or_stop", fake_wait)

    class Crashy(Blocking):
        async def start(self, on_message):
            raise RuntimeError("boom")

    async def worker(stop_):
        await stop_.wait()

    await asyncio.wait_for(run_service(fake_app(worker), {"x": Crashy()}, stop, restart_delay=1, max_delay=8), 5)
    assert delays == [1, 2, 4, 8, 8, 8]


async def test_channel_that_returns_is_logged_and_restarted(monkeypatch, caplog):
    starts = []
    stop = asyncio.Event()

    async def fake_wait(stop_, delay):
        if len(starts) == 2:
            stop.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(bootstrap_mod, "_wait_or_stop", fake_wait)

    class Quitter(Blocking):
        async def start(self, on_message):
            starts.append(1)

    async def worker(stop_):
        await stop_.wait()

    await asyncio.wait_for(run_service(fake_app(worker), {"x": Quitter()}, stop, restart_delay=1), 5)
    assert len(starts) == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("channel x stopped" in w for w in warnings)  # M10


def _patch_run(monkeypatch, channels, problems=(), app_hook=None):
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", lambda c, s, n: (channels, list(problems)))
    real_build_app = cli_mod.bootstrap.build_app

    def build(c, ch, claude_token=None):
        app = real_build_app(c, ch, embedder=FakeEmbedder(), claude_token=claude_token)
        if app_hook:
            app_hook(app)
        return app

    monkeypatch.setattr(cli_mod.bootstrap, "build_app", build)


async def test_run_exits_1_when_the_worker_dies(tmp_path, monkeypatch):
    cfg = make_test_config(tmp_path)
    cfg.channels = {"x": ChannelConfig(True, ["u1"])}

    def break_worker(app):
        async def dying(stop):
            raise RuntimeError("db gone")

        app.run_worker = dying

    _patch_run(monkeypatch, {"x": Blocking()}, app_hook=break_worker)
    assert await asyncio.wait_for(cli_mod._run(cfg), 10) == 1
    again = InstanceLock(cfg.data_dir / "octopus.lock")
    assert again.acquire()
    again.release()


async def test_run_startup_problems_go_to_the_log(tmp_path, monkeypatch, caplog):
    cfg = make_test_config(tmp_path)
    _patch_run(monkeypatch, {}, problems=["Telegram 已启用，但钥匙串里缺少 telegram_token"])
    assert await cli_mod._run(cfg) == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("缺少 telegram_token" in e for e in errors) and any("没有可用的入口" in e for e in errors)  # M8


async def test_run_crash_goes_to_the_log(tmp_path, monkeypatch, caplog):
    cfg = make_test_config(tmp_path)
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", lambda c, s, n: ({"x": Blocking()}, []))
    monkeypatch.setattr(cli_mod.bootstrap, "build_app", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        await cli_mod._run(cfg)
    assert any(r.levelno == logging.ERROR and r.exc_info for r in caplog.records)  # M8


@pytest.mark.parametrize("error, expected", [
    ("ilink", "ClawBot 登录失败：二维码过期了。请重新运行 octopus clawbot-login。"),
    ("keychain", "ClawBot 登录凭证没能保存到钥匙串（PasswordSetError）。请解锁钥匙串后重新运行 octopus clawbot-login。"),
])
async def test_clawbot_login_failures_exit_1_with_a_message(tmp_path, monkeypatch, capsys, error, expected):
    import keyring.errors

    import octopus.channels.clawbot as clawbot_mod
    from octopus.channels.ilink import ILinkError

    cfg = make_test_config(tmp_path)

    async def failing_login(secrets, state_path, qr_path):
        if error == "ilink":
            raise ILinkError(None, "二维码过期了")
        raise keyring.errors.PasswordSetError("denied")

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(clawbot_mod, "login_interactive", failing_login)
    assert await cli_mod._clawbot_login(cfg) == 1  # M11
    assert expected in capsys.readouterr().out
    again = InstanceLock(cfg.data_dir / "octopus.lock")
    assert again.acquire()
    again.release()


async def test_run_wires_the_clawbot_channel_into_commands(tmp_path, monkeypatch):
    cfg = make_test_config(tmp_path)
    claw = FakeChannel("clawbot")
    seen = {}

    async def fake_run_service(app, channels, stop):
        seen["clawbot"] = app.commands.clawbot
        return True

    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", lambda c, s, n: ({"clawbot": claw}, []))
    real_build_app = cli_mod.bootstrap.build_app
    monkeypatch.setattr(cli_mod.bootstrap, "build_app",
                        lambda c, ch, claude_token=None: real_build_app(c, ch, embedder=FakeEmbedder(),
                                                                         claude_token=claude_token))
    monkeypatch.setattr(cli_mod.bootstrap, "run_service", fake_run_service)
    assert await cli_mod._run(cfg) == 0
    assert seen["clawbot"] is claw


async def test_chat_has_no_clawbot_reference(tmp_path):
    cfg = make_test_config(tmp_path)
    app = build_app(cfg, {}, embedder=FakeEmbedder())
    try:
        assert app.commands.clawbot is None
        assert await app.commands.run("clawbot", []) == "微信 ClawBot 没有启用。"
    finally:
        app.close()


def test_build_channels_starts_the_extension_endpoint_with_its_token(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {}
    cfg.extension.enabled, cfg.extension.port = True, 9123
    channels, problems = build_channels(cfg, Secrets(extension_token="tok"), notify=None)
    assert problems == [] and set(channels) == {"extension"}
    ext = channels["extension"]
    assert (ext.token, ext.port, ext.acks_saves) == ("tok", 9123, False)


def test_build_channels_extension_without_a_token_is_a_problem(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {}
    cfg.extension.enabled = True
    channels, problems = build_channels(cfg, Secrets(), notify=None)
    assert channels == {}
    assert problems == ["Chrome 插件已启用，但钥匙串里还没有它的密码（运行 octopus extension token）"]


def test_build_channels_extension_disabled_is_not_started(tmp_path):
    cfg = make_test_config(tmp_path)
    cfg.channels = {}
    channels, problems = build_channels(cfg, Secrets(extension_token="tok"), notify=None)
    assert channels == {} and problems == []


async def test_run_gives_the_extension_endpoint_its_intake(tmp_path, monkeypatch):
    from octopus.channels.extension import ExtensionChannel
    from octopus.extension import Intake

    cfg = make_test_config(tmp_path)
    ext = ExtensionChannel("tok", port=0)
    seen = {}

    async def start(on_message):
        seen["intake"] = ext.intake
        signal.raise_signal(signal.SIGTERM)
        await asyncio.Event().wait()

    ext.start = start
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", lambda c, s, n: ({"extension": ext}, []))
    real_build_app = cli_mod.bootstrap.build_app
    monkeypatch.setattr(cli_mod.bootstrap, "build_app",
                        lambda c, ch, claude_token=None: real_build_app(c, ch, embedder=FakeEmbedder(),
                                                                         claude_token=claude_token))
    assert await asyncio.wait_for(cli_mod._run(cfg), 10) == 0
    assert isinstance(seen["intake"], Intake)


async def test_run_gives_the_mailbox_its_intake(tmp_path, monkeypatch):
    """P23: the email channel saves through the App's jobs and replies with its own SMTP sender."""
    from octopus.channels.email import EmailChannel
    from octopus.config import EmailConfig
    from octopus.mail import MailIntake

    cfg = make_test_config(tmp_path)
    cfg.email = EmailConfig(enabled=True, imap_host="i", smtp_host="s", username="box@example.com",
                            owner_addresses=["me@example.com"])
    box = EmailChannel(cfg.email, "pw")
    seen = {}

    async def start(on_message):
        seen["intake"] = box.intake
        signal.raise_signal(signal.SIGTERM)
        await asyncio.Event().wait()

    box.start = start
    monkeypatch.setattr(cli_mod, "Secrets", lambda **kw: Secrets())
    monkeypatch.setattr(cli_mod.bootstrap, "build_channels", lambda c, s, n: ({"email": box}, []))
    real_build_app = cli_mod.bootstrap.build_app
    monkeypatch.setattr(cli_mod.bootstrap, "build_app",
                        lambda c, ch, claude_token=None: real_build_app(c, ch, embedder=FakeEmbedder(),
                                                                         claude_token=claude_token))
    assert await asyncio.wait_for(cli_mod._run(cfg), 10) == 0
    assert isinstance(seen["intake"], MailIntake) and seen["intake"].send_mail == box.send_mail
