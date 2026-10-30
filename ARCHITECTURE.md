# Architecture

Octopus is one Python process (`octopus run`) that owns a data directory. Chat channels and save-only entrances
turn whatever arrives into a channel-independent `IncomingMessage`; the app decides what it means; slow work
(fetching, transcription, summarizing) runs as persisted jobs; the result is a Markdown note plus a search index;
questions are answered from that index with numbered sources. Everything lives on the user's machine; the only
outbound traffic is to the model provider the user configured, the sites behind the links they send (and FxTwitter
for long X posts when `x_full_text_service` is set), and the chat platforms they enabled.

Module paths below are under `src/octopus/`.

## Components

| Area | Modules | Role |
|---|---|---|
| Entry point | `cli.py`, `bootstrap.py`, `instance.py` | Argument parsing; wiring config → database → library / index / models → `App`; a lock so only one process runs channels and the worker |
| Channels | `channels/` (`cli`, `telegram`, `feishu`, `clawbot` + `ilink`, `discord`, `email`, `extension`) | Receive messages and send replies; each implements `channels/base.py` |
| Message handling | `app.py`, `router.py`, `commands.py`, `manage.py`, `ops.py`, `focus.py`, `pending.py`, `actions.py`, `outbox.py` | Classify a message, run commands, ask the user to confirm changes, remember what "this one" means, hold replies that could not be delivered |
| Jobs and pipeline | `jobs.py`, `pipeline.py`, `fetchers/`, `media/`, `summarize.py` | Persisted save queue with retries; fetch → extract or transcribe → summarize → write Markdown → index |
| Library and index | `library.py`, `db.py`, `index/`, `embed.py`, `sync.py`, `move.py`, `backup.py` | Markdown notes as the source of truth; SQLite with FTS5 and sqlite-vec as a rebuildable cache; Obsidian sync; daily backups |
| Question answering | `qa.py`, `conversations.py` | Hybrid retrieval, prompt building, numbered citations, short chat memory |
| Models | `llm/` (`router`, `claude_cli`, `anthropic_api`, `openai_compat`, `ollama`, `api`) | Per-task primary / fallback chains over the configured providers |
| Platform | `host.py`, `service.py` (launchd), `systemd.py`, `secrets.py`, `config.py`, `settings.py` | OS-specific backends, the background service, secrets, configuration |
| Text | `i18n/` (`zh.py`, `en.py`), `formatting.py`, `text.py`, `urls.py` | Every user-facing string, reply layout, language detection, link extraction and normalization |
| Save-only intake | `extension.py`, `mail.py` | Turn an extension request or an email into ordinary save jobs |

## Data flow

```
channel (Telegram, Feishu, ClawBot, Discord, CLI)      extension endpoint / Shortcuts      email (IMAP poll)
          │ IncomingMessage                                   │ /save request                  │ mail
          ▼                                                   ▼                                ▼
      App.handle ── router.classify ──┐                extension.Intake                 mail.read_mail
          │                           │                       └──────────────┬─────────────────┘
          │  chat / question          │ save (link, text, file, image)       │
          ▼                           ▼                                      ▼
      manage (intent)            jobs (SQLite queue, retries 1 min / 5 min / 30 min)
      qa.answer                       │
          │                           ▼
          │                  pipeline: fetcher → media (ASR / OCR / PDF) → summarize (LLM) → Library.save → index
          │                           │
          ▼                           ▼
      reply (buttons)            "saved" reply through the same channel (or the outbox when it cannot be sent)
```

- **Routing.** `router.classify` is deterministic: links, files and images are saves; `/s` or `save:` saves text;
  bare "save" / "ask" flip the last answer or note; everything else is chat. In chat, `manage.wants_manage` decides
  whether a management request is likely (delete, rename, find …); only then is the intent model called. Changes to
  existing entries are never made directly: `ops.py` records them and the user confirms with a button or a typed yes.
- **Windows.** After `/add <id>`, `/new`, a burst of images or a link that could not be read, `pending.py` keeps an
  entry open so the screenshots or pasted text that follow are merged into it; "save separately" splits the last
  merge back out.
- **Jobs.** `jobs.py` stores each save in SQLite before anything slow happens, so a restart resumes it. A failed job is
  retried after 1, 5 and 30 minutes, then reported.
