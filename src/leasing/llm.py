"""Thin helpers around the Anthropic SDK shared by the agent and the brief writer."""

from typing import Any

import anthropic
from anthropic.types.beta import BetaMessage

# Server-side refusal fallback: if a request is declined by a safety classifier, the API re-runs
# it on Anthropic's recommended fallback model inside the same call instead of returning the
# refusal. "default" routes by refusal category, so there is no model list to maintain.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def create_message(client: Any, **params: Any) -> BetaMessage:
    return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **params)


def text_of(message: BetaMessage) -> str:
    return "".join(block.text for block in message.content if block.type == "text").strip()


def dump_content(message: BetaMessage) -> list[dict[str, Any]]:
    """Content blocks as JSON-safe dicts, unchanged, so they can be replayed on the next turn."""
    return message.model_dump(mode="json", exclude_none=True)["content"]
