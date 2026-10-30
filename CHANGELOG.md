# Changelog

All notable changes are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Saving
- X posts: the post's videos (then its quoted post's; up to 4) are downloaded from video.twimg.com within
  `max_download_mb` and transcribed locally after the text; a video that cannot be read keeps the text (partial).
- X long posts: `[media] x_full_text_service = "fxtwitter"` (off by default) gets the full text from FxTwitter;
  without it, or when FxTwitter fails, the note is partial as before.
- X links saved from the Chrome extension are fetched as well (for the videos and the full text); the page is the
  fallback when the fetch fails or the post cannot be seen.
- WeChat Official Account "shared article" pages are read: the article inline in the page, else the original it links to.
- Links of platforms that need a login (Reddit, Xiaohongshu, Zhihu, Instagram, Facebook, LinkedIn, Threads, Medium,
  Weibo), and any page that reads as a login wall, say in the reply how to save them in full: open the page in Chrome
  on the computer, logged in, and save it with the extension (or install the extension first). README has a "Which
  platforms need the extension" table.
- Xiaohongshu share links on xhslink.cn (the iOS app's new short host, also typed without https://) get that line
  too, as does any partial link whose recorded redirect lands on one of those platforms.
- Xiaohongshu notes, saved from chat or the extension (or filled from it), are no longer titled with the whole note
  text: " - 小红书", hashtags and stickers go, and a text over 40 characters is cut to its first sentence (at most 40
  characters plus "…"); notes saved before keep their titles, and other sites' titles are unchanged.

### Entrances
- Discord: taps are answered before anything waits and the idle connection is kept for 3 minutes, so they no longer show "This interaction failed" after a short pause (up to 3 minutes); a late answer is logged with its timing.
- Discord: the commands are in Discord's `/` menu (DMs with the bot), so Octopus's own `/s` is there to pick instead of `/shrug` or `/spoiler`.
- Chrome extension: the settings page shows the command that makes its password (`uv run octopus extension token --copy`, with a Copy button); the popup says "Set up first" with the same hint and a button to the settings when no password is saved, and adds the hint to "Wrong password". The settings page also gives the Docker command; `extension token --copy` on Linux says calmly that copying needs a Mac.
- Chrome extension: clicking the toolbar icon only opens the popup; nothing is saved until you press its "Save this page"
  button ("Saved already — save again" when the tab saved something in the last 10 minutes; disabled on pages that
  can't be saved). New keyboard shortcut Alt+Shift+S (Option+Shift+S on a Mac) saves the current page without the
  popup, skipping a tab saved in the last 10 minutes (grey ✓); change it at `chrome://extensions/shortcuts`.
- Chrome extension: saving the page of a link saved earlier as incomplete (partial) fills that entry in, keeping
  its id, as a link nothing was read from already was; the list and the Telegram copy say so. Sending the same link in
  chat is still "already saved". A title set with /title or edited in Obsidian (entries saved from this version on)
  and text edited in Obsidian are kept (the page then goes
  under "## Page content (extension)"); files only the replaced text linked go to the trash. An incomplete X post
  saved from the extension is fetched again too (its videos downloaded and transcribed again).
- Links are also recognised by the addresses they redirected to, the browser tab's address and, for Xiaohongshu,
  Reddit, Threads and Weibo, the post's id (`url_aliases` in the note's frontmatter), so a phone's share link and the
  browser's address find the same entry. Reddit app share links (`/s/…`) are not resolved. A page's canonical address
  only fills an incomplete entry saved under exactly that address; it never makes a page "already saved".

- `/read` of a long entry no longer floods the chat: it sends 5 parts at a time (`read_batch_parts` in `config.toml`;
  ClawBot keeps its smaller send budget), and the last one has **Continue (6/38)** and **Send as a file** buttons
  (on WeChat, the typed `/read <id> K` hint). `/read <id> all` (`全部`) sends every part at once; `/read <id> file`
  (`文件`) sends the note's Markdown as `<title>-<short id>.md` on Telegram, Discord and Feishu (WeChat, or a file too big
  for the platform: it says so and sends parts); without an id they mean the current entry. The buttons stay after a
  tap, and a double tap sends once. Entries over 10 parts say so under the first batch.

