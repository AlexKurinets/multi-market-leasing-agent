from pathlib import Path

import pytest
from sqlalchemy import select

from leasing.compliance import has_consent
from leasing.db.models import Conversation, CrmEvent, Inquiry, Lead
from leasing.db.session import market_session
from leasing.embeddings import HashingEmbedder
from leasing.ingestion import NewLead, NotContactable, ingest_lead
from leasing.ingestion.parsers import parse_lead_email
from leasing.retrieval import search_policy_chunks

from .conftest import NOW

SAMPLES = Path(__file__).parent.parent / "samples"


def _lead(**overrides):
    data = dict(
        source="website",
        first_name="Jordan",
        phone="(602) 555-0147",
        email="jordan@example.com",
        message="Is the one bedroom available?",
        sms_consent=True,
        consent_evidence="Website form checkbox v3",
    )
    data.update(overrides)
    return NewLead(**data)


def test_repeat_inquiries_attach_to_one_lead(factory):
    with market_session("PHX", factory) as session:
        first = ingest_lead(session, _lead(), NOW)
        second = ingest_lead(session, _lead(phone="602.555.0147", email=None, source="zillow"), NOW)
        inquiries = session.scalars(select(Inquiry).where(Inquiry.lead_id == first.lead_id)).all()
        conversation = session.scalar(
            select(Conversation).where(Conversation.lead_id == first.lead_id)
        )
        sms_ok = has_consent(session, first.lead_id, "sms")
    assert first.is_new and not second.is_new
    assert second.lead_id == first.lead_id
    assert [i.source for i in inquiries] == ["website", "zillow"]
    assert conversation.channel == "sms"
    assert sms_ok


def test_same_person_in_two_markets_is_two_leads(factory):
    with market_session("PHX", factory) as session:
        phx = ingest_lead(session, _lead(), NOW)
    with market_session("DAL", factory) as session:
        dal = ingest_lead(session, _lead(), NOW)
        assert session.scalar(select(Lead).where(Lead.id == phx.lead_id)) is None
    assert dal.is_new and dal.lead_id != phx.lead_id


def test_email_only_lead_uses_email_channel(factory):
    with market_session("ATL", factory) as session:
        result = ingest_lead(session, _lead(phone=None, sms_consent=False), NOW)
        conversation = session.get(Conversation, result.conversation_id)
        events = session.scalars(select(CrmEvent.kind).where(CrmEvent.lead_id == result.lead_id))
        assert conversation.channel == "email"
        assert sorted(events) == ["log_message", "upsert_contact"]


def test_phone_without_consent_and_no_email_is_rejected(factory):
    with pytest.raises(NotContactable), market_session("ATL", factory) as session:
        ingest_lead(session, _lead(email=None, sms_consent=False), NOW)


def test_parse_listing_site_emails():
    zillow = parse_lead_email("zillow", (SAMPLES / "zillow_lead.txt").read_text())
    assert (zillow.first_name, zillow.last_name) == ("Jordan", "Rivera")
    assert zillow.email == "Jordan.Rivera@example.com"
    assert zillow.property_hint == "Saguaro Flats"
    assert zillow.message.startswith("Hi! Is the one bedroom still available")
    assert zillow.message.endswith("pet deposit for a small dog?")

    apts = parse_lead_email("apartments_com", (SAMPLES / "apartments_com_lead.txt").read_text())
    assert (apts.first_name, apts.phone) == ("Sam", "214.555.0199")
    assert apts.move_in == "2026-12-01"
    assert apts.message == (
        "Looking for a 2 bedroom with garage parking. Do you offer 6 month leases?"
    )


@pytest.mark.parametrize(("market", "amount"), [("PHX", "$350"), ("DAL", "$500"), ("ATL", "$400")])
def test_policy_search_never_crosses_markets(factory, market, amount):
    with market_session(market, factory) as session:
        results = search_policy_chunks(session, HashingEmbedder(), "pet deposit for a dog", k=3)
    assert results[0].title == "Pets and assistance animals"
    assert amount in results[0].body
    others = {"$350", "$500", "$400"} - {amount}
    assert not any(o in r.body for r in results for o in others)
