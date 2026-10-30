# Security policy

## Reporting a vulnerability

Report privately via GitHub's **Report a vulnerability** (Security tab) at
<https://github.com/andrewman2035/octopus-kb/security>: it opens a private security advisory. Do not open a public
issue, pull request or discussion for a vulnerability.

Include what an attacker can do, the steps or a proof of concept, the version (commit) and platform. You will get an
acknowledgement within a week; fixes are released as soon as they are ready, and you are credited in the advisory
unless you prefer otherwise.

## Scope

Octopus is a single-user application running on the owner's machine. In scope:

- **The local save endpoint** (`[channels.extension]`, `127.0.0.1:<port>`): requests without the token being
  accepted, a web page in the browser reaching it (CORS, DNS rebinding, Host checks), resource exhaustion through it.
- **Owner checks**: a message from an account that is not on a channel's allow-list (Telegram, Feishu, WeChat
  ClawBot, Discord) being acted on; mail from someone other than the owner being saved despite the authentication
  rules (forged or injected `Authentication-Results`, alignment bypasses).
- **Secrets**: tokens, passwords or API keys written to the config, logs, notes, replies or files readable by other
  users; `secrets.toml` permission handling.
- **Content handling**: a saved page, file or mail that makes Octopus change or delete entries without the owner's
  confirmation, write outside its data directory, or run commands.
- The Docker image and compose file exposing the endpoint beyond the host.

Out of scope: attacks that need the owner's own unlocked account or local user session; the security of the chat
platforms, model providers and websites themselves; what a model says in an answer (Octopus cites its sources, but
answers can be wrong).

## Supported versions

Only the latest release (and `main`) receives fixes.
