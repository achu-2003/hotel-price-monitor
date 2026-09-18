"""The HTTP call behind the advisor, and the only file that knows the provider.

``repricing_advisor`` builds the prompt, bounds the answer and records it;
this builds one request and returns the model's JSON as a string. The split
is what lets every branch of the advisor be tested without a key, and what
keeps a change of provider to this file.

WHY httpx AND NOT THE VENDOR SDK
================================
One POST with a JSON body. The project already depends on ``httpx`` for every
other outbound call and already has a timeout and retry idiom for them;
adding an SDK for this would bring a second HTTP stack, its own transitive
pins, and its own opinions about retries into a worker that runs Playwright.
The request shape below is the documented one and is stable.

STRUCTURED OUTPUT, NOT PARSED PROSE
===================================
``response_format`` with a strict JSON schema makes the provider reject its
own answer if it does not match, rather than handing back a paragraph with a
number in it for us to regex. ``temperature`` is 0 so the same night asked
twice gives the same answer -- a shadow log full of noise would be unreadable,
and a rate that moves because a sampler rolled differently is indefensible.
"""
from __future__ import annotations

import httpx


class AdvisorCallError(RuntimeError):
    """Anything that stopped a usable answer coming back.

    One class on purpose: the advisor treats every failure the same way --
    record it, carry on with the rule -- so the caller never branches on
    which kind it was. The message carries the detail for the log.
    """


def completer(*, api_key: str, base_url: str, timeout: float):
    """Build the ``complete`` callable ``repricing_advisor.advise`` expects."""

    def complete(*, system: str, user: str, schema: dict, model: str) -> str:
        body = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "rate_position", "strict": True, "schema": schema},
            },
        }
        try:
            response = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=timeout,
            )
        except httpx.HTTPError as exc:
            raise AdvisorCallError(f"request failed: {exc}") from exc

        if response.status_code != 200:
            # The body carries the provider's own reason (bad model name,
            # quota, revoked key) and is far more useful than the status
            # alone -- it is what turns "advisor is broken" into "the model
            # name in .env does not exist on this account".
            detail = response.text[:300].replace("\n", " ")
            raise AdvisorCallError(f"HTTP {response.status_code}: {detail}")

        try:
            payload = response.json()
            choice = payload["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise AdvisorCallError(f"unexpected response shape: {exc}") from exc

        # A refusal is a 200 with the answer replaced. Surfaced as an error
        # rather than parsed, so it lands in the shadow log saying what it is.
        if choice.get("refusal"):
            raise AdvisorCallError(f"model refused: {str(choice['refusal'])[:200]}")

        content = choice.get("content")
        if not content:
            raise AdvisorCallError("model returned no content")
        return content

    return complete


def completer_from_settings(settings):
    """``None`` when this deployment has no key, which disables the advisor."""
    key = settings.openai_api_key
    if key is None or not key.get_secret_value():
        return None
    return completer(
        api_key=key.get_secret_value(),
        base_url=settings.openai_base_url,
        timeout=settings.advisor_timeout_seconds,
    )


__all__ = ["AdvisorCallError", "completer", "completer_from_settings"]
