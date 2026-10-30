# Contributing

Thanks for helping. Octopus is a single-user tool that people run on their own machines, so changes are judged by
two questions: does it keep the owner's data safe and local, and does it keep working for someone who set it up a year
ago?

Read [ARCHITECTURE.md](ARCHITECTURE.md) first; it explains how a message travels from a channel to a note.

## Development setup

```bash
git clone https://github.com/andrewman2035/octopus-kb.git && cd octopus-kb
uv sync                 # Python 3.12 and every dependency, including the dev group (pytest, respx)
uv run octopus --help
```

macOS (Apple Silicon) and Linux are both supported; platform-specific dependencies are selected by `uv.lock`. You do
not need Ollama, a model key or any chat account to run the tests.

To try a change by hand, use a throwaway data directory so your own `~/Octopus` is never touched:

```bash
export OCTOPUS_DATA_DIR=/tmp/octo-dev   # config.toml, notes, database and logs all go here
uv run octopus init
uv run octopus chat
```

On macOS secrets still go to your Keychain; add `[secrets]` with `backend = "file"` to `/tmp/octo-dev/config.toml` to
keep them in that folder too.

[docs/manual-testing.md](docs/manual-testing.md) lists what to check by hand for channels, media and the service.

## Tests

```bash
uv run pytest                      # the whole suite, about a minute
uv run pytest tests/test_router.py -q
uv run pytest -m live              # only with real models and network; never needed for a pull request
```

The suite must stay green on macOS and Linux (CI runs both). Tests never touch the real home directory, Keychain,
launchd / systemd, chat platforms, model providers or websites: `tests/conftest.py` gives every test its own `HOME`
and fails any test that touches the real keyring; HTTP is mocked with `respx`; channels and models have fakes in
`tests/fakes.py`. Keep it that way: a test that needs the network or a real account belongs behind the `live` marker.

**Write the test first.** For a bug, add a test that fails for the reason in the report, then fix the code until it
passes. For a feature, start from the behaviour a user would see (a message in, a reply out) and add focused unit
tests where the logic is subtle. Fix the code, not the test: a test is only changed when the behaviour it pins is
meant to change.

## Lint

```bash
uvx ruff check --select F,E9 <the files you changed>
node --check extension/chrome/*.js          # when you touch the extension
```

Match the surrounding style: lines up to 120 characters, comments that say why rather than what, no new abstraction
for a single use.

## User-facing text (i18n)

Octopus speaks Chinese or English (`language` in `config.toml`) and understands both whatever it speaks.

- Every string a user can see (replies, buttons, notices, errors, CLI output, help) lives in **both**
  `src/octopus/i18n/zh.py` and `src/octopus/i18n/en.py`, under the same key with the same named placeholders, and is
  looked up with `t("module.name", …)`. `tests/test_i18n.py` checks that the catalogs match.
- No Chinese characters in code, comments or docstrings outside `i18n/zh.py`. Code that has to match Chinese input or
  third-party content carries a `# zh: <reason>` mark (`input`, `content`, `log`, `wire`); `tests/test_i18n_lint.py`
  enforces this and caps the number of marks.
- A new command word must work in both languages (`tests/test_english_input.py` mirrors the Chinese routing tests).
- The Chrome extension's words are in `extension/chrome/_locales/en` and `_locales/zh_CN`. After editing either, run
  `uv run python extension/chrome/build_locales.py`; it regenerates `_locales/zh_TW` and `messages.js`, and a test
  fails if they are stale.
- Log lines are not translated (their format is fixed so they can be searched).

## Privacy in the repository

Test data must be invented: no real names, addresses, entry ids, article titles or chat messages from your own use.
Use `example.com` / `example.org` / `example.net` (or `.test`) for email addresses and `/Users/me` or `/home/me` for
paths; `tests/test_no_private_data.py` checks the public files.

## Commits and pull requests

- One logical change per commit. Messages start with a type and say what changed for the user, e.g.
  `fix: a link inside full-width parentheses ends at the parenthesis`, `feat: …`, `docs: …`, `test: …`.
- A pull request describes the behaviour before and after, how it was tested (the commands, and any manual check),
  and anything a user has to do after upgrading (a config key, a migration).
- Changes to security-relevant code (owner checks, the localhost endpoint, email trust, secrets) need a test for the
  attack they prevent.

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md).
