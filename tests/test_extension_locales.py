"""P25 Part B: the Chrome extension speaks the browser's language (chrome.i18n): every message the scripts and pages
use exists in every locale, with the same placeholders, and English is the default."""
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHROME = Path(__file__).resolve().parent.parent / "extension" / "chrome"
LOCALES = sorted(p.name for p in (CHROME / "_locales").iterdir())


def messages(locale):
    return json.loads((CHROME / "_locales" / locale / "messages.json").read_text(encoding="utf-8"))


def used():
    names = set()
    for path in CHROME.glob("*.js"):
        if path.name == "messages.js":
            continue
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'msg\("([A-Za-z]+)"', text))
        for block in re.findall(r"messagesOf\(\{(.*?)\}\)", text, re.S):
            names |= set(re.findall(r':\s*"([A-Za-z]+)"', block))
    for path in CHROME.glob("*.html"):
        names |= set(re.findall(r'data-i18n="([A-Za-z]+)"', path.read_text(encoding="utf-8")))
    manifest = (CHROME / "manifest.json").read_text(encoding="utf-8")
    return names | set(re.findall(r"__MSG_([A-Za-z]+)__", manifest))


def test_english_is_the_default_and_chinese_is_there():
    assert json.loads((CHROME / "manifest.json").read_text())["default_locale"] == "en"
    assert {"en", "zh_CN"} <= set(LOCALES)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_used_message_exists_with_the_same_placeholders(locale):
    en, here = messages("en"), messages(locale)
    assert set(here) == set(en) and used() <= set(en)
    for name, m in en.items():
        assert set(m.get("placeholders", {})) == set(here[name].get("placeholders", {})), name
        for placeholder in m.get("placeholders", {}):
            assert f"${placeholder.upper()}$" in here[name]["message"], name


def test_the_english_messages_have_no_chinese():
    assert not [m["message"] for m in messages("en").values() if re.search("[一-鿿＀-￯]", m["message"])]


def test_the_popup_reads_the_code_and_falls_back_to_the_old_state():
    common = (CHROME / "common.js").read_text(encoding="utf-8")
    popup = (CHROME / "popup.js").read_text(encoding="utf-8")
    assert "if (item.code) return item.code;" in common and '处理中: "processing"' in common
    assert 'i.state === "' not in popup and "codeOf(" in popup


def test_zh_tw_is_the_traditional_conversion_and_messages_js_is_current():
    """Review p25b m6 / m5: zh_TW is built from zh_CN (OpenCC s2tw); messages.js holds all three as built."""
    spec = importlib.util.spec_from_file_location("build_locales", CHROME / "build_locales.py")
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    built = build.catalogs()
    assert built["zh_TW"] == messages("zh_TW") and built["zh_TW"]["kindPage"]["message"] == "網頁"
    assert (CHROME / "messages.js").read_text(encoding="utf-8") == build.messages_js(built)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("choice,ui,saving,missing", [("auto", "en-CA", "Saving…", "noSuchKey"),
                                                      ("auto", "zh-CN", "正在保存…", "noSuchKey"),
                                                      ("auto", "zh-TW", "正在保存…", "noSuchKey"),
                                                      ("zh", "en-US", "正在保存…", "noSuchKey"),
                                                      ("en", "zh-CN", "Saving…", "noSuchKey")])
def test_the_popup_words_follow_the_setting_and_never_go_blank(choice, ui, saving, missing):
    script = """
const fs = require("fs"); const vm = require("vm");
const ctx = { console, chrome: { i18n: { getUILanguage: () => %s, getMessage: () => "" },
  storage: { local: { get: async (d) => ({ ...d, uiLanguage: %s }) } } } };
vm.createContext(ctx);
for (const f of ["messages.js", "common.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
vm.runInContext('(async () => { await loadUiLanguage(); console.log(JSON.stringify([TEXT.savingPage, msg("noSuchKey"),'
  + ' msg("failedStatus", 503)])); })()', ctx);
""" % (json.dumps(ui), json.dumps(choice))
    out = subprocess.run(["node", "-e", script], cwd=CHROME, capture_output=True, text=True, check=True).stdout
    said, blank, status = json.loads(out)
    assert (said, blank) == (saving, missing) and "503" in status



