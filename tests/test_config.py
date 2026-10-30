from pathlib import Path

import pytest

from octopus.config import ConfigError, ensure_dirs, load_config, make_test_config, write_default_config


def test_defaults_when_file_missing(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.summarize.primary == "claude:haiku"
    assert cfg.summarize.fallback == "local"
    assert cfg.answer.primary == "claude:sonnet"
    assert cfg.answer.fallback == "local"
    assert cfg.local.model == "qwen3.5:35b-a3b"
    assert cfg.local.base_url == "http://127.0.0.1:11434"
    assert cfg.embed.model == "bge-m3"
    assert cfg.embed.dim == 1024
    assert cfg.max_concurrent_jobs == 2
    assert cfg.data_dir == Path("~/Octopus").expanduser()


def test_load_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        f'data_dir = "{tmp_path}"\n'
        "[models.answer]\n"
        'primary = "local"\n'
        'fallback = ""\n'
        "[channels.telegram]\n"
        "enabled = true\n"
        "allowed_users = [42]\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.data_dir == tmp_path
    assert cfg.answer.primary == "local"
    assert cfg.answer.fallback is None
    assert cfg.channels["telegram"].enabled is True
    assert cfg.channels["telegram"].allowed_users == ["42"]
    assert cfg.db_path == tmp_path / "octopus.db"
    assert cfg.library_dir == tmp_path / "library"
    assert cfg.llm_workdir == tmp_path / "llm-workdir"


def test_default_config_file_round_trips(tmp_path):
    p = tmp_path / "config.toml"
    assert write_default_config(p) is True
    assert write_default_config(p) is False  # never overwrites
    cfg = load_config(p)
    assert cfg.summarize.primary == "claude:haiku"
    assert set(cfg.channels) == {"telegram", "feishu", "clawbot"}
    assert all(not c.enabled for c in cfg.channels.values())


def test_ensure_dirs(tmp_path):
    cfg = make_test_config(tmp_path)
    ensure_dirs(cfg)
    for d in (cfg.library_dir, cfg.attachments_dir, cfg.trash_dir, cfg.backups_dir, cfg.logs_dir, cfg.llm_workdir):
        assert d.is_dir()


def test_media_config_defaults_and_override(tmp_path):
    cfg = make_test_config(tmp_path)
    assert cfg.media.whisper_model == "mlx-community/whisper-large-v3-turbo"
    assert cfg.media.youtube_cookies_from_browser == ""
    assert cfg.media.max_download_mb == 1024
    assert cfg.downloads_dir == tmp_path / "downloads"
    assert cfg.incoming_dir == tmp_path / "attachments" / "_incoming"
    p = tmp_path / "c.toml"
    p.write_text('[media]\nyoutube_cookies_from_browser = "chrome"\nmax_download_mb = 5\n', encoding="utf-8")
    cfg2 = load_config(p)
    assert (cfg2.media.youtube_cookies_from_browser, cfg2.media.max_download_mb) == ("chrome", 5)


def test_ensure_dirs_creates_media_dirs(tmp_path):
    cfg = make_test_config(tmp_path)
    ensure_dirs(cfg)
    assert cfg.downloads_dir.is_dir() and cfg.incoming_dir.is_dir()


def test_ensure_dirs_makes_data_private(tmp_path):
    # final review I6: library, logs, Keychain-adjacent state: only the owner may read them
    data = tmp_path / "Octopus"
    data.mkdir(mode=0o755)
    data.chmod(0o755)
    cfg = make_test_config(data)
    ensure_dirs(cfg)
    assert data.stat().st_mode & 0o777 == 0o700
    for d in (cfg.library_dir, cfg.attachments_dir, cfg.trash_dir, cfg.backups_dir, cfg.logs_dir,
              cfg.llm_workdir, cfg.downloads_dir, cfg.incoming_dir):
        assert d.stat().st_mode & 0o777 == 0o700, d


def test_bad_toml_is_friendly(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("data_dir = \n", encoding="utf-8")
    with pytest.raises(ConfigError, match="配置文件格式有误") as exc:
        load_config(p)
    assert str(p) in str(exc.value) and "第 1 行" in str(exc.value)


@pytest.mark.parametrize("text,where", [
    ('[embed]\nmodel = "bge-m3"\ndimm = 3\n', "embed.dimm"),
    ('[media]\nyoutube_cookie = "chrome"\n', "media.youtube_cookie"),
    ('[local_llm]\nmodle = "x"\n', "local_llm.modle"),
    ('[models.answer]\nprimay = "local"\n', "models.answer.primay"),
    ('[models.sumarize]\nprimary = "local"\n', "models.sumarize"),
    ('colour = "red"\n', "colour"),
    ('[channels.telegram]\nenable = true\n', "channels.telegram.enable"),
])
def test_unknown_keys_are_reported(tmp_path, text, where):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="配置文件里有不认识的设置：" + where.replace(".", r"\.")):
        load_config(p)


def test_default_config_file_still_loads(tmp_path):
    p = tmp_path / "c.toml"
    write_default_config(p)
    cfg = load_config(p)
    assert set(cfg.channels) == {"telegram", "feishu", "clawbot"}
    assert cfg.media.max_download_mb == 1024


# ---- P4 Task 5: private data dir from the first run ----

def test_write_default_config_creates_a_private_parent(tmp_path):
    path = tmp_path / "Octopus" / "config.toml"
    assert write_default_config(path)
    assert path.parent.stat().st_mode & 0o777 == 0o700


# ---- final review M3 / M8 ----

def test_relative_data_dir_is_resolved_against_the_config_file(tmp_path, monkeypatch):
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "config.toml").write_text('data_dir = "rel-data"\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)  # the cwd must not matter
    assert load_config(Path("conf/config.toml")).data_dir == (conf_dir / "rel-data").resolve()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    assert load_config(conf_dir / "config.toml").data_dir == (conf_dir / "rel-data").resolve()


def test_absolute_and_tilde_data_dirs_are_unchanged(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{tmp_path / "abs"}"\n', encoding="utf-8")
    assert load_config(p).data_dir == tmp_path / "abs"
    p.write_text('data_dir = "~/somewhere"\n', encoding="utf-8")
    assert load_config(p).data_dir == Path("~/somewhere").expanduser()


@pytest.mark.parametrize("text,needle", [
    ('[channels.telegram]\nenabled = "yes"\n', "channels.telegram.enabled"),
    ('[channels.telegram]\nenabled = 1\n', "channels.telegram.enabled"),
    ('[channels.telegrm]\nenabled = true\n', "channels.telegrm"),
    ('max_concurrent_jobs = 0\n', "max_concurrent_jobs"),
    ('max_concurrent_jobs = "lots"\n', "max_concurrent_jobs"),
    ('max_concurrent_jobs = true\n', "max_concurrent_jobs"),
    ('[channels.feishu]\nallowed_users = "ou_1"\n', "channels.feishu.allowed_users"),
    ('[channels.feishu]\nallowed_users = 5\n', "channels.feishu.allowed_users"),
])
def test_wrong_config_values_are_reported_in_chinese(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith("配置文件有误：") and needle in str(exc.value)


def test_valid_channel_values_still_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('max_concurrent_jobs = 1\n[channels.clawbot]\nenabled = true\nallowed_users = []\n'
                 '[channels.telegram]\nallowed_users = [1, "b"]\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.max_concurrent_jobs == 1 and cfg.channels["clawbot"].enabled is True
    assert cfg.channels["telegram"].allowed_users == ["1", "b"]


# ---- P17: the Chrome extension's local save endpoint ----

def test_extension_is_off_by_default_on_port_8765(tmp_path):
    cfg = make_test_config(tmp_path)
    assert (cfg.extension.enabled, cfg.extension.port) == (False, 8765)
    assert "extension" not in cfg.channels  # not a chat channel: no allow-list, no owner notices


def test_extension_section_is_in_the_init_template(tmp_path):
    p = tmp_path / "c.toml"
    write_default_config(p)
    assert "[channels.extension]" in p.read_text(encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.extension.enabled, cfg.extension.port) == (False, 8765)
    assert "extension" not in cfg.channels


def test_extension_section_loads(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[channels.extension]\nenabled = true\nport = 9000\n", encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.extension.enabled, cfg.extension.port) == (True, 9000)


@pytest.mark.parametrize("text,needle", [
    ("[channels.extension]\nenabled = 1\n", "channels.extension.enabled"),
    ('[channels.extension]\nport = "8765"\n', "channels.extension.port"),
    ("[channels.extension]\nport = true\n", "channels.extension.port"),
    ("[channels.extension]\nport = 80\n", "channels.extension.port"),
    ("[channels.extension]\nport = 70000\n", "channels.extension.port"),
])
def test_wrong_extension_values_are_reported_in_chinese(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith("配置文件有误：") and needle in str(exc.value)


def test_extension_takes_no_allow_list(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[channels.extension]\nallowed_users = []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"配置文件里有不认识的设置：channels\.extension\.allowed_users"):
        load_config(p)


# ---- P20: model providers by API key ----

def test_api_providers_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[models.summarize]\nprimary = "anthropic:haiku"\nfallback = "openai:deepseek"\n'
                 '[models.answer]\nprimary = "openai:lm-studio"\nfallback = "claude:sonnet"\n'
                 '[providers.deepseek]\nbase_url = "https://api.deepseek.com/v1"\nmodel = "deepseek-chat"\n'
                 '[providers.lm-studio]\nbase_url = "http://127.0.0.1:1234/v1"\nmodel = "qwen"\n', encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.summarize.primary, cfg.summarize.fallback) == ("anthropic:haiku", "openai:deepseek")
    assert cfg.providers["deepseek"].base_url == "https://api.deepseek.com/v1"
    assert cfg.providers["deepseek"].model == "deepseek-chat"
    assert set(cfg.providers) == {"deepseek", "lm-studio"}


def test_no_providers_by_default(tmp_path):
    assert load_config(tmp_path / "missing.toml").providers == {}


@pytest.mark.parametrize("text,needle", [
    ('[models.answer]\nprimary = "gpt4"\n', "models.answer.primary"),
    ('[models.answer]\nprimary = "claude"\n', "models.answer.primary"),
    ('[models.answer]\nprimary = "anthropic:"\n', "models.answer.primary"),
    ('[models.answer]\nprimary = "gemini:pro"\n', "models.answer.primary"),
    ('[models.answer]\nprimary = ""\n', "models.answer.primary"),
    ('[models.answer]\nprimary = 3\n', "models.answer.primary"),
    ('[models.summarize]\nfallback = "ollama"\n', "models.summarize.fallback"),
    ('[models.answer]\nprimary = "openai:deepseek"\n', "providers.deepseek"),
    ('[providers.deepseek]\nmodel = "deepseek-chat"\n', "providers.deepseek.base_url"),
    ('[providers.deepseek]\nbase_url = "https://x/v1"\n', "providers.deepseek.model"),
    ('[providers.deepseek]\nbase_url = "api.deepseek.com"\nmodel = "m"\n', "providers.deepseek.base_url"),
    ('[providers.deepseek]\nbase_url = "https://x/v1"\nmodel = ""\n', "providers.deepseek.model"),
    ('[providers.DeepSeek]\nbase_url = "https://x/v1"\nmodel = "m"\n', "providers.DeepSeek"),
    ('[providers.anthropic]\nbase_url = "https://x/v1"\nmodel = "m"\n', "providers.anthropic"),
    ('providers = 1\n', "providers"),
    ('[providers]\ndeepseek = "https://x/v1"\n', "providers.deepseek"),
    ('[models.answer]\nprimary = "anthropic: haiku"\n', "models.answer.primary"),
    ('[models.answer]\nprimary = "anthropic:haiku "\n', "models.answer.primary"),
    ('[models.answer]\nprimary = " local"\n', "models.answer.primary"),
    ('[models.summarize]\nfallback = "claude:\thaiku"\n', "models.summarize.fallback"),
    ('[providers.lm-studio]\nbase_url = "http://a/v1"\nmodel = "m"\n[providers.lm_studio]\nbase_url = "http://b/v1"\n'
     'model = "m"\n', "OCTOPUS_LM_STUDIO_API_KEY"),
])
def test_bad_model_specs_and_providers_are_reported_in_chinese(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith("配置文件") and needle in str(exc.value)


def test_unknown_provider_keys_are_reported(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.deepseek]\nbase_url = "https://x/v1"\nmodel = "m"\napi_key = "sk-1"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"配置文件里有不认识的设置：providers\.deepseek\.api_key"):
        load_config(p)


def test_default_template_mentions_api_providers_but_keeps_the_defaults(tmp_path):
    from octopus.config import DEFAULT_CONFIG_TOML

    assert "anthropic:" in DEFAULT_CONFIG_TOML and "[providers.deepseek]" in DEFAULT_CONFIG_TOML
    assert "anthropic_api_key" in DEFAULT_CONFIG_TOML and "ANTHROPIC_API_KEY" in DEFAULT_CONFIG_TOML
    p = tmp_path / "c.toml"
    write_default_config(p)
    cfg = load_config(p)
    assert (cfg.summarize.primary, cfg.summarize.fallback) == ("claude:haiku", "local")
    assert (cfg.answer.primary, cfg.answer.fallback) == ("claude:sonnet", "local")
    assert cfg.providers == {}


def test_base_url_whitespace_is_stripped_on_both_sides(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[providers.ds]\nbase_url = "  https://x/v1 "\nmodel = " m "\n', encoding="utf-8")
    assert load_config(p).providers["ds"].base_url == "https://x/v1"


def test_parse_spec_and_api_key_provider():
    from octopus.config import api_key_provider, parse_spec

    assert parse_spec("local") == ("local", "")
    assert parse_spec("claude:haiku") == ("claude", "haiku")
    assert parse_spec("openai:deep-seek") == ("openai", "deep-seek")
    assert [api_key_provider(s) for s in ("local", "claude:haiku", "anthropic:opus", "openai:ds", None)] == \
        [None, None, "anthropic", "ds", None]


# ---- P18: library_dir, Obsidian ----

def test_library_dir_defaults_to_data_dir(tmp_path):
    cfg = make_test_config(tmp_path)
    assert cfg.library_dir == tmp_path / "library"
    assert cfg.attachments_dir == tmp_path / "attachments"
    assert cfg.incoming_dir == tmp_path / "attachments" / "_incoming"


def test_library_dir_in_a_vault_keeps_attachments_there_but_staging_out(tmp_path):
    vault = tmp_path / "Vault" / "Octopus"
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{tmp_path / "data"}"\nlibrary_dir = "{vault}"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.library_dir == vault
    assert cfg.attachments_dir == vault / "attachments"
    # staging, downloads, trash, logs and the database never go into the vault
    for d in (cfg.incoming_dir, cfg.downloads_dir, cfg.trash_dir, cfg.logs_dir, cfg.db_path, cfg.llm_workdir,
              cfg.backups_dir):
        assert not d.is_relative_to(vault)
    assert cfg.incoming_dir == tmp_path / "data" / "attachments" / "_incoming"


def test_library_dir_tilde_and_relative(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('library_dir = "~/Vault/Octopus"\n', encoding="utf-8")
    assert load_config(p).library_dir == Path("~/Vault/Octopus").expanduser()
    p.write_text(f'data_dir = "{tmp_path / "data"}"\nlibrary_dir = "notes"\n', encoding="utf-8")
    assert load_config(p).library_dir == tmp_path.resolve() / "notes"


def test_library_dir_equal_to_the_default_changes_nothing(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{tmp_path}"\nlibrary_dir = "{tmp_path / "library"}"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.attachments_dir == tmp_path / "attachments"


@pytest.mark.parametrize("value", ["{data}", "{data}/..", "", "5"])
def test_library_dir_that_would_hold_the_data_dir_is_refused(tmp_path, value):
    data = tmp_path / "data"
    p = tmp_path / "c.toml"
    raw = value.format(data=data)
    line = f'library_dir = "{raw}"' if raw != "5" else "library_dir = 5"
    p.write_text(f'data_dir = "{data}"\n{line}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "library_dir" in str(exc.value)


def test_default_config_mentions_library_dir():
    from octopus.config import DEFAULT_CONFIG_TOML

    assert '# library_dir = "' in DEFAULT_CONFIG_TOML


@pytest.mark.parametrize("inside", ["trash", "backups", "downloads", "attachments", "logs", "vault", "library/sub"])
def test_library_dir_inside_the_data_dir_is_refused_except_its_library(tmp_path, inside):
    data = tmp_path / "data"
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{data}"\nlibrary_dir = "{data / inside}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert "library_dir" in str(exc.value)


def test_library_dir_through_a_symlink_to_the_default_is_the_default(tmp_path):
    real = tmp_path / "real"
    (real / "library").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    p = tmp_path / "c.toml"
    p.write_text(f'data_dir = "{real}"\nlibrary_dir = "{link / "library"}"\n', encoding="utf-8")
    cfg = load_config(p)
    assert not cfg.library_elsewhere and cfg.attachments_dir == real / "attachments"


def test_default_config_has_discord_off_and_an_email_example(tmp_path):
    """P23: new sections are optional and off; the commented email example is valid once uncommented."""
    from octopus.config import DEFAULT_CONFIG_TOML

    p = tmp_path / "config.toml"
    p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    cfg = load_config(p)
    assert "discord" not in cfg.channels and not cfg.email.enabled
    for section in ("discord", "email"):
        block = DEFAULT_CONFIG_TOML[DEFAULT_CONFIG_TOML.index(f"# [channels.{section}]"):].split("\n\n")[0]
        p.write_text("\n".join(line[2:] for line in block.splitlines()) + "\n", encoding="utf-8")
        cfg = load_config(p)
        assert cfg.email.enabled if section == "email" else cfg.channels["discord"].enabled


# ---- P22: Linux backends, the extension's bind address, OCTOPUS_DATA_DIR ----

def test_media_backends_default_to_auto_and_load(tmp_path):
    cfg = make_test_config(tmp_path)
    assert (cfg.media.asr_backend, cfg.media.ocr_backend, cfg.media.faster_whisper_model) == ("auto", "auto", "small")
    p = tmp_path / "c.toml"
    p.write_text('[media]\nasr_backend = "faster-whisper"\nocr_backend = "rapidocr"\nfaster_whisper_model = "medium"\n',
                 encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.media.asr_backend, cfg.media.ocr_backend, cfg.media.faster_whisper_model) == (
        "faster-whisper", "rapidocr", "medium")


@pytest.mark.parametrize("text,needle", [
    ('[media]\nasr_backend = "whisper"\n', "media.asr_backend"),
    ('[media]\nocr_backend = "tesseract"\n', "media.ocr_backend"),
    ('[media]\nasr_backend = 1\n', "media.asr_backend"),
    ('[media]\nfaster_whisper_model = ""\n', "media.faster_whisper_model"),
    ('[media]\nx_full_text_service = "nitter"\n', "media.x_full_text_service"),
    ('[channels.extension]\nbind = "everywhere"\n', "channels.extension.bind"),
    ('[channels.extension]\nbind = 0\n', "channels.extension.bind"),
])
def test_wrong_linux_settings_are_reported_in_chinese(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith("配置文件有误：") and needle in str(exc.value)


def test_extension_binds_loopback_by_default_and_takes_another_address(tmp_path):
    assert make_test_config(tmp_path).extension.bind == "127.0.0.1"
    p = tmp_path / "c.toml"
    p.write_text('[channels.extension]\nenabled = true\nbind = "0.0.0.0"\n', encoding="utf-8")
    assert load_config(p).extension.bind == "0.0.0.0"


def test_bind_is_only_an_extension_setting(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[channels.telegram]\nbind = "0.0.0.0"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match=r"channels\.telegram\.bind"):
        load_config(p)


def test_the_user_template_is_unchanged_without_octopus_data_dir(tmp_path, monkeypatch):
    from octopus.config import DEFAULT_CONFIG_TOML

    monkeypatch.delenv("OCTOPUS_DATA_DIR", raising=False)
    p = tmp_path / "c.toml"
    write_default_config(p)
    assert p.read_text(encoding="utf-8") == DEFAULT_CONFIG_TOML
    assert DEFAULT_CONFIG_TOML.startswith('# Octopus 配置文件\ndata_dir = "~/Octopus"\n')


def test_octopus_data_dir_is_written_into_a_new_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOPUS_DATA_DIR", "/data")
    p = tmp_path / "c.toml"
    write_default_config(p)
    assert 'data_dir = "/data"\n' in p.read_text(encoding="utf-8")
    assert load_config(p).data_dir == Path("/data")


def test_default_data_dir_follows_octopus_data_dir(monkeypatch):
    from octopus.config import default_data_dir

    monkeypatch.delenv("OCTOPUS_DATA_DIR", raising=False)
    assert default_data_dir() == Path("~/Octopus").expanduser()
    monkeypatch.setenv("OCTOPUS_DATA_DIR", "/data")
    assert default_data_dir() == Path("/data")
    monkeypatch.setenv("OCTOPUS_DATA_DIR", "  ")
    assert default_data_dir() == Path("~/Octopus").expanduser()


def test_secrets_backend_defaults_to_auto_and_loads(tmp_path):
    assert make_test_config(tmp_path).secrets_backend == "auto"
    p = tmp_path / "c.toml"
    for value in ("auto", "keyring", "file"):
        p.write_text(f'[secrets]\nbackend = "{value}"\n', encoding="utf-8")
        assert load_config(p).secrets_backend == value


@pytest.mark.parametrize("text,needle", [
    ('[secrets]\nbackend = "vault"\n', "secrets.backend"),
    ('[secrets]\nbackend = 1\n', "secrets.backend"),
    ('[secrets]\nfile = "x"\n', "secrets.file"),
])
def test_wrong_secrets_settings_are_reported_in_chinese(tmp_path, text, needle):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(p)
    assert str(exc.value).startswith("配置文件") and needle in str(exc.value)


# ---- P29: a long X post's full text from FxTwitter, opt-in ----

def test_x_full_text_service_is_off_by_default_and_opts_in(tmp_path):
    assert make_test_config(tmp_path).media.x_full_text_service == ""
    p = tmp_path / "c.toml"
    p.write_text('[media]\nx_full_text_service = "fxtwitter"\n', encoding="utf-8")
    assert load_config(p).media.x_full_text_service == "fxtwitter"
    p.write_text('[media]\nx_full_text_service = ""\n', encoding="utf-8")
    assert load_config(p).media.x_full_text_service == ""
