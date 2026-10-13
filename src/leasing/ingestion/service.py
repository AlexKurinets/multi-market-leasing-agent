from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select

from leasing.compliance import record_consent
from leasing.crm.outbox import enqueue
from leasing.db.models import Conversation, Inquiry, Lead, Message
from leasing.db.scoping import MarketSession
from leasing.ingestion.normalize import normalize_email, normalize_phone


@dataclass(frozen=True)
class NewLead:
    source: str
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    property_id: int | None = None
    message: str = ""
    sms_consent: bool = False
    consent_evidence: str = ""


@dataclass(frozen=True)
class IngestResult:
    lead_id: int
    conversation_id: int
    inbound_message_id: int
    is_new: bool


class NotContactable(ValueError):
    """The inquiry has neither a valid phone with SMS consent nor a valid email."""


def find_existing_lead(session: MarketSession, phone: str | None, email: str | None) -> Lead | None:
    """Dedupe within the session's market. The same person in two cities is two leads."""
    conditions = []
    if phone:
        conditions.append(Lead.phone == phone)
    if email:
        conditions.append(Lead.email == email)
    if not conditions:
        return None
    return session.scalars(select(Lead).where(or_(*conditions)).order_by(Lead.id)).first()


def ingest_lead(session: MarketSession, data: NewLead, now: datetime) -> IngestResult:
    phone = normalize_phone(data.phone)
    email = normalize_email(data.email)
    if not phone and not email:
        raise NotContactable("Inquiry has no valid phone number or email address.")

    lead = find_existing_lead(session, phone, email)
    is_new = lead is None
    if lead is None:
        lead = Lead(
            first_name=data.first_name,
            last_name=data.last_name,
            email=email,
            phone=phone,
            property_id=data.property_id,
            source=data.source,
        )
        session.add(lead)
        session.flush()
    else:
        # Fill gaps only; never overwrite what a person already confirmed.
        lead.email = lead.email or email
        lead.phone = lead.phone or phone
        lead.first_name = lead.first_name or data.first_name
        lead.last_name = lead.last_name or data.last_name
        lead.property_id = lead.property_id or data.property_id

    session.add(Inquiry(lead_id=lead.id, source=data.source, message=data.message, raw={}))

    if phone and data.sms_consent:
        record_consent(session, lead, "sms", data.consent_evidence or f"{data.source} form", now)
    if email:
        record_consent(session, lead, "email", f"reply to inbound inquiry via {data.source}", now)

    conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead.id))
    if conversation is None:
        channel = "sms" if phone and data.sms_consent else "email" if email else None
        if channel is None:
            raise NotContactable("Phone given without SMS consent and no email address.")
        conversation = Conversation(lead_id=lead.id, channel=channel)
        session.add(conversation)
        session.flush()

    inbound = Message(
        conversation_id=conversation.id,
        direction="inbound",
        channel="inquiry",
        author="lead",
        body=data.message,
        status="received",
    )
    session.add(inbound)
    session.flush()

    enqueue(session, lead, "upsert_contact")
    if data.message:
        enqueue(session, lead, "log_message", {"message_id": inbound.id})

    return IngestResult(lead.id, conversation.id, inbound.id, is_new)
