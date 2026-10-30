# Manual testing

The automated suite (`uv run pytest`) never talks to real chat platforms, model providers, mail servers, the
Keychain or the service manager: every one of them is faked. This checklist covers what only a real setup can show.
Run the parts your change touches, on a throwaway data directory (`export OCTOPUS_DATA_DIR=/tmp/octo-dev`
before `uv run octopus init`, so `~/Octopus` is never touched) or on your own install.

Use content you made up or public pages you do not mind saving; do not paste the results of someone else's private
conversations into an issue.

## Core (terminal)

1. `uv run octopus init`, then `uv run octopus chat`. `init` says which language it picked (Chinese on a Chinese
   system, otherwise English) and the config has that `language` line; `--language zh` / `en` overrides it.
2. `hi` shows the guide. `/s a short test note` replies "Got it" and then the saved note with a summary and an id.
3. Paste a public article link: a note with title, summary, key points and tags; `/recent` lists it.
4. Ask a question about it: the answer cites `[1]` with the note's title. `/read <id>` shows the full text;
   `/find <word>` finds it.
5. "delete the one above" asks for confirmation; `yes` moves the file to `trash/`.
6. `/model local` (needs Ollama): the next answer comes from the local model; `/model default` switches back.

## Media

- A YouTube link and a podcast episode link: transcript saved (first run downloads the Whisper model).
- An X post with a video: the post's text, then the video's transcript. A long X post: full text with
  `x_full_text_service = "fxtwitter"`, otherwise partial with the 280-character note.
- A screenshot with text: OCR text saved. On Linux, also a transparent PNG.
- A PDF link and a PDF file: text extracted.
- A voice message on any chat channel: handled as if typed.

## Chat channels

For each channel you changed (Telegram, Feishu, WeChat ClawBot, Discord):

- A message from an account that is not on the allow-list is ignored and logged as such.
- Buttons work (Save this, Make it a question, Confirm / Cancel), also after `uv run octopus service restart`.
- ClawBot: a reply that could not be sent is delivered after the next message you send.

## Save-only entrances

- Chrome extension (`extension/chrome`, loaded unpacked): opening the popup saves nothing; save a page with its "Save
  this page" button and with Alt+Shift+S, a selection and an image with right-click; the popup shows the status; a
  wrong password says so; with the service stopped it says Octopus is not running.
- macOS Shortcuts (`extension/shortcuts`, built and signed with `build.py`): the page shortcut from Safari's share
  sheet, the text one from the Services menu.
- Email: a link, a forwarded newsletter and an attachment from an owner address are saved and answered; mail from
  any other address stays unread.

## Service and storage

- `uv run octopus service install` / `status` / `restart` / `stop` / `start` / `uninstall` (launchd on macOS, systemd
  `--user` on Linux).
- Obsidian: `uv run octopus library move <vault folder>`; an edit made in Obsidian is found by the next question; a
  renamed note keeps working.
- `uv run octopus backup`; the restore steps in the README bring the database back.
- Docker: `docker compose build`, `docker compose run --rm octopus init`, `docker compose up -d`, and the test image
  (`docker build --target test -t octopus-test . && docker run --rm octopus-test`).

## Language

With `language = "en"`: replies, buttons, the Telegram menu, the CLI and `--help` are English; Chinese commands still
work. With the default `language = "zh"`, a message in Chinese gets exactly the same replies as before.