UV_COMMAND = "uv run octopus extension token --copy"
DOCKER_COMMAND = "docker compose run --rm octopus extension token"


def test_both_pages_show_the_command_that_makes_the_password(capsys):
    """P32: the options page and the popup's setup hint show the command the password comes from (Copy button); the
    options page also the Docker one (review I2), and the hint is tied to the password field (M8)."""
    from octopus.cli import main

    for page in ("options.html", "popup.html"):
        html = (CHROME / page).read_text(encoding="utf-8")
        assert f'<code>{UV_COMMAND}</code> <button type="button" class="copy"' in html, page
    options = (CHROME / "options.html").read_text(encoding="utf-8")
    assert f'<code>{DOCKER_COMMAND}</code> <button type="button" class="copy"' in options
    # the password field is described by the hint sentence only, not the commands and Copy buttons (P32 re-review R1)
    assert 'aria-describedby="token-hint"' in options and '<p id="token-hint" class="hint" data-i18n="tokenHint">' in options
    with pytest.raises(SystemExit):  # the command is real (review M4)
        main(["extension", "token", "--help"])
    assert "--copy" in capsys.readouterr().out


def test_the_hint_names_the_cloned_folder_not_the_data_folder():
    """Review I1: "the Octopus folder" reads as the data folder ~/Octopus, where `uv run` finds no project."""
    for locale in ("en", "zh_CN", "zh_TW"):
        for name in ("tokenHint", "setupHint"):
            text = messages(locale)[name]["message"]
            assert "octopus-kb" in text and "Octopus folder" not in text and "Octopus 文件夾" not in text, (locale, name)
            assert "Octopus 文件夹" not in text, (locale, name)


def test_the_english_readme_gives_the_docker_password_command():
    """Review I2: a Docker user who runs `uv run …` on the host gets a second, wrong password."""
    readme = (CHROME.parent.parent / "README.md").read_text(encoding="utf-8")
    assert DOCKER_COMMAND in readme


POPUP_SCRIPT = """
const fs = require("fs"); const vm = require("vm");
const handlers = {};
const el = (id) => ({ hidden: false, disabled: false, textContent: "", className: "", dataset: {}, title: "",
  addEventListener(type, f) { if (id) handlers[id] = f; }, append() {}, replaceChildren() {} });
const els = {};
for (const id of ["status", "list", "setup", "options", "open-settings", "save-area", "save-page", "save-reason"]) {
  els[id] = el(id);
}
els.setup.hidden = els["save-area"].hidden = els["save-reason"].hidden = true;
const store = { token: %(token)s, uiLanguage: %(lang)s, lastError: { text: "Set up first", at: Date.now() },
  sentSaves: %(lately)s.map((s) => ({ ...s, at: Date.now() })) };
let sent = 0; let during = null;
const button = () => (els["save-area"].hidden ? null : [els["save-page"].textContent, els["save-page"].disabled]);
const ctx = { console, setTimeout, clearTimeout, Blob,
  document: { getElementById: (id) => els[id], querySelectorAll: () => [], createElement: () => el(),
    documentElement: {} },
  fetch: async () => {
    if (%(status)d < 0) throw new TypeError("connection refused");
    return { status: %(status)d, ok: %(status)d === 200, json: async () => ({ items: %(items)s }) };
  },
  chrome: { i18n: { getUILanguage: () => "en-US", getMessage: () => "" },
    runtime: { openOptionsPage() {}, sendMessage: async () => { sent += 1; during = button(); return %(reply)s; } },
    tabs: { query: async () => [{ id: 1, url: %(url)s, title: "A" }] },
    storage: { local: { get: async (d) => ({ ...d, ...store }), set: async (o) => Object.assign(store, o) } } } };
vm.createContext(ctx);
for (const f of ["messages.js", "common.js", "popup.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
setTimeout(async () => {
  const before = button();
  if (%(click)s) await handlers["save-page"]();
  console.log(JSON.stringify({ status: els.status.textContent, setup: !els.setup.hidden, sent, before, during,
    after: button(), reason: els["save-reason"].hidden ? null : els["save-reason"].textContent,
    lastError: store.lastError }));
}, 100);
"""
WEB, NEW_TAB = "https://example.org/a", "chrome://newtab/"
SAVE, AGAIN = ["Save this page", False], ["Saved already — save again", False]


