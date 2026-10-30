## What changes for the user

<!-- Behaviour before and after. Link the issue if there is one. -->

## How it was tested

- [ ] `uv run pytest` passes
- [ ] `uvx ruff check --select F,E9` on the changed files (and `node --check` for extension JS)
- [ ] New behaviour has a test that failed before the change
- [ ] Manual check (which part of docs/manual-testing.md, on which platform):

## Checklist

- [ ] User-facing text is in both `i18n/zh.py` and `i18n/en.py` (and the extension's `_locales`, rebuilt with
      `extension/chrome/build_locales.py`)
- [ ] README.md and README.zh-CN.md updated together when behaviour or setup changed
- [ ] No personal data in tests or docs (invented ids, titles, `example.com` addresses)
- [ ] Anything users must do after upgrading is described above
