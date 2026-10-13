"""One conversational turn: a manual tool-use loop over the Messages API.

A manual loop rather than the SDK's tool runner because each turn persists every request and
response to Postgres as it happens, runs tools inside a market-scoped database session, and
applies post-checks before anything reaches the prospect.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from leasing.agent.prompts import SYSTEM_PROMPT
from leasing.agent.tools import ToolContext, execute_tool, tool_definitions
from leasing.config import Settings
from leasing.db.models import Conversation, ConversationEntry, Market
from leasing.db.scoping import MarketSession
from leasing.embeddings import Embedder, HashingEmbedder
from leasing.guardrails import steering_language
from leasing.llm import create_message, dump_content, text_of
from leasing.validation import unsupported_numbers

HOLDING_REPLY = "Thanks for your patience. A member of our leasing team will follow up with you."


@dataclass
class TurnResult:
    reply: str | None
    tool_calls: list[str] = field(default_factory=list)
    handoff_reason: str | None = None
    tour_booked: bool = False
    withheld_reply: str | None = None


def load_history(session: MarketSession, conversation: Conversation) -> list[dict[str, Any]]:
    entries = session.scalars(
        select(ConversationEntry)
        .where(ConversationEntry.conversation_id == conversation.id)
        .order_by(ConversationEntry.seq)
    )
    return [{"role": e.role, "content": e.content} for e in entries]


def append_entry(
    session: MarketSession,
    conversation: Conversation,
    history: list[dict[str, Any]],
    role: str,
    content: Any,
) -> None:
    """Append-only: earlier turns are never edited, which keeps replayed thinking valid."""
    session.add(
        ConversationEntry(
            conversation_id=conversation.id, seq=len(history), role=role, content=content
        )
    )
    history.append({"role": role, "content": content})


def hand_off(conversation: Conversation, reason: str) -> None:
    conversation.owner = "human"
    conversation.handoff_reason = conversation.handoff_reason or reason
    conversation.lead.status = "handed_off"


class LeasingAgent:
    def __init__(self, client: Any, settings: Settings, embedder: Embedder | None = None) -> None:
        self.client = client
        self.settings = settings
        self.embedder = embedder or HashingEmbedder()
        self.tools = tool_definitions()

    def run_turn(
        self, session: MarketSession, conversation: Conversation, event: str, now: datetime
    ) -> TurnResult:
        market = session.get(Market, conversation.market_id)
        ctx = ToolContext(session, market, conversation.lead, conversation, self.embedder, now)
        history = load_history(session, conversation)
        append_entry(session, conversation, history, "user", event)

        reply: str | None = None
        failure: str | None = None
        for _ in range(self.settings.agent_max_iterations):
            response = create_message(
                self.client,
                model=self.settings.anthropic_model,
                max_tokens=16000,
                system=[{"type": "text", "text": SYSTEM_PROMPT}],
                tools=self.tools,
                messages=history,
                output_config={"effort": self.settings.agent_effort},
                cache_control={"type": "ephemeral"},
            )
            if response.stop_reason == "refusal":
                failure = "model_refusal"
                break
            append_entry(session, conversation, history, "assistant", dump_content(response))

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    output, is_error = execute_tool(ctx, block.name, block.input)
                    result: dict[str, Any] = {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                    if is_error:
                        result["is_error"] = True
                    results.append(result)
                append_entry(session, conversation, history, "user", results)
                continue
            if response.stop_reason == "max_tokens":
                failure = "reply_truncated"
                break
            if response.stop_reason == "pause_turn":
                continue
            reply = text_of(response) or None
            break
        else:
            failure = "tool_loop_limit"

        result = TurnResult(
            reply=reply,
            tool_calls=list(ctx.calls),
            handoff_reason=ctx.handoff_reason,
            tour_booked=ctx.tour_booked is not None,
        )

        if reply:
            problem = self._check_reply(reply, history)
            if problem:
                failure = problem
                result.withheld_reply = reply
                result.reply = None

        if failure:
            hand_off(conversation, failure)
            result.handoff_reason = result.handoff_reason or failure
        if result.reply is None and result.handoff_reason:
            result.reply = HOLDING_REPLY
        return result

    @staticmethod
    def _check_reply(reply: str, history: list[dict[str, Any]]) -> str | None:
        # Every dollar amount or percentage must come from a tool result or from the prospect.
        sources = [json.dumps(turn["content"]) for turn in history if turn["role"] == "user"]
        unsupported = unsupported_numbers(reply, sources, kinds=("currency", "percent"))
        if unsupported:
            return "unsupported_figures: " + ", ".join(t.raw for t in unsupported)
        steering = steering_language(reply)
        if steering:
            return f"steering_language: {steering}"
        return None
