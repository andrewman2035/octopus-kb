// Options: the password (paste once), the port, "also send to Telegram", the extension's language. Kept in
// chrome.storage.local.
const tokenEl = document.getElementById("token");
const portEl = document.getElementById("port");
const telegramEl = document.getElementById("telegram");
const noteEl = document.getElementById("telegram-note");
const statusEl = document.getElementById("status");
const uiLanguageEl = document.getElementById("uiLanguage");
const shortcutEl = document.getElementById("shortcut-now");

function say(text, ok) {
  statusEl.textContent = text;
  statusEl.className = ok ? "ok" : "bad";
}

// Tries the connection and says whether Telegram can be used there.
async function check(settings) {
  noteEl.hidden = true;
  try {
    const data = await callOctopus("/recent", undefined, settings);
    say(TEXT.connected, true);
    if (settings.telegram && !data.telegram) {
      noteEl.textContent = TEXT.noTelegram;
      noteEl.hidden = false;
    }
  } catch (e) {
    say(errorText(e), false);
  }
}

// The key Chrome actually gave the shortcut: none when another extension already had it, or another one chosen at
// chrome://extensions/shortcuts.
async function showShortcut() {
  const command = (await chrome.commands.getAll()).find((c) => c.name === "save-page");
  shortcutEl.textContent = command && command.shortcut ? msg("shortcutNow", command.shortcut) : msg("shortcutUnset");
}

async function load() {
  uiLanguageEl.value = await loadUiLanguage();
  localizePage();
  await showShortcut();
  wireCopyButtons();
  const s = await loadSettings();
  tokenEl.value = s.token;
  portEl.value = s.port;
  telegramEl.checked = s.telegram;
  if (s.token) await check(s);
}

document.getElementById("save").addEventListener("click", async () => {
  const port = Number(portEl.value);
  const s = {
    token: tokenEl.value.trim(),
    port: Number.isInteger(port) && port >= 1024 && port <= 65535 ? port : DEFAULT_PORT,
    telegram: telegramEl.checked,
  };
  portEl.value = s.port;
  await chrome.storage.local.set({ ...s, uiLanguage: uiLanguageEl.value });
  await loadUiLanguage();
  localizePage();
  await showShortcut();
  say(TEXT.saved, true);
  if (s.token) await check(s);
});

load();