def run_popup(token="right", url=WEB, status=200, reply=None, click=False, lately=(), items=(), lang="auto"):
    script = POPUP_SCRIPT % {"token": json.dumps(token), "url": json.dumps(url), "status": status,
                             "reply": json.dumps(reply), "click": json.dumps(click), "lang": json.dumps(lang),
                             "lately": json.dumps(list(lately)), "items": json.dumps(list(items))}
    out = subprocess.run(["node", "-e", script], cwd=CHROME, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def lately(kind="selection", tab=1, id="s1"):
    return {"tab": tab, "url": WEB, "id": id, "kind": kind}


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("kw,button,reason", [
    ({}, SAVE, None),
    ({"lately": [lately()]}, AGAIN, None),  # a selection saved from this tab 0 minutes ago
    ({"lately": [lately(tab=2)]}, SAVE, None),  # another tab
    ({"lately": [lately("page", id="p1")], "items": [{"id": "p1", "kind": "page", "code": "failed", "at": 0}]},
     SAVE, None),  # a page save that failed does not count
    ({"url": NEW_TAB}, ["Save this page", True], "This page can't be saved"),
    ({"lang": "zh", "lately": [lately()]}, ["已存过，再存一次", False], None),
    ({"lang": "zh"}, ["存这个页面", False], None)])
def test_opening_the_popup_saves_nothing_and_shows_the_button(kw, button, reason):
    """P34: the icon only opens the popup; the button reads "save again" inside 10 minutes and is disabled, with the
    reason, on a page that cannot be saved. A recent right-click error is still said, then dropped."""
    r = run_popup(**kw)
    assert r["sent"] == 0 and r["before"] == button and r["reason"] == reason and not r["setup"]
    assert r["status"].startswith("最近存的" if kw.get("lang") == "zh" else "Recently saved")
    assert "Set up first" in r["status"] and r["lastError"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("kw,said,during,after", [
    ({"reply": {"ok": True, "id": 7}}, "Received", ["Saving…", True], AGAIN),
    ({"reply": {"ok": True, "id": 7}, "lately": [lately()]}, "Received", ["Saving…", True], AGAIN),  # save again
    ({"reply": {"ok": True, "id": 7}, "lang": "zh"}, "已收到", ["正在保存…", True], ["已存过，再存一次", False]),
    ({"reply": {"ok": False, "error": "Octopus is not running", "code": "notRunning"}, "status": -1},
     "Octopus is not running", ["Saving…", True], SAVE),
    ({"reply": None}, "Not saved", ["Saving…", True], SAVE)])  # the service worker did not answer
def test_the_button_sends_one_save_and_says_it_was_received(kw, said, during, after):
    r = run_popup(click=True, **kw)
    assert r["sent"] == 1 and r["status"].startswith(said) and r["during"] == during and r["after"] == after
    assert not r["setup"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("kw,said", [
    ({"token": ""}, "Set up first"),
    ({"token": "", "url": NEW_TAB}, "Set up first"),
    ({"token": "wrong", "status": 401}, "Wrong password"),
    ({"token": "wrong", "status": 401, "url": NEW_TAB}, "Wrong password"),
    ({"token": "wrong", "status": 401, "lately": [lately()]}, "Wrong password"),  # review M2
    ({"token": "wrong", "click": True, "reply": {"ok": False, "error": "密码不对", "code": "badToken"}}, "密码不对")])
def test_the_popup_says_set_up_first_or_wrong_password_with_the_command(kw, said):
    """P32: no password yet -> "Set up first" and the hint, nothing sent and a stale right-click error dropped (review
    M6); a rejected one, on any path -> "Wrong password" and the same hint, decided by the error's code, not its words
    (review M2, M3; the reply in another language). P34: that state wins, so there is no save button."""
    r = run_popup(**kw)
    assert r["status"].startswith(said) and r["setup"] and r["after"] is None
    assert r["sent"] == (1 if kw.get("click") else 0)
    if not kw["token"]:
        assert r["before"] is None and r["lastError"] is None


BACKGROUND_SCRIPT = """
const fs = require("fs"); const vm = require("vm");
const listeners = {}; const badges = []; const colors = []; const posts = []; const titles = []; const queried = [];
const on = (name) => ({ addListener: (f) => { listeners[name] = f; } });
const TAB = { id: 1, url: %(url)s, title: "A" };
const store = { token: %(token)s, sentSaves: %(lately)s.map((s) => ({ ...s, at: Date.now() })) };
const timers = [];
const ctx = { console, Blob, setTimeout: (f) => timers.push(f),
  fetch: async (url, init) => {
    if (init.method === "POST") posts.push(JSON.parse(init.body));
    await new Promise((done) => setTimeout(done, 20)); // Octopus takes a moment
    return { status: %(status)d, ok: %(status)d === 200,
      json: async () => (init.method === "POST" ? { id: posts.length } : { items: [] }) };
  },
  chrome: { i18n: { getUILanguage: () => "en-US", getMessage: () => "" },
    runtime: { onInstalled: on("installed"), onMessage: on("message") },
    storage: { onChanged: on("changed"),
      local: { get: async (d) => ({ ...d, ...store }), set: async (o) => Object.assign(store, o) } },
    contextMenus: { onClicked: on("menu"), removeAll() {}, create() {} },
    commands: { onCommand: on("command") },
    action: { setBadgeText: (o) => badges.push(o.text), setBadgeBackgroundColor: (o) => colors.push(o.color),
      setTitle: (o) => titles.push([o.tabId, o.title]) },
    scripting: { executeScript: async () => {
      if (!/^https?:/.test(TAB.url)) throw new Error("Cannot access a chrome:// URL");
      return [{ result: { url: TAB.url, title: "A", html: "<html><body>hi</body></html>" } }];
    } },
    tabs: { query: async (q) => { queried.push(q); return [TAB]; } } } };
ctx.importScripts = (...files) => { for (const f of files) vm.runInContext(fs.readFileSync(f, "utf8"), ctx); };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync("background.js", "utf8"), ctx);
(async () => {
  for (const [command, withTab, wait] of %(presses)s) {
    listeners.command(command, withTab ? TAB : undefined);
    await new Promise((done) => setTimeout(done, wait === undefined ? 100 : wait));
  }
  await new Promise((done) => setTimeout(done, 200));
  const shown = [...badges];
  for (const f of timers) f(); // 5 s later: the badge and the tooltip go back
  console.log(JSON.stringify({ posts: posts.map((p) => [p.kind, p.url]), badges: shown, colors, queried, titles,
    lastError: store.lastError ? store.lastError.text : null }));
})();
"""
GREEN, GREY = "#2e7d32", "#777777"


def press_shortcut(presses, token="right", url=WEB, lately=(), status=200):
    script = BACKGROUND_SCRIPT % {"token": json.dumps(token), "url": json.dumps(url), "status": status,
                                  "lately": json.dumps(list(lately)), "presses": json.dumps(presses)}
    out = subprocess.run(["node", "-e", script], cwd=CHROME, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_shortcut_saves_the_page_then_not_again_within_10_minutes():
    """P34: the keyboard shortcut saves the current tab without the popup (✓ on the badge, like right-click saves);
    pressed again inside 10 minutes it sends nothing and the ✓ is grey."""
    r = press_shortcut([["save-page", True], ["save-page", True]])
    assert r["posts"] == [["page", WEB]]
    assert r["badges"] == ["…", "✓", "…", "✓"] and r["colors"][1::2] == [GREEN, GREY] and r["lastError"] is None
    assert r["titles"] == [[1, "Saved already in the last 10 minutes: not sent again"], [1, "Octopus — recent saves"]]  # review M3


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_double_press_sends_one_save():
    """Review M1: the second press comes while the first save is still on its way: it counts as saved already."""
    r = press_shortcut([["save-page", True, 0], ["save-page", True, 0]])
    assert r["posts"] == [["page", WEB]] and sorted(r["colors"][-2:]) == sorted([GREEN, GREY])


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("kw,posts,badge,color,last_error", [
    ({"lately": [lately()]}, [], "✓", GREY, None),  # a selection saved from this tab lately
    ({"lately": [lately(tab=2)]}, [["page", WEB]], "✓", GREEN, None),
    ({"url": NEW_TAB}, [], "!", "#c62828", "This page can't be saved"),
    ({"token": ""}, [], "!", "#c62828", "Set up first: paste the password in the settings"),
    # review M2: a password that is missing or wrong wins over "saved lately"
    ({"token": "", "lately": [lately()]}, [], "!", "#c62828", "Set up first: paste the password in the settings"),
    ({"token": "wrong", "status": 401, "lately": [lately()]}, [], "!", "#c62828", "Wrong password")])
def test_the_shortcut_follows_the_10_minute_rule_and_says_errors_on_the_badge(kw, posts, badge, color, last_error):
    r = press_shortcut([["save-page", True]], **kw)
    assert r["posts"] == posts and r["badges"][-1] == badge and r["colors"][-1] == color
    assert r["lastError"] == last_error


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_shortcut_finds_the_tab_itself_and_ignores_other_commands():
    r = press_shortcut([["something-else", True], ["save-page", False]])
    assert r["queried"] == [{"active": True, "lastFocusedWindow": True}]  # review M6: a worker has no current window
    assert r["posts"] == [["page", WEB]] and r["badges"] == ["…", "✓"]


OPTIONS_SCRIPT = """
const fs = require("fs"); const vm = require("vm");
const el = () => ({ hidden: false, textContent: "", className: "", value: "", checked: false, dataset: {},
  addEventListener() {} });
const els = {}; for (const id of ["token", "port", "telegram", "telegram-note", "status", "uiLanguage", "save",
  "shortcut-now"]) els[id] = el();
const ctx = { console, Blob, setTimeout,
  document: { getElementById: (id) => els[id], querySelectorAll: () => [], documentElement: {} },
  chrome: { i18n: { getUILanguage: () => "en-US", getMessage: () => "" },
    commands: { getAll: async () => [{ name: "save-page", shortcut: %(shortcut)s }, { name: "_execute_action" }] },
    storage: { local: { get: async (d) => ({ ...d, uiLanguage: %(lang)s }), set: async () => {} } } } };
vm.createContext(ctx);
for (const f of ["messages.js", "common.js", "options.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
setTimeout(() => console.log(JSON.stringify(els["shortcut-now"].textContent)), 100);
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("shortcut,lang,said", [
    ("⌥⇧S", "en", "Now: ⌥⇧S"),
    ("Alt+Shift+S", "zh", "现在是：Alt+Shift+S"),
    ("", "en", "Not set: set one at chrome://extensions/shortcuts"),
    ("", "zh", "还没有设上：到 chrome://extensions/shortcuts 里设一个")])
def test_the_options_page_shows_the_key_chrome_assigned(shortcut, lang, said):
    """Review M4: Chrome may leave the shortcut unset (another extension has it) or the owner may have changed it."""
    script = OPTIONS_SCRIPT % {"shortcut": json.dumps(shortcut), "lang": json.dumps(lang)}
    out = subprocess.run(["node", "-e", script], cwd=CHROME, capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == said


def test_the_manifest_has_the_shortcut_and_the_icon_only_opens_the_popup():
    """P34: Alt+Shift+S (Option+Shift+S on a Mac: "Alt" is Option there), described in the user's language; the
    options page and both READMEs say where to change it."""
    manifest = json.loads((CHROME / "manifest.json").read_text(encoding="utf-8"))
    command = manifest["commands"]["save-page"]
    assert command["suggested_key"] == {"default": "Alt+Shift+S", "mac": "Alt+Shift+S"}
    assert command["description"] == "__MSG_commandSavePage__" and manifest["action"]["default_popup"] == "popup.html"
    assert 'data-i18n="shortcutHint"' in (CHROME / "options.html").read_text(encoding="utf-8")
    for locale in LOCALES:
        assert "chrome://extensions/shortcuts" in messages(locale)["shortcutHint"]["message"], locale
    for readme in ("README.md", "README.zh-CN.md"):
        text = (CHROME.parent.parent / readme).read_text(encoding="utf-8")
        assert "chrome://extensions/shortcuts" in text and "Alt+Shift+S" in text, readme
    zh, en = messages("zh_CN"), messages("en")
    assert [zh[n]["message"] for n in ("savePageButton", "saveAgainButton", "savingButton")] == \
        ["存这个页面", "已存过，再存一次", "正在保存…"]
    assert [en[n]["message"] for n in ("savePageButton", "saveAgainButton")] == \
        ["Save this page", "Saved already — save again"]
    # review M5: the icon's tooltip no longer promises a save; M9: the unused "saving" message is gone
    assert "Save" not in en["actionTitle"]["message"] and "存到" not in zh["actionTitle"]["message"]
    assert "saving" not in en


COPY_SCRIPT = """
const fs = require("fs"); const vm = require("vm");
const timers = []; let selected = null; let handler = null;
const code = { textContent: "uv run octopus extension token --copy" };
const button = { textContent: "Copy", previousElementSibling: code, addEventListener: (_, f) => { handler = f; } };
const ctx = { console, Promise,
  setTimeout: (f) => timers.push(f),
  getSelection: () => ({ selectAllChildren: (node) => { selected = node.textContent; } }),
  navigator: { clipboard: { writeText: async (t) => { if (%(fails)s) throw new Error("denied"); } } },
  document: { querySelectorAll: () => [button] },
  chrome: { i18n: { getUILanguage: () => "en-US", getMessage: () => "" } } };
vm.createContext(ctx);
for (const f of ["messages.js", "common.js"]) vm.runInContext(fs.readFileSync(f, "utf8"), ctx);
vm.runInContext("wireCopyButtons()", ctx);
(async () => {
  await handler();
  const after = button.textContent;
  for (const f of timers) f();
  console.log(JSON.stringify([after, button.textContent, selected]));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
@pytest.mark.parametrize("fails,after,later,selected", [
    (False, "Copied", "Copy", None),
    (True, "Copy", "Copy", "uv run octopus extension token --copy")])
def test_the_copy_button_says_copied_then_resets_or_selects_the_command(fails, after, later, selected):
    """Review M1: "Copied" goes back to "Copy"; when the clipboard refuses, the command is selected for Cmd/Ctrl-C."""
    out = subprocess.run(["node", "-e", COPY_SCRIPT % {"fails": json.dumps(fails)}], cwd=CHROME, capture_output=True,
                         text=True, check=True).stdout
    assert json.loads(out) == [after, later, selected]
