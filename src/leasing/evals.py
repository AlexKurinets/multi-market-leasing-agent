"""Regression evals for the conversation agent.

Each case replays prospect messages against the seeded demo data with the real model, then
checks behaviour: which tools were used, whether it handed off, and which figures the replies
contain or must never contain (another market's numbers are the usual "must not"). Run it before
shipping a prompt, tool or model change; see ``evals/run_evals.py``.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from leasing.agent import LeasingAgent, TurnResult
from leasing.channels import ConsoleSender
from leasing.conversations import respond
from leasing.db.models import Message, Property
from leasing.db.scoping import MarketSession
from leasing.db.session import market_session
from leasing.ingestion import NewLead, ingest_lead
from leasing.workflows.types import RespondRequest


@dataclass(frozen=True)
class EvalCase:
    id: str
    market: str
    messages: list[str]
    expect: dict[str, Any]
    property: str | None = None


@dataclass
class CaseOutcome:
    id: str
    replies: list[str]
    tool_calls: list[str]
    handed_off: bool
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def load_cases(path: Path) -> list[EvalCase]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [EvalCase(**json.loads(line)) for line in lines if line.strip()]


def score(case: EvalCase, replies: list[str], tool_calls: list[str], handed_off: bool) -> list[str]:
    expect = case.expect
    text = "\n".join(replies).lower()
    failures = []
    if "handoff" in expect and handed_off != expect["handoff"]:
        failures.append(f"handoff={handed_off}, expected {expect['handoff']}")
    if expect.get("tools_any") and not set(expect["tools_any"]) & set(tool_calls):
        failures.append(f"none of {expect['tools_any']} called (called {tool_calls})")
    for tool in expect.get("tools_none", []):
        if tool in tool_calls:
            failures.append(f"{tool} should not be called")
    for needle in expect.get("must_include", []):
        if needle.lower() not in text:
            failures.append(f"missing {needle!r}")
    for needle in expect.get("must_not_include", []):
        if needle.lower() in text:
            failures.append(f"contains {needle!r}")
    return failures


class RecordingAgent(LeasingAgent):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.turns: list[TurnResult] = []

    def run_turn(self, *args: Any, **kwargs: Any) -> TurnResult:
        result = super().run_turn(*args, **kwargs)
        self.turns.append(result)
        return result


def run_case(
    case: EvalCase, engine: Engine, agent_factory: Callable[[], "RecordingAgent"]
) -> CaseOutcome:
    """Run one case inside a transaction that is rolled back, so the database is untouched."""
    connection = engine.connect()
    outer = connection.begin()
    try:
        factory = sessionmaker(
            bind=connection,
            class_=MarketSession,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        return _run(case, factory, agent_factory())
    finally:
        outer.rollback()
        connection.close()


def _run(
    case: EvalCase, factory: sessionmaker[MarketSession], agent: RecordingAgent
) -> CaseOutcome:
    sender = ConsoleSender("sms")
    senders = {"sms": sender, "email": ConsoleSender("email")}
    now = datetime.now(UTC)
    lead_id = conversation_id = None
    handed_off = False
    for i, text in enumerate(case.messages):
        with market_session(case.market, factory) as session:
            if lead_id is None:
                property_id = (
                    session.scalar(select(Property.id).where(Property.name == case.property))
                    if case.property
                    else None
                )
                ingested = ingest_lead(
                    session,
                    NewLead(
                        source="eval",
                        first_name="Casey",
                        phone="+14805550123",
                        property_id=property_id,
                        message=text,
                        sms_consent=True,
                    ),
                    now,
                )
                lead_id, conversation_id = ingested.lead_id, ingested.conversation_id
                trigger, ids = "new_lead", []
            else:
                message = Message(
                    conversation_id=conversation_id,
                    direction="inbound",
                    channel="sms",
                    author="lead",
                    body=text,
                    status="received",
                )
                session.add(message)
                session.flush()
                trigger, ids = "inbound", [message.id]
        result = respond(
            RespondRequest(case.market, lead_id, trigger, f"eval/{case.id}/{i}", ids),
            agent=agent,
            senders=senders,
            factory=factory,
            now=now,
        )
        handed_off = handed_off or result.handed_off
        if result.handed_off or result.tour_booked or result.status != "sent":
            break

    replies = [body for _, body in sender.sent]
    tool_calls = [call for turn in agent.turns for call in turn.tool_calls]
    return CaseOutcome(
        case.id, replies, tool_calls, handed_off, score(case, replies, tool_calls, handed_off)
    )
