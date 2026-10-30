"""P28: `octopus init` without --language writes the config in the system's language (Chinese on a Chinese system,
English otherwise), says which it picked and how to change it. An existing config is never touched, and a config
without a `language` line still means Chinese."""
import subprocess
import tomllib

import pytest

from octopus import host, i18n
from octopus.cli import main
from octopus.config import load_config, write_default_config
from octopus.i18n import detected_language as real_detected_language
from octopus.i18n import mac_ui_language, system_language


# ---- the pure decision ----

@pytest.mark.parametrize("value", ["zh", "zh_CN.UTF-8", "zh-Hans", "zh_TW", "zh-Hant-HK", "ZH_cn.utf8", "zh_SG@x"])
def test_a_chinese_locale_means_chinese(value):
    assert system_language({"LANG": value}, None) == "zh"


@pytest.mark.parametrize("value", ["en_US.UTF-8", "de_DE", "ja_JP.UTF-8", "zu_ZA", "fr"])
def test_any_other_locale_means_english(value):
    assert system_language({"LANG": value}, None) == "en"


@pytest.mark.parametrize("environ", [{}, {"LANG": ""}, {"LANG": "C"}, {"LANG": "POSIX"}, {"LC_ALL": "C.UTF-8"},
                                     {"LC_ALL": "C", "LC_MESSAGES": "POSIX", "LANG": "C.utf8"}])
def test_nothing_detectable_means_english(environ):  # Docker, a bare shell
    assert system_language(environ, None) == "en"


