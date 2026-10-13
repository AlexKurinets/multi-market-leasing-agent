"""Draft-then-send delivery with an idempotency key.

A reply is written to ``messages`` as ``drafted`` before any provider call, keyed by the
workflow turn that produced it. Delivery moves it to ``sending`` and commits *before* calling
the provider, so a retried activity can tell "never attempted" from "attempted, outcome
unknown". Texts are delivered at most once: a missed follow-up is recoverable, a duplicate text
to a prospect is not. Unknown outcomes are surfaced to the leasing team instead of retried.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from leasing.channels.base import ProviderRejected, Sender
from leasing.crm.outbox import enqueue
from leasing.db.models import Conversation, Message
from leasing.db.scoping import MarketSession
from leasing.db.session import market_session


class Delivery(StrEnum):
    SENT = "sent"
    ALREADY_SENT = "already_sent"
    UNKNOWN = "unknown"
    FAILED = "failed"


def draft_outbound(
    session: MarketSession,
    conversation: Conversation,
    body: str,
    author: str,
    idempotency_key: str,
) -> Message:
    existing = session.scalar(select(Message).where(Message.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    message = Message(
        conversation_id=conversation.id,
        direction="outbound",
        channel=conversation.channel,
        author=author,
        body=body,
        status="drafted",
        idempotency_key=idempotency_key,
    )
    session.add(message)
    session.flush()
    return message


def deliver(
    factory: sessionmaker[MarketSession] | None,
    market_id: str,
    message_id: int,
    senders: dict[str, Sender],
) -> Delivery:
    with market_session(market_id, factory) as session:
        message = session.get(Message, message_id)
        if message.status == "sent":
            return Delivery.ALREADY_SENT
        if message.status in ("sending", "unknown"):
            # A previous attempt reached the provider call and never recorded the result.
            message.status = "unknown"
            message.error = "Previous delivery attempt has no recorded outcome; not resending."
            return Delivery.UNKNOWN
        lead = message.conversation.lead
        to = lead.phone if message.channel == "sms" else lead.email
        message.status = "sending"

    sender = senders[message.channel]
    try:
        receipt = sender.send(to, message.body)
    except ProviderRejected as exc:
        _finish(factory, market_id, message_id, "failed", error=str(exc))
        return Delivery.FAILED
    except Exception as exc:  # timeout, connection reset: the provider may have sent it
        _finish(factory, market_id, message_id, "unknown", error=repr(exc))
        return Delivery.UNKNOWN
    _finish(factory, market_id, message_id, "sent", provider_id=receipt.provider_id)
    return Delivery.SENT


def _finish(
    factory: sessionmaker[MarketSession] | None,
    market_id: str,
    message_id: int,
    status: str,
    provider_id: str | None = None,
    error: str | None = None,
) -> None:
    with market_session(market_id, factory) as session:
        message = session.get(Message, message_id)
        message.status = status
        message.provider_id = provider_id
        message.error = error
        if status == "sent":
            message.sent_at = datetime.now(UTC)
            enqueue(session, message.conversation.lead, "log_message", {"message_id": message.id})
