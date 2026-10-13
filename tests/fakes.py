"""Test doubles. FakeAnthropic returns scripted responses as real SDK ``BetaMessage`` objects,
so the agent code under test handles exactly the types the live API returns."""

import copy
import itertools
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from anthropic.types.beta import BetaMessage

_ids = itertools.count(1)

Scripted = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def message(*blocks: dict[str, Any], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": f"msg_{next(_ids)}",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def text(value: str) -> dict[str, Any]:
    return {"type": "text", "text": value}


def thinking(signature: str = "sig-opaque") -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


def tool_use(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": name, "input": tool_input}


def reply(value: str) -> dict[str, Any]:
    return message(thinking(), text(value))


def call(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return message(thinking(), tool_use(name, tool_input), stop_reason="tool_use")


class FakeAnthropic:
    def __init__(self, *responses: Scripted) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=self)

    def create(self, **params: Any) -> BetaMessage:
        self.requests.append(copy.deepcopy(params))
        if not self._responses:
            raise AssertionError("FakeAnthropic: no scripted response left")
        item = self._responses.pop(0)
        if callable(item):
            item = item(params)
        return BetaMessage.model_validate(item)

    def tool_results(self, request_index: int) -> list[dict[str, Any]]:
        """Tool results the agent sent in a given request."""
        last = self.requests[request_index]["messages"][-1]
        return [b for b in last["content"] if isinstance(b, dict) and b["type"] == "tool_result"]


class FakeStarter:
    def __init__(self) -> None:
        self.started: list[tuple[str, int]] = []
        self.signals: list[tuple[str, int, int]] = []
        self.stopped: list[tuple[str, int, str]] = []

    async def start(self, market_id: str, lead_id: int) -> str:
        self.started.append((market_id, lead_id))
        return f"lead-{market_id}-{lead_id}"

    async def signal_inbound(self, market_id: str, lead_id: int, message_id: int) -> str:
        self.signals.append((market_id, lead_id, message_id))
        return f"lead-{market_id}-{lead_id}"

    async def stop(self, market_id: str, lead_id: int, reason: str) -> None:
        self.stopped.append((market_id, lead_id, reason))