- **Pipeline.** `pipeline.py` picks a fetcher by URL (`fetchers/__init__.py`), extracts or transcribes, asks the
  summarize model for title / summary / key points / tags (falling back to the local model), writes the Markdown file
  and indexes it. Pages sent by the browser extension use the HTML the user was looking at.
- **Questions.** `qa.py` pins the entries a question names (an id, `@id`, a title, "this one"), adds hybrid search
  hits, and asks the answer model with the material numbered; the reply lists the sources it cited.

## Storage layout

Default data directory: `~/Octopus` (`/data` in Docker; `data_dir` in `config.toml`).

| Path | Content |
|---|---|
| `config.toml` | Configuration (no secrets) |
| `library/YYYY/MM/<id>-<title>.md` | Notes: YAML front matter (id, title, source, url, tags, …), then Summary, Key points, My comments, the original text and an Attachments block. `library_dir` can move it into an Obsidian vault |
| `attachments/<id>/` | Images, audio and files of an entry (next to the library when `library_dir` is set); `attachments/_incoming/` holds files still being saved |
| `octopus.db` | SQLite: jobs, conversations, pending windows, confirmations, outbox, per-entry index rows, FTS5 (`chunks_fts`) and sqlite-vec (`chunks_vec`) tables. Rebuildable from the Markdown with `octopus reindex`, except runtime state |
| `backups/` | One SQLite backup per day, 7 kept |
| `trash/` | Deleted notes |
| `logs/` | `octopus.log` and the service manager's output |
| `secrets.toml` | Only when no keyring is used (Linux / Docker): mode 0600, owner-checked |

Entry ids are `YYYYMMDD-xxxxxx` (date plus six hex characters); users may type the six characters or their first or
last four.

## Channels and entrances

| Channel | Transport | Who is accepted |
|---|---|---|
| CLI (`octopus chat`) | stdin / stdout | The local user |
| Telegram | Bot API long polling (python-telegram-bot) | `allowed_users` |
| Feishu (Lark) | Self-built app over the long connection (lark-oapi WebSocket) | `allowed_users` (open_id) |
| WeChat ClawBot | Tencent's iLink bot interface (HTTP long polling; QR login) | The account that scanned the QR, plus `allowed_users` |
| Discord | Gateway (discord.py), direct messages only, plus `/` application commands (registered at start) | `owner_user_ids` |
| Email (save only) | IMAP poll + SMTP reply | `owner_addresses`, and only mail the receiving provider authenticated (DMARC, else aligned DKIM / SPF) |
| Chrome extension, macOS Shortcuts (save only) | HTTP on `127.0.0.1:<port>` | Requests carrying the extension token |

Channels run under a supervisor in `bootstrap.run_service`: a channel that fails is restarted with backoff without
affecting the others. ClawBot can only reply after the user wrote recently, so replies it cannot send wait in the
outbox and go out with the next message.

## Extension endpoint

`channels/extension.py` is a minimal HTTP/1.1 server inside `octopus run`: `POST /save` (returns 202 with an id) and
`GET /recent`, nothing else. Because any web page can send requests to localhost:

- every request needs `Authorization: Bearer <token>`, compared in constant time (a header a page cannot send without
  a CORS preflight, and preflights are answered only for `chrome-extension://` origins);
- a `Host` other than `127.0.0.1:<port>` / `localhost:<port>` is refused (DNS rebinding);
- heads and bodies are size- and time-limited (15 MB body), one request per connection;
- it binds `127.0.0.1` by default; in Docker it binds `0.0.0.0` inside the container and compose publishes it as
  `127.0.0.1:<port>` on the host. Any other non-loopback bind logs a warning.

The token is created on first use and kept with the other secrets. The macOS Shortcuts post to the same endpoint.

## Obsidian sync

The library folder is plain Markdown, so it can live inside an Obsidian vault (`octopus library move <dir>`). A
hidden `.octopus-library` marker identifies it; without the marker, sync stops and the user is told (a renamed or
moved folder is re-adopted with `octopus library adopt`).

- `sync.py` scans the folder every minute. A file whose modification time and size changed, and whose text differs
  from the index, is re-indexed. Files saved in the last few seconds wait for the next pass.
- Notes are identified by the `id` in their front matter, not by file name: renaming or moving a note is fine. A copy
  with the same id is never treated as the note.
- A note whose file is gone on two passes is dropped from the index; its attachments stay. If many vanish at once
  (more than 5, or more than a tenth), nothing is dropped and the user is warned; `octopus library sync
  --apply-deletes` confirms.