### Models
- `/model`, the "Models" button and `octopus models` say what the arrow meant: `claude:haiku (falls back to local)`.

### Platforms and operations
- `octopus init` without `--language` writes the new config in the system language: Chinese when it is Chinese
  (macOS: the first preferred language; elsewhere `LANGUAGE` / `LC_ALL` / `LC_MESSAGES` / `LANG`; Cantonese counts
  as Chinese), otherwise English, including
  a container or shell with no locale. It says which language it picked and how to change it. `--language` still
  wins, an existing config is never changed, and a config without a `language` line is still Chinese.
- Every other new default config (`service install` or `library move` without one) is written the same way, and
  until a config exists (`--help`, argparse errors, commands run without one) Octopus speaks the system language.
- macOS service: the launchd job's `LANG` follows the config's language (`en_US.UTF-8` for English; Chinese keeps
  `zh_CN.UTF-8`). Run `octopus service restart` after changing `language`. The Linux unit keeps `C.UTF-8`.

## 0.1.0

First public version, released under the MIT License as `octopus-kb`
(<https://github.com/andrewman2035/octopus-kb>). The command, the import package and the data directory are
`octopus` / `~/Octopus`.

### Saving
- Links: web articles (trafilatura), WeChat Official Account posts, X posts, YouTube (captions, else local
  transcription), Bilibili, podcasts (Apple Podcasts, Xiaoyuzhou, direct audio), PDF links; tracking parameters
  removed and duplicates recognized.
- Images and screenshots with OCR; PDFs with an OCR fallback; audio, video and voice messages transcribed locally.
- Text with `/s`, `save:` or `save,`; pasted articles with their links kept as text.
- Each entry becomes a Markdown note with title, summary, key points, tags and attachments; summaries in the
  configured or the content's language.
- Collecting in pieces: screenshots sent together become one entry, `/add <id>`, `/new`, `done`, "save separately",
  and links that could not be read wait for screenshots or pasted text.
- A persisted job queue with retries; replies that could not be delivered are re-sent later.

### Asking and managing
- Answers from your own notes with hybrid search (FTS5 with jieba + sqlite-vec embeddings from a local `bge-m3`) and
  numbered citations; entries named by id, `@id`, title or "this one" are read in full.
- "Save this" and "Make it a question" to flip an answer or a note.
- Managing notes in chat (find, list, delete, rename, comment, summarize again) with a confirmation step; `/recent`,
  `/list`, `/read`, `/find`, `/delete`, `/resummarize`, `/model`, `/status`, `/help`.

### Entrances
- Terminal (`octopus chat`), Telegram, Feishu, WeChat ClawBot, Discord; each answers only its allow-listed owner.
- Email (save only) with provider-authenticated sender checks (DMARC, else aligned DKIM / SPF).
- Chrome extension (page, selection, image) and macOS Shortcuts (Safari share sheet, selected text in any app)
  through a token-protected endpoint on `127.0.0.1`.

### Models
- Claude through Claude Code on your own subscription, the Anthropic API, any OpenAI-compatible service, or local
  models through Ollama; a primary and a fallback per task; `/model local` at runtime.

### Platforms and operations
- macOS (Apple Silicon: mlx-whisper, Vision OCR, Keychain, launchd), Linux (faster-whisper, RapidOCR, environment /
  file secrets or Secret Service, systemd `--user`) and Docker.
- Obsidian: the library can live in a vault (`library move` / `adopt` / `sync`); edits made there are re-indexed.
- Daily database backups, a trash folder for deleted notes, one log line per message and reply.
- English and Chinese interface (`language`), with input understood in both.
