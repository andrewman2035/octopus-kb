# Octopus

English · [简体中文](README.zh-CN.md)

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)
![macOS (Apple Silicon) · Linux · Docker](https://img.shields.io/badge/runs%20on-macOS%20%28Apple%20Silicon%29%20%C2%B7%20Linux%20%C2%B7%20Docker-lightgrey.svg)

**Your own AI knowledge base. Send it links, screenshots, files and voice notes from the chat app you already use;
it reads, transcribes and summarizes them into Markdown notes on your own disk, and answers your questions from
them with sources.**

```text
You:      https://www.youtube.com/watch?v=…            (pasted into Telegram)
Octopus:  Got it, working on it…
Octopus:  ✅ Saved: How index funds really work
          …a short summary…
          #investing #index-funds
          id: 20261013-7f3a2c

  … a week later …

You:      what was that video about long-term investing?
Octopus:  It argued that … [1]

          Sources:
          [1] How index funds really work (id: 20261013-7f3a2c) https://www.youtube.com/watch?v=…
```

## Why Octopus

- **Save from wherever you are.** Telegram, Discord, Feishu, WeChat (ClawBot), email, a Chrome extension, macOS
  Shortcuts or the terminal. No new app to open: share a link to the bot and you are done.
- **It reads what many tools only link to.** WeChat Official Account articles are saved in full; YouTube and
  Bilibili videos, X videos, podcasts and voice notes are transcribed; screenshots are OCR'd. Pages behind a login
  (Xiaohongshu, Zhihu, Reddit, Instagram …) are saved from your own logged-in Chrome with one click, and Octopus tells
  you when that is needed.
- **Your notes, your disk.** Every entry is a plain `.md` file with a summary, key points and tags. Put the folder in
  your Obsidian vault; nothing is locked into a database or a cloud account.
- **Ask your notes, not the internet.** Questions are answered from what you saved, with numbered citations; when
  nothing relevant is saved, it says so.
- **Your choice of model.** Use your Claude subscription (through Claude Code), an API key, any OpenAI-compatible
  service, or run fully local with Ollama.
- **Free and open source** (MIT). Runs on macOS (Apple Silicon), Linux or Docker; Windows through WSL2.

| | Octopus | Typical cloud read-later / AI note apps |
|---|---|---|
| Where notes live | Markdown files on your computer | the vendor's servers |
| How you save | the chat apps you already use, browser, email | their app or extension |
| Video, podcasts, voice | transcribed and summarized | often a link only |
| Chinese platforms (WeChat articles, Bilibili, Xiaoyuzhou …) | built in | rarely |
| Model | your choice, local possible | theirs |
| Price | free; you pay only for the model you use (nothing if it runs locally) | subscription |

## Features

- **Save almost anything**: web articles (including WeChat Official Account posts), YouTube and Bilibili videos,
  podcasts (Apple Podcasts, Xiaoyuzhou, direct audio), X posts, PDFs, images and screenshots (OCR), audio and video
  files, voice messages, plain text.
- **Notes you own**: one Markdown file per entry with YAML front matter; attachments next to it. The database is
  only a cache and can be rebuilt from the files.
- **Answers from your notes**: hybrid search (full-text with Chinese segmentation plus vector search), numbered
  citations, short follow-up memory. With nothing relevant saved, it says so and answers from general knowledge.
- **Manage in chat**: "what did I save last week?", "delete the one above", "rename this one to …", "add a comment
  to this one: …", "summarize that one again". Octopus always asks before changing anything.
- **Collect in pieces**: screenshots sent one after another become one entry; `/add <id>` and `/new` add images,
  files and text to an entry; a link that cannot be read waits for screenshots or pasted text.
- **Many entrances**: Telegram, Feishu, WeChat ClawBot, Discord, the terminal, a Chrome extension (the page you are
  looking at, a selection, an image), macOS Shortcuts (Safari share sheet, any app's selected text), email.
- **Your choice of model**: your Claude subscription through Claude Code, an Anthropic API key, any
  OpenAI-compatible service, or fully local models through Ollama, each with a fallback.
- **Obsidian**: keep the notes inside your vault; edits you make there are picked up.
- **English or Chinese**: the interface speaks either; input is understood in both.
- **Runs as a background service** on macOS (launchd) and Linux (systemd), or in Docker.

## Requirements

All platforms:

- Python 3.12, managed by [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com/) with `ollama pull bge-m3` (embeddings for search; required). For a local model:
  `ollama pull qwen3.5:35b-a3b` or any model you set in `[local_llm]`
- ffmpeg
- A model: a logged-in Claude Code (`claude` command) for the default `claude:…` models, or an API key, or local
  models only (see "Models")

| | macOS (native) | Linux (native) | Docker |
|---|---|---|---|
| Hardware | Apple Silicon | 64-bit Linux | a Linux Docker host |
| Speech to text | mlx-whisper | faster-whisper (CPU) | faster-whisper (CPU) |
| OCR | macOS Vision | RapidOCR | RapidOCR |
| Secrets | Keychain | environment or `secrets.toml` (optionally Secret Service) | environment or `data/secrets.toml` |
| Background service | launchd | systemd `--user` | `restart: unless-stopped` |
| Claude subscription models | yes | yes, with Claude Code installed | no (use an API key or local) |
| Chrome extension, Shortcuts | yes | extension only | extension only (port published on 127.0.0.1) |

## Quick start

```bash
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
uv sync
uv run octopus init     # writes ~/Octopus/config.toml, in English unless the system is Chinese
uv run octopus chat     # try it in the terminal
```

In `chat`, send `hi` for the guide. Paste a link or send `/s one line`; Octopus says "Got it, working on it…" and then
sends the saved result. Ask a question and it answers from what you saved. `/quit` exits.

To reach it from your phone, enable a channel (see "Channels") and install the background service.

### Language

`octopus init` writes a new config with `language = "en"`, or `"zh"` when the system language is Chinese (on macOS
the first preferred language in System Settings, elsewhere `LANGUAGE` / `LC_ALL` / `LC_MESSAGES` / `LANG`; a container with no
locale gets English), and says which it picked. `--language en` or `--language zh` chooses instead. To switch later,
edit `language` in `config.toml` and restart the service; `init` never changes an existing config. Every other way
a new config gets written (`service install` or `library move` with none yet) picks the language the same way, and
until a config exists (`--help`, errors, commands run without one) Octopus speaks that language too. A config without
a `language` line is Chinese, as before.

`language = "en"` or `"zh"` at the top of `config.toml` sets what Octopus says: replies, buttons, the
Telegram menu, notices, errors, the CLI and `--help`, and new notes' headings (`## Summary`, `## Key points`, …).
Commands and messages are understood in both languages whatever it says. Existing notes keep their headings; both
sets are read.

Summaries follow `language` by default; `summary_language = "en"` or `"zh"` forces one, `"auto"` writes each in its
content's language. Answers follow the language of your question (with an English setup, a question is answered in
Chinese only when it is mostly Chinese).

## Everyday use

**Rule of thumb: plain text is a chat; to save text, start with `/s` (or `save:`).** Links, images and files are
saved automatically.

| You send | Octopus |
|---|---|
| a link (web page, WeChat article, YouTube, Bilibili, X, podcast, PDF, …) | reads or transcribes it and saves a note |
| an X post | its text, then its videos transcribed (up to 4); a long post's full text needs `x_full_text_service` (FAQ) |
| a screenshot or image | reads the text in it (OCR) and saves it |
| a file, audio or video | extracts the text or transcribes it |
| `/s text`, `save: text`, `save, text` | saves the text as a note ("save the world?" is a question) |
| a voice message | transcribed and handled as if typed; say "save, …" to save it |
| anything else | a chat: answered from your notes, with sources |

### Which platforms need the extension

Some platforms show nothing without a login: fetched by link, Octopus gets a login page, a 403 or a few lines. Open
those in Chrome on your computer (log in first if needed) and save them with the
[Chrome extension](#chrome-extension-save-only). When such a link sent in chat is not read in full, the reply adds a
line saying so; saving the same page with the extension later fills that entry in (its id stays the same; see
[Chrome extension](#chrome-extension-save-only) for when).

| Platform | How to save |
|---|---|
| WeChat articles, YouTube, Bilibili, podcasts (Xiaoyuzhou, Apple Podcasts, audio links), X, ordinary web pages, PDF | send the link |
| Reddit, Xiaohongshu (RED), Zhihu (and its columns), Instagram, Facebook, LinkedIn posts (public articles can usually be sent as links), Threads, Medium (member-only), Weibo | log in and open the page in Chrome on your computer, then click **Save this page** in the extension |
| Douyin, TikTok, Xiaohongshu videos | the video can't be downloaded; the extension usually gets only the page's text (title, description): send screenshots or a screen recording for its content |
| WeChat Channels | the extension can't read it either: send screenshots, a screen recording or the pasted text |

Answers have a **Save this** button (or reply `save`), which keeps your question and the answer as one note. Saved
notes have **Make it a question** (or reply `ask`). Buttons work for 7 days; typed `save` / `ask` apply to the last
reply within 10 minutes.

### Managing notes in chat

| You say | Octopus |
|---|---|
| what did I save today? / what have I saved about AI? | lists matching notes (id and title) |
| delete the one above / delete 20261013-73b48c | moves it to the trash folder |
| rename the one above to … | changes the title |
| add a comment to this one: … | adds it under "My comments" |
| summarize that one again | writes a new summary |

Octopus asks first; tap **Confirm** or reply `yes` / `no` (on WeChat ClawBot, reply in text). A typed `yes` only
counts while that question is still the last reply.

### Adding to an entry

- Images sent one after another become one entry: just send an article's screenshots.
- A link that cannot be read is kept as a placeholder; screenshots or pasted text sent in the next 30 minutes are
  added to it (later, quote-reply that message).
- `/add <id>` then send images, files or one text; `/add <id> text` adds the text right away; `/new` (or `new`) starts
  a fresh entry; `done` (or `/done`) stops.
- Added to the wrong one: tap **Not this one, save separately** (or reply `separate`).
- Ids: type the full id, or just the 6 characters after the `-`, or their first or last 4 (e.g. `/delete 6e43`).
  `@id question` asks about that entry only.

### Commands

| Command | What it does |
|---|---|
| `/s <text>` (`/save`) | save the text as a note |
| `/recent [n]` | the latest entries (10 by default, at most 50) |
| `/list [date]` | everything saved that day (today by default), e.g. `/list 2026-10-14` |
| `/read <id> [K]` | the whole entry in chat, from part K; a long one comes 5 parts at a time (`read_batch_parts` in `config.toml`) with **Continue (6/38)** and **Send as a file** buttons under the last (on WeChat: reply `/read <id> K`) |
| `/read <id> file`, `/read <id> all` | the whole entry as a Markdown file `<title>-<short id>.md` (Telegram, Discord, Feishu; on WeChat, or when it is too big, it comes in parts instead); every part at once; without an id (`/read file`), the current entry |
| `/find <words>` | entries containing every word (no AI) |
| `/delete <id> …` (`/del`) | delete one or more; without an id, a list with delete buttons |
| `/resummarize <id>` | write the summary again |
| `/add <id>`, `/new`, `/done` | add to an entry, start a new one, stop adding |
| `/model`, `/model local`, `/model default` | show the models, switch everything to the local model, switch back |
| `/status` | status |
| `/clawbot` | a new WeChat ClawBot login QR code |
| `/help` | the guide |

## Models

Summaries and answers each have a `primary` model and an optional `fallback`, set in `[models.summarize]` and
`[models.answer]` of `config.toml`. Mixing is fine (for example an API primary with a local fallback). Search
embeddings always use the local Ollama `bge-m3`.

**1. Claude subscription (default).** Uses a logged-in Claude Code CLI on your own account; no key.

```toml
[models.summarize]
primary = "claude:haiku"
fallback = "local"

[models.answer]
primary = "claude:sonnet"
fallback = "local"
```

For the background service, give it a long-lived token: run `claude setup-token` and store what it prints with
`uv run octopus secret set claude_oauth_token`. This is meant for your own personal use of your own subscription;
Anthropic's terms apply. If you set Octopus up for someone else, use an API key.

**2. API key.** `anthropic:haiku`, `anthropic:sonnet`, `anthropic:opus` or a full model id
(`anthropic:claude-sonnet-5-5`). Any OpenAI-compatible service (DeepSeek, Qwen via DashScope, OpenRouter, LM Studio,
…) is declared once and used as `openai:<name>` (lower-case letters, digits, `-` and `_`):

```toml
[providers.deepseek]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"

[models.summarize]
primary = "openai:deepseek"
fallback = "local"
```

Keys never go into the config file:

- macOS: `uv run octopus secret set anthropic_api_key` (or `<name>_api_key`, e.g. `deepseek_api_key`) stores it in the
  Keychain. The background service cannot see variables from your terminal, so use the Keychain on a Mac.
- Linux / Docker: `ANTHROPIC_API_KEY` / `OCTOPUS_<NAME>_API_KEY` (upper case, `-` as `_`), or `secret set` (stored in
  `secrets.toml` in the data folder, mode 600).
- When both exist, the environment wins. Keys are read on every call; after editing `config.toml`, restart the service.

`uv run octopus models` lists the models in use and whether each key is set (never the key); `uv run octopus models
test` sends each model a very short message (API calls cost a little). Busy servers (429, 5xx) are retried up to 3
times; a rejected key is reported and the fallback is used.

**3. Fully local.** `primary = "local"` and `fallback = ""` for both tasks; the local model is set in `[local_llm]`.
Nothing leaves your machine except the links you ask it to read. `/model local` switches temporarily; `/model
default` switches back.

## Channels

Channels are independent; enable at least one in `config.toml` and the service will start. Each chat channel answers
only the accounts on its allow-list: **put only your own accounts there** (system notices such as a ClawBot QR code
go to every allow-listed account). After changing the config or a secret, run `uv run octopus service restart`.

`uv run octopus secret set <name>` stores a secret; `uv run octopus secret list` shows which are set and where from.

### Telegram

1. Message @BotFather, send `/newbot`, follow the prompts and copy the token.
2. `uv run octopus secret set telegram_token`
3. Find your numeric user id (for example with @userinfobot) and add it:

```toml
[channels.telegram]
enabled = true
allowed_users = ["123456789"]
```

### Feishu (Lark)

1. In the Feishu open platform, create a self-built app and add the bot capability.
2. Grant `im:message`, `im:message.p2p_msg:readonly`, `im:resource` and `im:message:send_as_bot`.
3. `uv run octopus secret set feishu_app_id` and `uv run octopus secret set feishu_app_secret`.
4. Set `[channels.feishu] enabled = true` and start `uv run octopus run` once (the console only accepts the long
   connection while it is running). In the console, subscribe to "receive message v2.0" and to card callbacks over
   the long connection, then publish a version.
5. Message the bot; it is ignored, but the log names your `open_id`
   (`grep "ignoring message" ~/Octopus/logs/octopus.log`). Put it in `allowed_users = ["ou_…"]` and restart.

### WeChat ClawBot

1. Make sure your WeChat has the ClawBot plugin (Me → Settings → Plugins).
2. `uv run octopus clawbot-login` shows a QR code; scan it with WeChat.
3. Set `[channels.clawbot] enabled = true`. The account that scanned is the owner.

ClawBot can only reply after you have written to it recently: results and notices wait until your next message.
It sends at most 3 messages per 8 minutes, so `/read` sends up to 3 parts and ends with how to go on; it cannot send
files, so `/read <id> file` comes in parts there.
When it needs a new login while the service runs, the QR code is sent to your other channels (or saved as
`clawbot-qr.png` in the data folder); `/clawbot` asks for a fresh one. ClawBot uses Tencent's iLink bot interface.

### Discord

1. In the [Discord developer portal](https://discord.com/developers/applications): New Application → Bot → Reset
   Token. No privileged intents are needed (Octopus reads direct messages only).
2. `uv run octopus secret set discord_bot_token`
3. OAuth2 → URL Generator → scope `bot`; invite it to a server of yours (Discord only lets you DM a bot you share a
   server with).
4. Enable Developer Mode, right-click your avatar → Copy User ID, and add it:

```toml
[channels.discord]
enabled = true
owner_user_ids = ["123456789012345678"]
```

5. Restart the service and send the bot a direct message. Files up to 100 MB.

Typing `/` in Discord opens its own command menu, and Discord's built-ins (`/shrug`, `/spoiler`, `/giphy`, `/tts`,
`/me` …) are in it too: `/s` + Enter can pick `/shrug` or `/spoiler` instead of sending your text. Octopus puts its
commands in that menu (registered when the service starts; nothing to set up). Pick ours, the ones with the bot's
icon, and type what follows into the `text` field (one line; for several lines, type the command as a message). A
command typed out and sent as a message, not picked from the menu, is understood as before. Use a bot application
just for Octopus: at start it replaces the application's global commands with its own.

### Email (save only)

Forward articles, newsletters, links and attachments to a dedicated mailbox; Octopus checks it every minute, saves
what arrives and replies with the title, summary and id. Email is for saving only, never chat.

- A body that is only links: each link is saved (up to 10); unsubscribe links are never opened.
- A forwarded newsletter or HTML mail: saved from the mail itself, with its "view in browser" link noted.
- Other text: a note titled with the subject. Attachments are saved like files sent in chat.

To stop anyone else from impersonating you, a mail is saved only when it comes from one of your `owner_addresses`
**and** the receiving provider authenticated it (DMARC pass; for a sender domain without DMARC, aligned DKIM or SPF).
Octopus reads the provider's own `Authentication-Results` header, matched by its server name, so a forged header in
the mail does not count. **Use Gmail for the receiving mailbox** (its server name, `mx.google.com`, is built in);
another provider works only if it writes that header with a fixed server name, which you put in `authserv_id`. QQ Mail
and Outlook / Hotmail are not supported. Send from an address whose domain publishes DMARC (Gmail does).

1. Create a new Gmail address just for this (Octopus reads every new mail in it), turn on 2-step verification and
   create an app password. Do not let this mailbox fetch mail from other accounts.
2. `uv run octopus secret set email_password`
3. Configure and restart:

```toml
[channels.email]
enabled = true
imap_host = "imap.gmail.com"
smtp_host = "smtp.gmail.com"
username = "my-octopus-box@example.com"
owner_addresses = ["me@example.com"]   # the addresses you send from
reply = "email"                         # or "telegram", "none"
```

### Chrome extension (save only)

Saves the page you are looking at, a selection or an image, as you see it: pages behind a login or a paywall that you
can read are saved too, usually without Octopus fetching them again (video and podcast links
are still fetched by link, X posts too, for their videos and full text, with the page as the fallback; and a page with
very little text is tried again by its link). Which platforms need it:
[Which platforms need the extension](#which-platforms-need-the-extension). The extension talks only to Octopus on
`127.0.0.1`.

1. In `config.toml`: `[channels.extension]` with `enabled = true` and `port = 8765`.
2. `uv run octopus extension token` shows its password; `uv run octopus service restart`.
3. In Chrome: `chrome://extensions` → Developer mode → Load unpacked → the `extension/chrome` folder.
4. Open the extension's settings (they show the command too), paste the password, click Save ("Connected to Octopus").

Three ways to save:

- **The popup**: click the toolbar icon (pin it first) and press **Save this page** at the top; opening the popup
  saves nothing. Below the button are the last 10 saves and their status. If the tab saved something in the last
  10 minutes, the button reads "Saved already — save again" (it still saves); on pages that can't be saved
  (`chrome://`, a new tab) it is disabled.
- **The keyboard shortcut**: Alt+Shift+S (Option+Shift+S on a Mac) saves the page you are on without the popup; the
  icon shows ✓ or !. Within 10 minutes of a save from that tab it sends nothing, the ✓ is grey and the icon's tooltip
  says it was saved already. The extension's settings show the key Chrome assigned; change it at
  `chrome://extensions/shortcuts` (Chrome leaves the shortcut unset when another extension already uses it: set one
  there).
- **Right-click** a selection, an image or the page → "Save to Octopus" (the result shows on the icon too).

A link saved before (sent from your phone, say) that was not read, or read only in part, is filled in when you save
its page: the same entry and id, "Filled in the entry saved earlier" in the list ("Updated the entry saved earlier"
when it is still incomplete). Only when the page reads more than the entry has, and only while the entry is still
incomplete (one completed with screenshots or text, or read in full, is "already saved" as before); YouTube,
Bilibili and podcast links are saved by link and never filled this way. A title you set with /title and text you
edited in Obsidian (the title too, for entries saved from this version on) stay: when the text was edited, the page goes below it under "## Page content (extension)";
supplements and /note stay too. The phone's share link and the address Chrome opens may differ: Xiaohongshu
(xhslink.com / xhslink.cn short links), Threads and the redirects Octopus followed are recognised as the same entry; the page's own
canonical address only fills an incomplete entry saved under exactly that address (never makes it "already saved",
and never one above the page, like its section); a Reddit app share link (`/s/…`) is not (Octopus never reads Reddit), nor a Weibo mobile link whose
number differs from the desktop one: those are saved as a new entry.

Results are not posted to chat unless you tick "Also send to Telegram". Chrome says the extension can "read and change your data on 127.0.0.1": that is how it reaches Octopus; it
cannot read any website. The extension follows the browser's language (its settings can fix English or Chinese).
After updating the repository, reload it in `chrome://extensions`.

### Safari and other Mac apps (Shortcuts)

They use the extension's endpoint, so do steps 1–2 above first (the Chrome extension itself is not needed).

Build and sign the shortcuts with your own identity first (macOS; `shortcuts sign` uses the Apple account you are
signed in with): `uv run python extension/shortcuts/build.py --lang en` (or without `--lang` for both languages).
Signed `.shortcut` files carry the signer's certificate, which is why they are not shipped. No paid Apple Developer
account is needed: `build.py` runs `shortcuts sign --mode anyone`, which only needs this Mac to be signed in to an
Apple ID (iCloud). Without one, build the two shortcuts by hand with
[docs/shortcuts-manual.md](docs/shortcuts-manual.md): shortcuts you create yourself in the Shortcuts app need no
signing. After `build.py`, run `uv run octopus extension token --copy` and double-click the English shortcuts in
`extension/shortcuts`:

- `Save to Octopus (page).shortcut`: Safari → Share → Save to Octopus (page). Octopus reads the link, as if you had
  sent it in chat.
- `Save to Octopus (selected text).shortcut`: select text in any app → right-click → Services → Save to Octopus
  (selected text).

Each asks for the password once, when you add it. The password stays inside the shortcut (and syncs through iCloud,
where it is useless: it only reaches `127.0.0.1`); do not share a shortcut after filling it in: anyone else builds and signs their own with `build.py`. Without `--lang`,
`build.py` also makes the same shortcuts in Chinese (`存到 Octopus（…）.shortcut`). If a shortcut file will not import, build it by
hand with [docs/shortcuts-manual.md](docs/shortcuts-manual.md).

## Command line

All commands are `uv run octopus <command>`; `--config <path>` (first) selects another config file.

| Command | What it does |
|---|---|
| `init [--language en\|zh]` | write the default config (in the system language unless given) |
| `chat` | use Octopus in the terminal |
| `run` | run the service in the foreground (all enabled channels and the worker) |
| `reindex` | rebuild the index from the Markdown |
| `backup` | back up the database now |
| `secret set <name>` / `secret list` | store a secret / show which are set and where from |
| `clawbot-login` | log in to WeChat ClawBot |
| `extension token [--copy]` | the extension's (and Shortcuts') password |
| `models` / `models test` | show the models and keys / try each model |
| `library move <dir>` / `adopt <dir>` / `sync [--apply-deletes]` | move the notes (e.g. into an Obsidian vault) / use a folder that was moved by hand / list (and drop) notes whose files are gone |
| `service install` / `restart` / `stop` / `start` / `uninstall` / `status` | manage the background service |

## Background service

```bash
uv run octopus service install    # launchd on macOS, systemd --user on Linux
uv run octopus service status
```

- `stop` stops it for now (it starts again at the next login); `uninstall` removes it.
- While the service runs, `chat`, `reindex`, `clawbot-login` and `library …` refuse to run: only one Octopus may use
  the data at a time. Stop the service first, or just use a chat channel.
- macOS: it is a LaunchAgent, so it starts after you log in, not at boot. Run `service install` from a logged-in
  desktop session, and again after moving the repository or recreating `.venv`. The first Keychain read may ask for
  permission: choose "Always Allow".
- Linux: `loginctl enable-linger $USER` once makes it run without a login. Output goes to
  `~/Octopus/logs/systemd.out.log` / `.err.log` (launchd: `launchd.out.log` / `.err.log`).

## Where your data is

Everything is in `~/Octopus` (`/data` in Docker):

- `library/`: the notes, one Markdown file per entry by year and month, with YAML front matter.
- `attachments/`: saved images, audio and files.
- `octopus.db`: the database (index and runtime state); `backups/` keeps the 7 most recent daily copies.
- `trash/`: deleted notes. `logs/`: logs. `config.toml`: settings (no secrets).

The Markdown files are the real data; the index is a cache (`uv run octopus reindex` rebuilds it). Back up the whole
folder with your usual tools (Time Machine, a synced drive). To restore the database from a backup: stop the service,
delete `octopus.db-wal` and `octopus.db-shm` if present, copy `backups/octopus-YYYYMMDD.db` to `octopus.db`, start
again and run `reindex` if notes were saved after the backup.

## Obsidian

1. `uv run octopus service stop`
2. `uv run octopus library move ~/Documents/MyVault/Octopus` copies the notes and attachments, checks each one,
   sets `library_dir` in `config.toml` and keeps the old files in `backups/`. (With no notes yet, just set
   `library_dir`.)
3. `uv run octopus service start` and open the vault in Obsidian.

What you can edit in Obsidian: the body, summary, key points and your comments (re-indexed within a minute); the
title (`title` in the front matter, or a `# heading` above the summary); tags; the file name and folder (notes are
known by their `id`). Do not change the `id` or rename files in `attachments/`. A note deleted in Obsidian leaves the
index after about two minutes; if many disappear at once, Octopus does nothing and asks you to confirm with
`library sync --apply-deletes`. Notes without an Octopus `id` are never touched. Keep the hidden `.octopus-library`
file: it marks the folder as Octopus's.

## Linux and Docker

Native Linux works like macOS with faster-whisper (CPU; the `small` model by default, `[media] faster_whisper_model`
for larger ones) and RapidOCR (Chinese and English; HEIC images are not supported, send JPG / PNG). Secrets come from
the environment (`OCTOPUS_<NAME>`, `ANTHROPIC_API_KEY`) or `secrets.toml`; `[secrets] backend = "keyring"` uses an
unlocked Secret Service keyring instead.

```bash
sudo apt install ffmpeg
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
uv sync
uv run octopus init --language en
```

Docker:

```bash
mkdir -p data                                    # notes, config and secrets live here
docker compose build
docker compose run --rm octopus init --language en
# edit data/config.toml: models (API key or local) and channels
docker compose run --rm octopus secret set telegram_token
docker compose up -d
```

- The image has no Claude Code: use `anthropic:…`, `openai:<name>` or local models.
- For search, run the bundled Ollama: `docker compose --profile ollama up -d`, `docker compose exec ollama ollama pull
  bge-m3`, and set `base_url = "http://ollama:11434"` in `[embed]` (and `[local_llm]`).
- Chrome extension: set `bind = "0.0.0.0"` in `[channels.extension]` (only inside the container); compose publishes
  the port on `127.0.0.1` only. Its password: `docker compose run --rm octopus extension token` (not `uv run` on the
  host, which makes a different one).
- The container runs as uid 1000 (`OCTOPUS_UID` / `OCTOPUS_GID` in `.env` to change it) and uses `TZ` from `.env`
  (default `UTC`).
- Tests in the container: `docker build --target test -t octopus-test . && docker run --rm octopus-test`.

## Windows

Native Windows is not supported yet. Two ways that work:

**WSL2 (recommended).** `wsl --install` (in an administrator PowerShell) sets up Ubuntu. Inside Ubuntu, follow
"Linux and Docker" above: uv, `sudo apt install ffmpeg`, faster-whisper and RapidOCR, secrets from the environment or
`secrets.toml`.

- Background service: put `[boot]` and `systemd=true` in `/etc/wsl.conf`, run `wsl --shutdown` in Windows, open
  Ubuntu again, then `uv run octopus service install` and `loginctl enable-linger $USER`.
- WSL stops a distribution soon after its last Windows-side process ends, and the service stops with it. To keep it
  running, add a Task Scheduler task "At log on" that runs `wsl.exe -d Ubuntu --exec sleep infinity`.
  Set it to run hidden ("Run whether user is logged on or not", or a hidden-window launcher).
- Models: `claude:…` works with Claude Code installed and logged in inside Ubuntu; otherwise use an API key or local
  models. Install Ollama inside Ubuntu too (`ollama pull bge-m3`), so the default `127.0.0.1:11434` just works.
- Chrome extension (in Windows): WSL2 forwards `127.0.0.1:8765` from Windows to Ubuntu by default, so keep
  `bind = "127.0.0.1"`. Copy `extension/chrome` to a Windows folder (for example
  `cp -r extension/chrome /mnt/c/Users/<you>/octopus-chrome`) and load that one unpacked.
- Obsidian on Windows: open the notes at `\\wsl$\Ubuntu\home\<user>\Octopus\library`, or a vault folder you set as
  `library_dir` inside Ubuntu. Keep the notes in the Linux file system; Octopus re-scans the folder every minute, so
  edits made from Windows are picked up.
- `Ubuntu` above stands for your distribution's name as `wsl -l -v` shows it (it may be e.g. `Ubuntu-24.04`).

**Docker Desktop (WSL2 backend).** The Docker steps above apply as they are, including the port published on
`127.0.0.1:8765` for the extension. As on Linux, the container has no Claude Code: use API keys or Ollama (the
compose `ollama` profile, or Ollama for Windows at `http://host.docker.internal:11434`).

macOS-only parts (Shortcuts, MLX, macOS Vision OCR, Keychain, launchd) do not apply.

## Privacy

All notes, the index, backups and logs stay on your machine. What leaves it:

- **Model calls**: the text being summarized or the question with the matching notes goes to the provider you chose
  (Claude through your subscription, or the API service you configured). With `local` models nothing is sent.
- **Links you send**: Octopus fetches those pages (and downloads media for transcription). With
  `x_full_text_service = "fxtwitter"` (off by default), the links of long X posts are also sent to the FxTwitter
  service (api.fxtwitter.com) to get their full text.
- **Channels**: messages pass through the platforms you enable (Telegram, Feishu, Tencent for ClawBot, Discord, your
  mail provider).
- **One-time downloads**: the speech-to-text model from Hugging Face on first use.

Secrets are kept in the Keychain (or the environment / `secrets.toml` on Linux and Docker), never in the config or
logs. Browser cookies are never read unless you set `youtube_cookies_from_browser`.

## Disclaimer

Octopus is a personal tool provided as is, without warranty. You are responsible for using it within the terms of
every service it touches: it does not bypass logins, paywalls or anti-bot measures, and reading browser cookies is
off by default. WeChat ClawBot works through Tencent's iLink bot interface; the `claude:` models use your own Claude
subscription under Anthropic's terms (use an API key when running Octopus for anyone else); email trust depends on
your provider's authentication results (Gmail, DMARC). See [DISCLAIMER.md](DISCLAIMER.md).

## FAQ

- **Something does not respond.** Send `/status`, then read `~/Octopus/logs/octopus.log`.
- **Claude is not available where I am.** Send `/model local`; failed Claude calls also fall back to the local model
  and the reply says so.
- **YouTube blocks downloads.** Set `youtube_cookies_from_browser = "chrome"` (or `"safari"`) in `[media]` to use
  your browser's YouTube login. It is off by default.
- **A long X post stops after 280 characters.** X's public endpoint gives only the start. Set
  `x_full_text_service = "fxtwitter"` in `[media]` to get the full text from the FxTwitter service (off by default;
  see Privacy). Without it the note is partial and asks for screenshots.
- **"Could not get the content."** The page needs a login or blocks automated access. The entry is kept: send
  screenshots or paste the text within 30 minutes (later, quote-reply that message), or use the Chrome extension.
- **Keychain prompts on macOS.** Choose "Always Allow"; after moving the repository, run `service install` again.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, tests and conventions, and
[ARCHITECTURE.md](ARCHITECTURE.md) for how the parts fit together. Report security issues privately as described in
[SECURITY.md](SECURITY.md). Dependencies and their licenses are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); [CHANGELOG.md](CHANGELOG.md) lists releases.

## License

Octopus is released under the [MIT License](LICENSE). Its dependencies keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