def test_lc_all_then_lc_messages_then_lang_the_first_non_empty_non_c_wins():
    assert system_language({"LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "zh_CN", "LANG": "zh_CN"}, None) == "en"
    assert system_language({"LC_ALL": "", "LC_MESSAGES": "zh_HK", "LANG": "en_US"}, None) == "zh"
    assert system_language({"LC_ALL": "C", "LANG": "zh_CN.UTF-8"}, None) == "zh"
    assert system_language({"LC_MESSAGES": "POSIX", "LANG": "en_GB"}, None) == "en"
    assert system_language({"LC_CTYPE": "zh_CN.UTF-8"}, None) == "en"  # not about messages


def test_on_macos_the_ui_language_wins_over_the_environment():
    # Terminal and iTerm set LANG from the region (en_US.UTF-8 for many Chinese-UI users), and LC_ALL=en_US.UTF-8
    # is a common fix for locale warnings: the language the user reads macOS in is the better signal
    assert system_language({"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}, "zh-Hans-CN") == "zh"
    assert system_language({"LANG": "zh_CN.UTF-8"}, "en-US") == "en"
    assert system_language({"LANG": "zh_CN.UTF-8"}, None) == "zh"  # unreadable: the environment decides
    assert system_language({"LANG": "zh_CN.UTF-8"}, "") == "zh"


# ---- the macOS preferred language, through an injected runner ----

def _runner(stdout="", returncode=0, error=None, calls=None):
    def run(args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")
    return run


def test_mac_ui_language_is_the_first_preferred_language():
    calls = []
    assert mac_ui_language(_runner('(\n    "zh-Hans-CN",\n    "en-US"\n)\n', calls=calls)) == "zh-Hans-CN"
    (args, kwargs), = calls
    assert args == ["defaults", "read", "-g", "AppleLanguages"]
    assert 0 < kwargs["timeout"] <= 5 and kwargs["capture_output"] and kwargs["text"]
    assert mac_ui_language(_runner("(\n    en,\n    \"zh-Hans\"\n)\n")) == "en"  # defaults leaves plain words unquoted


@pytest.mark.parametrize("run", [
    _runner(error=FileNotFoundError("defaults")),
    _runner(error=subprocess.TimeoutExpired("defaults", 2)),
    _runner(error=PermissionError()),
    _runner("The domain/default pair of (kCFPreferencesAnyApplication, AppleLanguages) does not exist\n", 1),
    _runner("(\n)\n"),
    _runner(""),
])
def test_mac_ui_language_is_none_when_it_cannot_be_read(run):
    assert mac_ui_language(run) is None


def test_the_detection_reads_the_ui_language_only_on_macos(monkeypatch):
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setattr(i18n, "mac_ui_language", lambda: "zh-Hans-CN")
    assert real_detected_language() == "zh"
    real_detected_language.cache_clear()
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    monkeypatch.setattr(i18n, "mac_ui_language", lambda: pytest.fail("not on Linux"))
    assert real_detected_language() == "en"
    real_detected_language.cache_clear()
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert real_detected_language() == "zh"


def test_the_detection_runs_once_per_process(monkeypatch):
    calls = []
    monkeypatch.setattr(i18n, "mac_ui_language", lambda: calls.append(1) or "en-US")
    assert real_detected_language() == "en" and real_detected_language() == "en"
    monkeypatch.setattr(i18n, "mac_ui_language", lambda: "zh-Hans")  # later answers do not change the decision
    assert real_detected_language() == "en" and calls == [1]


def test_tests_never_run_the_real_defaults():
    with pytest.raises(pytest.fail.Exception):
        i18n.mac_ui_language()


@pytest.mark.parametrize("junk", ["UTF-8", "utf8", ".UTF-8", "1234", "en US", "zh_CN UTF-8", "-", "_"])
def test_a_value_that_is_not_a_locale_is_skipped(junk):  # e.g. LC_ALL=UTF-8, by analogy with macOS's LC_CTYPE
    assert system_language({"LC_ALL": junk, "LANG": "zh_CN.UTF-8"}, None) == "zh"
    assert system_language({"LC_ALL": junk}, None) == "en"


def test_gnu_language_comes_first_its_first_entry():
    assert system_language({"LANGUAGE": "zh_CN:en", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}, None) == "zh"
    assert system_language({"LANGUAGE": "en_GB:zh", "LANG": "zh_CN.UTF-8"}, None) == "en"
    assert system_language({"LANGUAGE": "", "LANG": "zh_CN.UTF-8"}, None) == "zh"
    assert system_language({"LANGUAGE": "UTF-8", "LANG": "zh_CN.UTF-8"}, None) == "zh"
    assert system_language({"LANGUAGE": "en"}, "zh-Hans-CN") == "zh"  # macOS: the UI language still wins


@pytest.mark.parametrize("value", ["yue", "yue-Hant-HK", "yue_HK.UTF-8"])
def test_cantonese_counts_as_chinese(value):
    assert system_language({"LANG": value}, None) == "zh"
    assert system_language({}, value) == "zh"


@pytest.mark.parametrize("value", ["es-419", "sr_RS@latin", "en_US.ISO8859-1", "pt-BR", "fil_PH"])
def test_real_locales_still_count(value):
    assert system_language({"LANG": value, "LC_ALL": ""}, None) == "en"
    assert system_language({"LANG": "zh_CN"}, value) == "en"


# ---- init ----

@pytest.fixture
def system(monkeypatch):
    calls = []

    def pick(language):
        def detected():
            calls.append(language)
            return language
        monkeypatch.setattr(i18n, "detected_language", detected)
        return calls
    return pick


def test_init_on_an_english_system_writes_an_english_config_and_says_so(tmp_path, monkeypatch, capsys, system):
    monkeypatch.setenv("HOME", str(tmp_path))
    system("en")
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    assert tomllib.loads(path.read_text(encoding="utf-8"))["language"] == "en"
    assert load_config(path).language == "en"
    out = capsys.readouterr().out
    assert out == (f"Config file written: {path}\n"
                   + i18n.text_in("en", "cli.language_from_system", path=path) + "\n")
    assert 'language = "zh"' in out and "octopus init --language zh" in out and str(path) in out


def test_init_on_a_chinese_system_writes_a_chinese_config_and_says_so(tmp_path, monkeypatch, capsys, system):
    monkeypatch.setenv("HOME", str(tmp_path))
    system("zh")
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    assert tomllib.loads(path.read_text(encoding="utf-8"))["language"] == "zh"
    out = capsys.readouterr().out
    assert out == f"已写入配置文件：{path}\n" + i18n.text_in("zh", "cli.language_from_system", path=path) + "\n"
    assert 'language = "en"' in out and "octopus init --language en" in out


@pytest.mark.parametrize("given, detected", [("zh", "en"), ("en", "zh")])
def test_the_language_option_wins_over_the_system(tmp_path, monkeypatch, capsys, system, given, detected):
    monkeypatch.setenv("HOME", str(tmp_path))
    system(detected)
    path = tmp_path / "config.toml"
    assert main(["--config", str(path), "init", "--language", given]) == 0
    assert tomllib.loads(path.read_text(encoding="utf-8"))["language"] == given
    assert capsys.readouterr().out == i18n.text_in(given, "cli.config_written", path=path) + "\n"


@pytest.mark.parametrize("args", [[], ["--language", "en"]])
def test_init_never_changes_an_existing_config(tmp_path, monkeypatch, capsys, system, args):
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = system("en")
    path = tmp_path / "config.toml"
    text = f'data_dir = "{tmp_path / "data"}"\n'  # no language line: Chinese, as before P28
    path.write_text(text, encoding="utf-8")
    assert main(["--config", str(path), "init", *args]) == 0
    assert path.read_text(encoding="utf-8") == text and calls == []
    assert capsys.readouterr().out.startswith(f"配置文件已存在：{path}\n")
    assert load_config(path).language == "zh"


def test_init_in_a_container_defaults_to_english(tmp_path, monkeypatch, capsys, system):
    monkeypatch.setenv("OCTOPUS_DATA_DIR", str(tmp_path / "data"))
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(host, "system", lambda: host.LINUX)
    monkeypatch.setattr(i18n, "detected_language", real_detected_language)
    path = tmp_path / "data" / "config.toml"
    assert main(["--config", str(path), "init"]) == 0
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    assert raw["language"] == "en" and raw["data_dir"] == str(tmp_path / "data")
    assert capsys.readouterr().out.startswith(f"Config file written: {path}\n")


@pytest.mark.parametrize("language", i18n.LANGUAGES)
def test_every_template_names_its_language(language):
    assert tomllib.loads(i18n.CATALOGS[language]["config.template"])["language"] == language


def test_a_config_without_a_language_line_is_still_chinese(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")
    assert i18n.DEFAULT_LANGUAGE == "zh" and load_config(path).language == "zh"


# ---- round 2: every new config and everything said before one exists ----

def test_service_install_without_a_config_writes_one_in_the_system_language(tmp_path, monkeypatch, capsys, system):
    import octopus.service as svc

    system("en")
    monkeypatch.setattr(svc, "install", lambda *a, **k: (True, "installed"))
    monkeypatch.setattr("octopus.cli.ensure_dirs", lambda c: None)  # the template's data_dir is ~/Octopus
    path = tmp_path / "sub" / "config.toml"
    assert main(["--config", str(path), "service", "install"]) == 0
    assert tomllib.loads(path.read_text(encoding="utf-8"))["language"] == "en"
    out = capsys.readouterr().out
    assert out.startswith(f"Config file written: {path}\n"
                          + i18n.text_in("en", "cli.language_from_system", path=path) + "\ninstalled\n")


def test_service_install_keeps_an_existing_config(tmp_path, monkeypatch, capsys, system):
    import octopus.service as svc

    calls = system("en")
    monkeypatch.setattr(svc, "install", lambda *a, **k: (True, "installed"))
    path = tmp_path / "config.toml"
    text = f'data_dir = "{tmp_path / "data"}"\n'
    path.write_text(text, encoding="utf-8")
    assert main(["--config", str(path), "service", "install"]) == 0
    assert path.read_text(encoding="utf-8") == text and calls == []
    assert "installed" in capsys.readouterr().out and i18n.get_language() == "zh"


@pytest.mark.parametrize("detected", i18n.LANGUAGES)
def test_write_default_config_uses_the_system_language_unless_told(tmp_path, system, detected):
    system(detected)
    assert write_default_config(tmp_path / "a.toml")
    assert tomllib.loads((tmp_path / "a.toml").read_text(encoding="utf-8"))["language"] == detected
    other = "en" if detected == "zh" else "zh"
    assert write_default_config(tmp_path / "b.toml", other)
    assert tomllib.loads((tmp_path / "b.toml").read_text(encoding="utf-8"))["language"] == other


def test_a_missing_config_runs_in_the_system_language_an_existing_one_in_its_own(tmp_path, system):
    system("en")
    assert load_config(tmp_path / "missing.toml").language == "en" and i18n.get_language() == "en"
    (tmp_path / "empty.toml").write_text("", encoding="utf-8")
    assert load_config(tmp_path / "empty.toml").language == "zh" and i18n.get_language() == "zh"
    system("zh")
    (tmp_path / "en.toml").write_text('language = "en"\n', encoding="utf-8")
    assert load_config(tmp_path / "en.toml").language == "en"


@pytest.mark.parametrize("detected, expected", [("en", "write the default config file"), ("zh", "写入默认配置文件")])
def test_help_before_any_config_is_in_the_system_language(tmp_path, capsys, system, detected, expected):
    system(detected)
    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "missing.toml"), "--help"])
    assert expected in capsys.readouterr().out


def test_help_with_a_config_follows_it_not_the_system(tmp_path, capsys, system):
    calls = system("en")
    path = tmp_path / "config.toml"
    path.write_text(f'data_dir = "{tmp_path}"\n', encoding="utf-8")  # no language line: Chinese
    with pytest.raises(SystemExit):
        main(["--config", str(path), "--help"])
    assert "写入默认配置文件" in capsys.readouterr().out and calls == []


def test_an_argparse_error_before_any_config_is_in_the_system_language(tmp_path, capsys, system):
    system("en")
    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "missing.toml"), "init", "--language", "fr"])
    assert "the language of a new config" in i18n.t("cli.help.language")  # the parser was built in English
    assert "invalid choice" in capsys.readouterr().err


