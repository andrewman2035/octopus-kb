// Toolbar button: opens this popup, which saves nothing by itself. At the top a "Save this page" button ("Saved
// already — save again" when this tab sent any save in the last 10 minutes, a page save that failed aside; disabled,
// with the reason, on a page that cannot be saved: chrome://, a new tab …); pressed, it says the save was received.
// Below, the latest saves, listed until none is still being saved. No password yet: "Set up first", no button.
const POLL_MS = 2000;
const STATE_CLASS = { processing: "pending", saved: "done", incomplete: "done", failed: "failed" };
const LAST_ERROR_MS = 2 * 60 * 1000; // a right-click or shortcut save that failed lately is said here

const statusEl = document.getElementById("status");
const listEl = document.getElementById("list");
const setupEl = document.getElementById("setup");
const saveAreaEl = document.getElementById("save-area");
const saveEl = document.getElementById("save-page");
const reasonEl = document.getElementById("save-reason");
let tab = null; // the current tab
let pollTimer = null;

for (const id of ["options", "open-settings"]) {
  document.getElementById(id).addEventListener("click", () => chrome.runtime.openOptionsPage());
}
saveEl.addEventListener("click", saveThisPage);

// code "noToken" or "badToken" (no password yet, or a wrong one): also where the password comes from (the command)
// and a button to the settings, instead of the save button.
function say(text, bad = false, code = "") {
  statusEl.textContent = text;
  statusEl.className = bad ? "bad" : "";
  const setUp = code === "noToken" || code === "badToken";
  setupEl.hidden = !setUp;
  if (setUp) saveAreaEl.hidden = true;
}

async function main() {
  await loadUiLanguage();
  localizePage();
  wireCopyButtons();
  const s = await loadSettings();
  if (!s.token) {
    say(TEXT.noToken, true, "noToken"); // set up first: no button, nothing is sent
    await chrome.storage.local.set({ lastError: null }); // a right-click save's "set up first": said above
    return;
  }
  const { lastError } = await chrome.storage.local.get({ lastError: null });
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  say(TEXT.listOnly);
  if (lastError && Date.now() - lastError.at < LAST_ERROR_MS) {
    statusEl.textContent += msg("lastError", lastError.text);
    await chrome.storage.local.set({ lastError: null });
  }
  const web = Boolean(tab && isWeb(tab.url));
  let again = false;
  try {
    again = web && (await sentLately(tab));
  } catch (e) {
    say(errorText(e), true, e.code); // a wrong password: the setup hint, no button
    return;
  }
  saveEl.textContent = again ? TEXT.saveAgain : TEXT.savePage;
  saveEl.disabled = !web;
  reasonEl.textContent = web ? "" : TEXT.cantSave;
  reasonEl.hidden = web;
  saveAreaEl.hidden = false;
  await poll();
}

// The button: the service worker saves the page (it finishes even when the popup closes first).
async function saveThisPage() {
  const label = saveEl.textContent;
  saveEl.disabled = true;
  saveEl.textContent = TEXT.savingPage;
  let r = null;
  try {
    r = await chrome.runtime.sendMessage({ type: "savePage", tab: { id: tab.id, url: tab.url, title: tab.title } });
  } catch (e) {
    r = null;
  }
  saveEl.disabled = false;
  saveEl.textContent = r && r.ok ? TEXT.saveAgain : label;
  if (r && r.ok) say(TEXT.received);
  else say((r && r.error) || TEXT.failed, true, r && r.code);
  if (r && !r.ok && ["notRunning", "badToken", "noToken"].includes(r.code)) return; // the list would say it again
  await poll();
}

async function poll() {
  clearTimeout(pollTimer); // one round of polling at a time (the button starts one too)
  let data;
  try {
    data = await callOctopus("/recent");
  } catch (e) {
    say(errorText(e), true, e.code);
    return;
  }
  render(data.items || []);
  if ((data.items || []).some((i) => codeOf(i) === "processing")) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, POLL_MS);
  }
}

function line(cls, text) {
  const el = document.createElement("div");
  el.className = cls;
  el.textContent = text;
  return el;
}

function render(items) {
  listEl.replaceChildren();
  if (!items.length) {
    listEl.append(line("sub", TEXT.empty));
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    const row = document.createElement("div");
    row.className = "row";
    const code = codeOf(item);
    const state = line(`state ${STATE_CLASS[code] || ""}`, STATE_TEXT[code] || item.state);
    const title = line("title", item.entry_title || item.title || item.url || KIND_LABEL[item.kind]);
    title.title = title.textContent;
    row.append(state, title);
    li.append(row);
    const sub = code === "failed" ? item.reason : item.summary;
    if (sub) li.append(line("sub", sub));
    if (item.incomplete && item.reason) li.append(line("sub", item.reason));
    if (item.filled) li.append(line("sub", item.filled)); // the page filled an entry saved earlier (same id)
    const when = new Date(item.at * 1000).toLocaleTimeString(UI_LOCALE.replace("_", "-"), { hour: "2-digit", minute: "2-digit" });
    li.append(line("meta", [KIND_LABEL[item.kind], when, item.entry_id ? msg("idLine", item.entry_id) : ""]
      .filter(Boolean).join(" · ")));
    listEl.append(li);
  }
}

main();
