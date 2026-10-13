from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from leasing.db.base import Base, MarketScoped
from leasing.embeddings import EMBEDDING_DIM


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), **kw)


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    timezone: Mapped[str] = mapped_column(String(64))
    # No automated follow-ups between quiet_start and quiet_end, local time (24h clock).
    quiet_start_hour: Mapped[int] = mapped_column(Integer, default=20)
    quiet_end_hour: Mapped[int] = mapped_column(Integer, default=9)


class Property(MarketScoped, Base):
    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(200))
    submarket: Mapped[str] = mapped_column(String(80))
    tour_start_hour: Mapped[int] = mapped_column(Integer, default=10)
    tour_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    tour_slot_minutes: Mapped[int] = mapped_column(Integer, default=30)

    units: Mapped[list["Unit"]] = relationship(back_populates="property")


class Unit(MarketScoped, Base):
    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("property_id", "unit_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    unit_number: Mapped[str] = mapped_column(String(16))
    bedrooms: Mapped[int] = mapped_column(Integer)
    bathrooms: Mapped[Decimal] = mapped_column(Numeric(3, 1))
    sqft: Mapped[int] = mapped_column(Integer)
    rent: Mapped[int] = mapped_column(Integer)  # whole dollars per month
    available_on: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default="available")

    property: Mapped[Property] = relationship(back_populates="units")


class Policy(MarketScoped, Base):
    """Structured, market-specific facts the agent may quote (fees, deposits, rules)."""

    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("market_id", "topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    topic: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    amount_unit: Mapped[str | None] = mapped_column(String(40))


class PolicyChunk(MarketScoped, Base):
    """Longer policy text for retrieval; always searched inside one market."""

    __tablename__ = "policy_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM))


class Lead(MarketScoped, Base):
    __tablename__ = "leads"
    __table_args__ = (
        Index(
            "uq_leads_market_phone",
            "market_id",
            "phone",
            unique=True,
            postgresql_where=text("phone IS NOT NULL"),
        ),
        Index(
            "uq_leads_market_email",
            "market_id",
            "email",
            unique=True,
            postgresql_where=text("email IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int | None] = mapped_column(ForeignKey("properties.id"))
    first_name: Mapped[str] = mapped_column(String(80), default="")
    last_name: Mapped[str] = mapped_column(String(80), default="")
    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(20))  # E.164
    source: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="new")
    crm_id: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = _ts()

    property: Mapped[Property | None] = relationship()
    inquiries: Mapped[list["Inquiry"]] = relationship(back_populates="lead")


class Inquiry(MarketScoped, Base):
    """One inbound inquiry. Repeat inquiries from the same person attach to one lead."""

    __tablename__ = "inquiries"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    source: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    received_at: Mapped[datetime] = _ts()

    lead: Mapped[Lead] = relationship(back_populates="inquiries")


class Consent(MarketScoped, Base):
    """Per-channel contact consent, with the evidence of where it came from."""

    __tablename__ = "consents"
    __table_args__ = (UniqueConstraint("lead_id", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    channel: Mapped[str] = mapped_column(String(10))  # "sms" | "email"
    granted: Mapped[bool] = mapped_column(default=False)
    evidence: Mapped[str] = mapped_column(Text, default="")
    captured_at: Mapped[datetime] = _ts()
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(MarketScoped, Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True)
    channel: Mapped[str] = mapped_column(String(10))
    owner: Mapped[str] = mapped_column(String(10), default="agent")  # "agent" | "human"
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    workflow_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = _ts()

    lead: Mapped[Lead] = relationship()
    entries: Mapped[list["ConversationEntry"]] = relationship(
        order_by="ConversationEntry.seq", back_populates="conversation"
    )
    messages: Mapped[list["Message"]] = relationship(
        order_by="Message.id", back_populates="conversation"
    )


class ConversationEntry(MarketScoped, Base):
    """The model-facing transcript, stored exactly as sent to and returned by the API.

    Rows are only ever inserted. Assistant turns keep their thinking blocks unchanged, which
    the API requires when a conversation continues on the same model.
    """

    __tablename__ = "conversation_entries"
    __table_args__ = (UniqueConstraint("conversation_id", "seq"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(10))
    content: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _ts()

    conversation: Mapped[Conversation] = relationship(back_populates="entries")


class Message(MarketScoped, Base):
    """What the prospect actually sees or sends, one row per SMS or email."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(String(10))  # "inbound" | "outbound"
    channel: Mapped[str] = mapped_column(String(10))
    author: Mapped[str] = mapped_column(String(10))  # "lead" | "agent" | "human"
    body: Mapped[str] = mapped_column(Text)
    # drafted -> sending -> sent | failed | unknown ; inbound rows are "received"
    status: Mapped[str] = mapped_column(String(10))
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    provider_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Tour(MarketScoped, Base):
    __tablename__ = "tours"
    __table_args__ = (UniqueConstraint("property_id", "starts_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(12), default="scheduled")
    created_at: Mapped[datetime] = _ts()

    property: Mapped[Property] = relationship()


class Comp(MarketScoped, Base):
    """A comparable rental listing in the same submarket."""

    __tablename__ = "comps"

    id: Mapped[int] = mapped_column(primary_key=True)
    submarket: Mapped[str] = mapped_column(String(80), index=True)
    bedrooms: Mapped[int] = mapped_column(Integer)
    rent: Mapped[int] = mapped_column(Integer)
    sqft: Mapped[int] = mapped_column(Integer)
    listed_on: Mapped[date] = mapped_column(Date)
    leased_on: Mapped[date | None] = mapped_column(Date)
    concession_weeks: Mapped[Decimal] = mapped_column(Numeric(4, 1), default=0)


class Brief(MarketScoped, Base):
    __tablename__ = "briefs"
    __table_args__ = (UniqueConstraint("property_id", "week_of"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    property_id: Mapped[int] = mapped_column(ForeignKey("properties.id"))
    week_of: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10))  # "approved" | "rejected"
    body: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    problems: Mapped[list[str]] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = _ts()

    property: Mapped[Property] = relationship()


class CrmEvent(MarketScoped, Base):
    """Transactional outbox for CRM write-back.

    Written in the same transaction as the change it describes, then delivered by the worker
    with retries, so a CRM outage never blocks or loses a conversation.
    """

    __tablename__ = "crm_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    kind: Mapped[str] = mapped_column(String(20))  # "upsert_contact" | "log_message" | "log_tour"
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts()
