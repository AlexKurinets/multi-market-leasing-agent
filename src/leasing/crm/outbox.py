from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from leasing.crm.base import ContactData, CrmAdapter, CrmError
from leasing.db.models import CrmEvent, Lead, Market, Message, Tour
from leasing.db.scoping import MarketSession
from leasing.db.session import admin_session

MAX_ATTEMPTS = 8


def enqueue(
    session: MarketSession, lead: Lead, kind: str, payload: dict[str, Any] | None = None
) -> CrmEvent:
    """Record a CRM change in the caller's transaction; the worker delivers it later."""
    event = CrmEvent(lead_id=lead.id, kind=kind, payload=payload or {})
    session.add(event)
    return event


@dataclass
class DrainStats:
    delivered: int = 0
    retried: int = 0
    failed: int = 0


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * 2 ** (attempts - 1), 3600))


def _contact(session: MarketSession, lead: Lead) -> ContactData:
    market = session.get(Market, lead.market_id)
    return ContactData(
        first_name=lead.first_name,
        last_name=lead.last_name,
        email=lead.email,
        phone=lead.phone,
        market=market.name if market else lead.market_id,
        property_name=lead.property.name if lead.property else None,
        source=lead.source,
        lead_status=lead.status,
    )


def _note_for(session: MarketSession, event: CrmEvent) -> str | None:
    if event.kind == "log_message":
        message = session.get(Message, event.payload["message_id"])
        if message is None:
            return None
        label = "Prospect" if message.direction == "inbound" else f"Leasing ({message.author})"
        return f"{label} via {message.channel}: {message.body}"
    if event.kind == "log_tour":
        tour = session.get(Tour, event.payload["tour_id"])
        if tour is None:
            return None
        return f"Tour booked at {tour.property.name} for {tour.starts_at.isoformat()}"
    return None


def drain(
    factory: sessionmaker[MarketSession] | None,
    adapter: CrmAdapter,
    now: datetime | None = None,
    batch: int = 50,
) -> DrainStats:
    """Deliver due events, oldest first. At-least-once: an upsert is idempotent by design, and
    a note may repeat only if the worker dies between the CRM call and the commit."""
    now = now or datetime.now(UTC)
    stats = DrainStats()
    with admin_session(factory) as session:
        events = session.scalars(
            select(CrmEvent)
            .where(CrmEvent.status == "pending", CrmEvent.next_attempt_at <= now)
            .order_by(CrmEvent.id)
            .limit(batch)
            .with_for_update(skip_locked=True)
        ).all()
        for event in events:
            lead = session.get(Lead, event.lead_id)
            try:
                if event.kind == "upsert_contact" or lead.crm_id is None:
                    lead.crm_id = adapter.upsert_contact(_contact(session, lead), lead.crm_id)
                note = _note_for(session, event)
                if note:
                    adapter.log_note(lead.crm_id, note, event.created_at)
            except CrmError as exc:
                event.attempts += 1
                event.last_error = str(exc)
                if exc.retryable and event.attempts < MAX_ATTEMPTS:
                    event.next_attempt_at = now + _backoff(event.attempts)
                    stats.retried += 1
                else:
                    event.status = "failed"
                    stats.failed += 1
                continue
            event.status = "done"
            event.attempts += 1
            stats.delivered += 1
    return stats
