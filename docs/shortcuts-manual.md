# Building the Shortcuts by hand

The two macOS Shortcuts that save to Octopus can be built click by click in the Shortcuts app instead of with
`extension/shortcuts/build.py`. Use this when you cannot sign with `build.py` (the Mac is not signed in to an Apple ID)
or a `.shortcut` file will not import. A shortcut you create yourself in the Shortcuts app needs no signing, and no
Apple Developer account is involved either way. (The same steps in Chinese: [setup.md](setup.md), section 10.)

First turn on the local endpoint as in the README's "Chrome extension" section, steps 1–2: `[channels.extension]`
with `enabled = true`, then `uv run octopus extension token` and `uv run octopus service restart` (the Chrome
extension itself is not needed).

## Save to Octopus (page)

1. Shortcuts app → File → New Shortcut. Name it "Save to Octopus (page)".
2. In the ⓘ Shortcut Details on the right, turn on **Show in Share Sheet**. In the input line at the top, receive
   **Safari web pages** and **URLs** from the **Share Sheet**.
3. Add a **Text** action and paste the password (`uv run octopus extension token --copy` copies it).
4. Add a second **Text** action with `http://127.0.0.1:8765/save` (change the port if you changed it in
   `config.toml`).
5. Add **Get URLs from Input** (input: Shortcut Input).
6. Add **Get Contents of URL**:
   - URL: the second Text (the address);
   - Method: **POST**;
   - Headers: key `Authorization`, value `Bearer ` (with the space) followed by the first Text (the password);
   - Request Body: **JSON**, with `kind` (Text) = `page` and `url` (Text) = the URLs from step 5.
7. Add **Get Dictionary Value**: get the value for key `id` in Contents of URL.
8. Add **If** Dictionary Value **has any value** → **Show Notification** "Octopus got it and is saving it";
   **Otherwise** → **Show Notification** "Not saved".

## Save to Octopus (selected text)

Repeat the steps above with these differences:

- Name it "Save to Octopus (selected text)". In Shortcut Details turn on **Use as Quick Action** and **Services
  Menu**, and receive **Text**.
- Skip step 5.
- In the Request Body, `kind` (Text) = `selection` and `text` (Text) = Shortcut Input.

## Using them

- Page: Safari (or any app that shares a link) → Share → Save to Octopus (page).
- Selected text: select text in any app → right-click → Services → Save to Octopus (selected text).

The first time, macOS asks whether the shortcut may connect to 127.0.0.1: choose **Always Allow**. "Not saved" means
the password is wrong: paste it again into the first Text action. If Octopus is not running, Shortcuts shows its own
"could not connect" error. The password stays in the shortcut and syncs through iCloud, where it is useless (it only
reaches `127.0.0.1`); do not share the shortcut after putting the password in.
