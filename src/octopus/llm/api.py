"""HTTP for the API-key providers: bounded retries when the server is busy, a Chinese hint when it rejects the key.
No key ever reaches an error text (the router logs it and the chat shows it): bodies and transport errors are
scrubbed of it, and a key that could not be a header value is refused before the request."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

from octopus.i18n import aliases, t
from octopus.llm.base import LLMError
from octopus.secrets import api_key_names

log = logging.getLogger(__name__)
ATTEMPTS = 3
BACKOFF = (2, 4)  # seconds before the 2nd and 3rd try when the server does not send retry-after
MAX_RETRY_AFTER = 30  # a longer wait holds the job up: give up and let the fallback model answer
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 529})  # 529: Anthropic "overloaded"
DETAIL_CHARS = 200
__getattr__ = aliases({"KEY_REJECTED": "llm.key_rejected", "NO_KEY": "llm.no_key", "BAD_KEY": "llm.bad_key",
                       "TRUNCATED_NOTE": "llm.truncated"})

# the key itself, or a function that looks it up when a call is made
KeySource = str | Callable[[], str | None] | None


def resolve_key(api_key: KeySource) -> str | None:
    return api_key() if callable(api_key) else api_key


def no_key(name: str, provider: str) -> LLMError:
    secret, env = api_key_names(provider)
    return LLMError(t("llm.no_key", name=name, secret=secret, env=env))


def check_key(name: str, provider: str, key: str) -> None:
    """A header value must be printable ASCII without spaces; anything else would make httpx quote the key in its
    error (or fail with a codec error), so refuse it here without quoting it."""
    if not (key.isascii() and key.isprintable()) or any(c.isspace() for c in key):
        secret, env = api_key_names(provider)
        raise LLMError(t("llm.bad_key", name=name, secret=secret, env=env))


def scrub(text: str, key: str | None) -> str:
    """One line, the key replaced by ***, at most DETAIL_CHARS characters."""
    if key:
        text = text.replace(key, "***")
    return " ".join(text.split())[:DETAIL_CHARS]


def truncated(name: str, text: str) -> str:
    """The reply hit the output cap: say so under the text (a cut summary JSON fails validation anyway)."""
    log.warning("%s stopped at max_tokens: the reply is cut off", name)
    return text + t("llm.truncated")


def _retry_after(r: httpx.Response) -> float | None:
    """retry-after in seconds; the HTTP-date form is not worth parsing (the backoff is used instead)."""
    try:
        wait = float(r.headers.get("retry-after", ""))
    except ValueError:
        return None
    return wait if wait >= 0 else None


def _detail(r: httpx.Response, key: str | None) -> str:
    try:
        msg = r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError, IndexError):
        msg = r.text
    return scrub(str(msg), key)


def _tries(n: int) -> str:
    return f"{n} try" if n == 1 else f"{n} tries"


async def post_json(name: str, url: str, payload: dict, headers: dict[str, str], timeout: float, provider: str,
                    sleep: Callable[[float], Awaitable[object]], key: str | None = None) -> object:
    """The parsed JSON body of a successful POST. provider ("anthropic" or a [providers] name) names the key in the
    hint for 401/403, which is never retried and never quotes the response; key is scrubbed from every error."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise LLMError(f"{name} timed out after {timeout:g}s") from None
        except httpx.ProtocolError as e:  # h11 quotes the offending header value: only the type is safe
            raise LLMError(f"{name} failed: {type(e).__name__}") from None
        except httpx.HTTPError as e:
            raise LLMError(f"{name} failed: {type(e).__name__}: {scrub(str(e), key)}") from None
        if r.status_code in (401, 403):
            secret, env = api_key_names(provider)
            raise LLMError(t("llm.key_rejected", name=name, status=r.status_code, secret=secret, env=env))
        if r.status_code in RETRY_STATUSES:
            note = ""
            if attempt < ATTEMPTS:
                wait = _retry_after(r)
                wait = BACKOFF[attempt - 1] if wait is None else wait
                if wait <= MAX_RETRY_AFTER:
                    await sleep(wait)
                    continue
                note = f" (retry-after {wait:g}s > {MAX_RETRY_AFTER}s, not retried)"
            raise LLMError(f"{name} HTTP {r.status_code} after {_tries(attempt)}{note}: {_detail(r, key)}")
        if r.status_code >= 400:
            raise LLMError(f"{name} HTTP {r.status_code}: {_detail(r, key)}")
        try:
            return r.json()
        except ValueError:
            raise LLMError(f"{name} returned an unexpected response: not JSON: {scrub(r.text, key)!r}") from None
    raise AssertionError("unreachable")