def test_a_command_without_a_config_speaks_the_system_language(tmp_path, monkeypatch, capsys, system):
    system("en")
    monkeypatch.setattr(host, "system", lambda: host.LINUX)  # the secrets file: no keyring
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--config", str(tmp_path / "missing.toml"), "secret", "list"]) == 0
    out = capsys.readouterr().out
    assert "telegram_token" in out and i18n.text_in("en", "cli.secret_not_set") in out
    assert i18n.text_in("zh", "cli.secret_not_set") not in out


# ---- round 3 ----

def test_a_config_path_that_cannot_be_checked_is_taken_as_there(tmp_path, monkeypatch, capsys, system):
    # e.g. a config in another user's 0700 folder: Path.exists() raises PermissionError there (not root)
    from pathlib import Path

    calls = system("en")
    locked = tmp_path / "locked" / "config.toml"
    real_exists = Path.exists

    def exists(self, *args, **kwargs):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    with pytest.raises(SystemExit):
        main(["--config", str(locked), "--help"])
    assert "写入默认配置文件" in capsys.readouterr().out and calls == []  # as before P28: a broken config


def test_write_new_config_says_nothing_when_another_process_wrote_it_first(tmp_path, monkeypatch, capsys, system):
    from octopus import cli

    system("en")
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "write_default_config", lambda p, language=None: False)  # lost the race
    assert cli._write_new_config(path) is False
    assert capsys.readouterr().out == ""


def test_library_move_without_a_config_writes_one_in_the_system_language(tmp_path, system):
    from octopus.config import ensure_dirs, make_test_config
    from octopus.move import move_library

    system("en")
    cfg = make_test_config(tmp_path / "data")
    ensure_dirs(cfg)
    config = tmp_path / "data" / "config.toml"
    target = tmp_path / "Vault" / "Octopus"
    move_library(cfg, config, target)
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    assert raw["language"] == "en" and raw["library_dir"] == str(target.resolve())