- When Octopus rewrites a note (merge, re-summarize, comment), the summary, key points and tags are regenerated;
  text the user edited in the body and comments is kept, and the user's own front-matter fields survive.
- Files without an Octopus id are never read or changed.

## Model providers

Two tasks, `summarize` and `answer`, each with a `primary` and an optional `fallback` (`llm/router.py`):

| Spec | Provider | Notes |
|---|---|---|
| `claude:<model>` | Claude Code CLI (`llm/claude_cli.py`) | Uses the user's own logged-in Claude subscription; not available in Docker |
| `anthropic:<model>` | Anthropic Messages API (`llm/anthropic_api.py`) | API key; plain HTTPS, no SDK |
| `openai:<name>` | Any OpenAI-compatible endpoint declared in `[providers.<name>]` | DeepSeek, OpenRouter, LM Studio, … |
| `local` | Ollama (`llm/ollama.py`) | Fully local |

Embeddings always come from a local Ollama `bge-m3`. API calls retry on 429 / 5xx a bounded number of times; a
rejected key is reported and the fallback is used. `/model local` switches every task to the local model at runtime.

## Platform backends

`host.py` picks per operating system:

| | macOS | Linux / Docker |
|---|---|---|
| Speech to text | mlx-whisper (Apple Silicon) | faster-whisper (CPU, int8) |
| OCR | Vision via ocrmac | RapidOCR (ONNX) |
| Secrets | Keychain (`keyring`) | Environment variables, then `secrets.toml`; Secret Service only when `backend = "keyring"` |
| Background service | LaunchAgent (`service.py`) | `systemd --user` unit (`systemd.py`); Docker uses `restart: unless-stopped` |

`[media] asr_backend` / `ocr_backend` can force a backend; one that is not installed is reported at startup.

## Security model

- **Owner only.** Octopus is single-user. Every chat channel ignores senders who are not on its allow-list and logs
  one line saying how to allow them. System notices (such as a ClawBot QR code) go only to allow-listed accounts, so
  the allow-lists must hold only the owner's own accounts.
- **Email trust.** Mail is saved only from `owner_addresses` and only when the receiving provider's own
  `Authentication-Results` header (matched by its server name, `authserv_id`; built in for Gmail) says DMARC passed,
  or, for a domain without DMARC, that DKIM or SPF aligned. Header injection through comments and quotes is rejected.
  Other mail stays unread.
- **Localhost endpoint.** See "Extension endpoint" above.
- **Secrets.** Tokens, passwords and API keys never go into `config.toml` or the logs. They are read from the
  environment, the OS keyring (service `octopus`) or `secrets.toml` (refused unless owned by the user and mode 0600).
  `secret list` shows where each one comes from, never its value.
- **Content as data.** Saved pages and files go into prompts as reference material; the prompts tell the model not to
  follow instructions inside them, and changes to entries always need the user's confirmation through Octopus itself.
- **Logs.** One line per incoming message and per reply, with sizes, tags and short previews; secrets, tokens and the
  full text of saves are not logged, links appear by host only.
- **Browser cookies.** `youtube_cookies_from_browser` is empty by default; Octopus reads no browser data unless the
  user sets it.

## Internationalization

Every user-facing string lives in `i18n/zh.py` (the source of truth) and `i18n/en.py`, with the same keys and
placeholders. `language` in `config.toml` picks the one Octopus speaks; new configs follow the system language
(Chinese on a Chinese system, otherwise English), and so does the CLI before a config exists. Input is understood in
both languages whatever it says. Prompts are chosen by the language of the content or question. A lint test
(`tests/test_i18n_lint.py`) keeps Chinese out of the code, comments and docstrings outside the Chinese catalog,
except statements marked `# zh: <reason>`. The Chrome extension has its own `_locales/` (built by
`extension/chrome/build_locales.py`).

## Testing

- `uv run pytest` runs the whole suite (thousands of tests, about a minute). Live tests that need real models or
  network are marked `live` and skipped by default (`-m live` runs them).
- Nothing external is touched: chat platforms, model providers, HTTP sites (`respx`), IMAP / SMTP, the Keychain, the
  service manager and the clock are faked. `tests/conftest.py` gives every test its own `HOME` and fails any test
  that touches the real keyring.
- CI (`.github/workflows/tests.yml`) runs the suite on Ubuntu and macOS; the Docker image has a `test` stage.
- `docs/manual-testing.md` lists what only a real setup can check.
