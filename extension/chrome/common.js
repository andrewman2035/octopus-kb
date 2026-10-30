// Shared by the service worker (importScripts), the popup and the options page. No dependencies; messages.js is
// generated (build_locales.py).

const DEFAULT_PORT = 8765;
const MAX_BODY_BYTES = 15 * 1024 * 1024; // the server's cap: bigger is refused here, before sending
const RESAVE_AFTER_MS = 10 * 60 * 1000; // a tab that sent any save this lately: "save again" / the shortcut skips it

// The words: messages.js (built from _locales by build_locales.py) in the language chosen in the settings, "auto"
// following the browser. A message missing there falls back to Chinese, then to its name: never a blank label.
let UI_LOCALE = browserLocale();

function browserLocale() {
  const ui = (chrome.i18n.getUILanguage() || "").replace("-", "_");
  if (/^zh_(TW|HK|MO|Hant)/i.test(ui)) return "zh_TW";
  return /^zh/i.test(ui) ? "zh_CN" : "en";
}

function localeOf(choice) {
  return choice === "zh" ? "zh_CN" : choice === "en" ? "en" : browserLocale();
}

// The language picked in the settings (chrome.storage.local uiLanguage: "auto" | "zh" | "en"); call before speaking.
async function loadUiLanguage() {
  const { uiLanguage } = await chrome.storage.local.get({ uiLanguage: "auto" });
  UI_LOCALE = localeOf(uiLanguage);
  return uiLanguage;
}

function msg(name, ...subs) {
  const all = typeof MESSAGES === "undefined" ? {} : MESSAGES;
  const m = (all[UI_LOCALE] || {})[name] || (all.zh_CN || {})[name];
  if (!m) return chrome.i18n.getMessage(name, subs.map(String)) || name;
  let text = m.message;
  for (const [placeholder, def] of Object.entries(m.placeholders || {})) {
    const value = subs[Number(def.content.slice(1)) - 1];
    text = text.split(`$${placeholder.toUpperCase()}$`).join(value === undefined ? "" : String(value));
  }
  return text;
}

// name -> message, read when used (so a language loaded later counts)
const messagesOf = (names) => new Proxy({}, { get: (_, key) => (key in names ? msg(names[key]) : undefined) });
const TEXT = messagesOf({
  received: "received", notRunning: "notRunning", badToken: "badToken", tooBig: "tooBig",
  noToken: "noToken", cantSave: "cantSave", noImage: "noImage", failed: "failed", empty: "empty",
  connected: "connected", saved: "saved", noTelegram: "noTelegram", incomplete: "stateIncomplete", listOnly: "listOnly",
  savePage: "savePageButton", saveAgain: "saveAgainButton", savingPage: "savingButton",
});
const KIND_LABEL = messagesOf({ page: "kindPage", selection: "kindSelection", image: "kindImage" });

// A /recent item's state as a code: Octopus sends `code` since P25; an older one only its Chinese `state`.
const OLD_STATES = { 处理中: "processing", 已存: "saved", 失败: "failed" };
function codeOf(item) {
  if (item.code) return item.code;
  const code = OLD_STATES[item.state] || "";
  return code === "saved" && item.incomplete ? "incomplete" : code;
}
const STATE_TEXT = messagesOf({
  processing: "stateProcessing", saved: "stateSaved", incomplete: "stateIncomplete", failed: "stateFailed",
});

// data-i18n="name" on an element: its text is that message (the static words of popup.html and options.html)
function localizePage() {
  for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = msg(el.dataset.i18n);
  document.documentElement.lang = UI_LOCALE.replace("_", "-");
}

// The password's setup hint (popup and options page): each button.copy copies the command (<code>) just before it,
// says "Copied" for a moment, or selects the command for Cmd/Ctrl-C when the clipboard refuses.
const COPIED_MS = 1500;
function wireCopyButtons() {
  for (const button of document.querySelectorAll("button.copy")) {
    const code = button.previousElementSibling;
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        button.textContent = msg("copied");
        setTimeout(() => { button.textContent = msg("copyButton"); }, COPIED_MS);
      } catch (e) {
        getSelection().selectAllChildren(code);
      }
    });
  }
}

// code: the message's name ("badToken", "noToken" …), so the popup decides by it rather than by translated words
class OctopusError extends Error {
  constructor(message, code = "") {
    super(message);
    this.code = code;
  }
}

async function loadSettings() {
  return chrome.storage.local.get({ token: "", port: DEFAULT_PORT, telegram: false });
}

function isWeb(url) {
  return /^https?:\/\//i.test(url || "");
}

function tooBig(body) {
  return new Blob([JSON.stringify(body)]).size > MAX_BODY_BYTES;
}

// Saves sent lately from a tab and page (page, selection or image): the popup's button then says "save again", and
// the keyboard shortcut does not save the page again.
async function savesLately(tabId, url) {
  const { sentSaves } = await chrome.storage.local.get({ sentSaves: [] });
  return sentSaves.filter((s) => s.tab === tabId && s.url === url && Date.now() - s.at < RESAVE_AFTER_MS);
}

// Whether this tab sent a save in the last 10 minutes; a page save that Octopus says failed does not count.
async function sentLately(tab) {
  const lately = await savesLately(tab.id, tab.url);
  if (!lately.length) return false;
  let failed = new Set();
  try {
    const data = await callOctopus("/recent");
    failed = new Set((data.items || []).filter((i) => i.kind === "page" && codeOf(i) === "failed").map((i) => i.id));
  } catch (e) {
    if (e.code === "noToken" || e.code === "badToken") throw e; // set up first / wrong password: say that instead
    return true; // Octopus cannot say: count it as saved
  }
  return lately.some((s) => !(s.kind === "page" && failed.has(s.id)));
}

async function rememberSave(tabId, urls, id, kind) {
  const { sentSaves } = await chrome.storage.local.get({ sentSaves: [] });
  const now = Date.now();
  const kept = sentSaves.filter((s) => now - s.at < RESAVE_AFTER_MS);
  for (const url of new Set(urls)) if (url) kept.push({ tab: tabId, url, id, kind, at: now });
  await chrome.storage.local.set({ sentSaves: kept });
}

function errorText(e) {
  return e instanceof OctopusError ? e.message : TEXT.failed;
}

// One request to Octopus on this Mac. Throws OctopusError with the short text to show.
async function callOctopus(path, body, settings) {
  const s = settings || (await loadSettings());
  if (!s.token) throw new OctopusError(TEXT.noToken, "noToken");
  const init = { method: body === undefined ? "GET" : "POST", headers: { Authorization: `Bearer ${s.token}` } };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    if (new Blob([init.body]).size > MAX_BODY_BYTES) throw new OctopusError(TEXT.tooBig, "tooBig");
    init.headers["Content-Type"] = "application/json";
  }
  let res;
  try {
    res = await fetch(`http://127.0.0.1:${Number(s.port) || DEFAULT_PORT}${path}`, init);
  } catch (e) {
    throw new OctopusError(TEXT.notRunning, "notRunning"); // connection refused: the service is not running
  }
  if (res.status === 401) throw new OctopusError(TEXT.badToken, "badToken");
  if (res.status === 413) throw new OctopusError(TEXT.tooBig, "tooBig");
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    data = {};
  }
  if (!res.ok) throw new OctopusError(data.error || msg("failedStatus", res.status));
  return data;
}
