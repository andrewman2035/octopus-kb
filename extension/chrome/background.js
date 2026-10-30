// Service worker: the ways to save (the popup's "Save this page" button, the keyboard shortcut, and the "Save to
// Octopus" context menus).
importScripts("messages.js", "common.js");

const MENUS = [
  ["save-selection", ["selection"]],
  ["save-image", ["image"]],
  ["save-page", ["page"]],
];
const SEND_AS_IS = ["image/png", "image/jpeg", "image/gif", "image/webp"]; // others are turned into PNG first

async function setUpMenus() {
  await loadUiLanguage();
  chrome.contextMenus.removeAll(() => {
    for (const [id, contexts] of MENUS) chrome.contextMenus.create({ id, title: msg("menuSave"), contexts });
  });
}

chrome.runtime.onInstalled.addListener(setUpMenus);
chrome.storage.onChanged.addListener((changes) => {
  if (changes.uiLanguage) setUpMenus(); // the language picked in the settings
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  const work = loadUiLanguage().then(() =>
    info.menuItemId === "save-selection" ? saveSelection(info, tab)
    : info.menuItemId === "save-image" ? saveImage(info, tab)
    : savePage(tab));
  showOnBadge(work);
});

// The keyboard shortcut (manifest "commands", Alt+Shift+S by default): saves the current page without the popup,
// unless this tab sent a save in the last 10 minutes (then a grey ✓ and nothing sent). Result on the badge.
chrome.commands.onCommand.addListener((command, tab) => {
  if (command !== "save-page") return;
  showOnBadge(loadUiLanguage().then(() => saveByShortcut(tab)));
});

const savingTabs = new Set(); // tabs whose shortcut save is on its way: pressed again meanwhile, it counts as saved

async function saveByShortcut(tab) {
  if (!tab) [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true }); // a worker has no current window
  if (!tab) throw new OctopusError(TEXT.cantSave, "cantSave");
  if (savingTabs.has(tab.id)) return { already: tab.id };
  savingTabs.add(tab.id);
  try {
    if (isWeb(tab.url) && (await sentLately(tab))) return { already: tab.id };
    return await savePage(tab);
  } finally {
    savingTabs.delete(tab.id);
  }
}

// The popup asks for the page save (it may close before the request ends; this worker finishes it).
chrome.runtime.onMessage.addListener((request, sender, reply) => {
  if (!request || request.type !== "savePage") return false;
  loadUiLanguage().then(() => savePage(request.tab)).then(
    (r) => reply({ ok: true, id: r.id }),
    (e) => reply({ ok: false, error: errorText(e), code: (e && e.code) || "" }),
  );
  return true; // answered later
});

async function showOnBadge(work) {
  chrome.action.setBadgeText({ text: "…" });
  chrome.action.setBadgeBackgroundColor({ color: "#777777" });
  try {
    const r = await work;
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: r && r.already ? "#777777" : "#2e7d32" }); // grey: saved before
    if (r && r.already) {
      // not only the colour: the icon's tooltip on that tab says it, for as long as the badge
      // the tab may close meanwhile: its title is gone with it
      Promise.resolve(chrome.action.setTitle({ tabId: r.already, title: msg("savedAlreadyTitle") })).catch(() => {});
      setTimeout(() => Promise.resolve(chrome.action.setTitle({ tabId: r.already, title: msg("actionTitle") }))
        .catch(() => {}), 5000);
    }
    await chrome.storage.local.set({ lastError: null });
  } catch (e) {
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#c62828" });
    await chrome.storage.local.set({ lastError: { text: errorText(e), at: Date.now() } }); // the popup says it
  }
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 5000);
}

async function savePage(tab) {
  const s = await loadSettings();
  let page = { url: tab.url, title: tab.title, html: null };
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => ({ url: location.href, title: document.title, html: document.documentElement.outerHTML }),
    });
    if (r && r.result) page = r.result;
  } catch (e) {
    // a page Chrome does not let extensions read: saved by its link only
  }
  if (!isWeb(page.url)) throw new OctopusError(TEXT.cantSave, "cantSave");
  const body = { kind: "page", url: page.url, title: page.title, html: page.html, telegram: s.telegram };
  if (tooBig(body)) body.html = null; // over the cap: Octopus saves it by its link instead
  const saved = await callOctopus("/save", body, s);
  await rememberSave(tab.id, [tab.url, page.url], saved.id, "page");
  return saved;
}

async function saveSelection(info, tab) {
  const s = await loadSettings();
  let text = info.selectionText || "";
  try {
    // the selection as the page has it (info.selectionText loses the line breaks)
    const [r] = await chrome.scripting.executeScript({
      target: { tabId: tab.id, frameIds: [info.frameId || 0] },
      func: () => String(window.getSelection()),
    });
    if (r && r.result && r.result.trim()) text = r.result;
  } catch (e) {
    // keep info.selectionText
  }
  const body = { kind: "selection", url: info.pageUrl || tab.url, title: tab.title, text, telegram: s.telegram };
  const saved = await callOctopus("/save", body, s);
  await rememberSave(tab.id, [tab.url, info.pageUrl], saved.id, "selection");
  return saved;
}

async function saveImage(info, tab) {
  const s = await loadSettings();
  const body = { kind: "image", url: info.pageUrl || tab.url, title: tab.title, telegram: s.telegram };
  try {
    body.image = await readImage(info.srcUrl);
  } catch (e) {
    // the image's server does not let the extension read it: Octopus downloads it from its link
    if (!isWeb(info.srcUrl)) throw new OctopusError(TEXT.noImage, "noImage");
    body.image_url = info.srcUrl;
  }
  const saved = await callOctopus("/save", body, s);
  await rememberSave(tab.id, [tab.url, info.pageUrl], saved.id, "image");
  return saved;
}

async function readImage(src) {
  const res = await fetch(src, { credentials: "omit" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  let blob = await res.blob();
  if (!blob.type.startsWith("image/")) throw new Error("not an image");
  if (!SEND_AS_IS.includes(blob.type)) {
    // AVIF, SVG …: as PNG, which Octopus reads
    const bitmap = await createImageBitmap(blob);
    const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
    canvas.getContext("2d").drawImage(bitmap, 0, 0);
    blob = await canvas.convertToBlob({ type: "image/png" });
  }
  return { mime: blob.type, data: toBase64(new Uint8Array(await blob.arrayBuffer())) };
}

function toBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}
