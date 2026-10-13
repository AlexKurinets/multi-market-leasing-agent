from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from leasing.agent import LeasingAgent
from leasing.channels.base import ConsoleSender, ProviderRejected, SendReceipt
from leasing.channels.delivery import Delivery, deliver, draft_outbound
from leasing.compliance import revoke_consent
from leasing.conversations import respond
from leasing.db.models import Conversation, CrmEvent, Lead, Message
from leasing.db.session import market_session
from leasing.ingestion import NewLead, ingest_lead
from leasing.workflows.types import RespondRequest

from .conftest import NOW
from .fakes import FakeAnthropic, call, reply


@pytest.fixture
def senders():
    return {"sms": ConsoleSender("sms"), "email": ConsoleSender("email")}


def _new_lead(factory, market="DAL", message="Do you have 2 bedrooms?"):
    with market_session(market, factory) as session:
        return ingest_lead(
            session,
            NewLead(
                source="website",
                first_name="Sam",
                phone="214.555.0199",
                message=message,
                sms_consent=True,
            ),
            NOW,
        )


def _request(lead_id, trigger="new_lead", key="wf/run/turn-1", **kw):
    return RespondRequest("DAL", lead_id, trigger, key, **kw)


def test_new_lead_reply_is_sent_and_logged_to_crm(factory, settings, senders):
    lead = _new_lead(factory)
    client = FakeAnthropic(reply("Hi Sam! Yes, we have 2 bedrooms. Want to tour?"))
    result = respond(
        _request(lead.lead_id),
        agent=LeasingAgent(client, settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )

    assert result.status == "sent"
    assert senders["sms"].sent == [
        ("+12145550199", "Hi Sam! Yes, we have 2 bedrooms. Want to tour?")
    ]
    assert "Do you have 2 bedrooms?" in client.requests[0]["messages"][0]["content"]
    with market_session("DAL", factory) as session:
        kinds = sorted(session.scalars(select(CrmEvent.kind)))
        sent = session.get(Message, result.message_id)
    assert kinds == ["log_message", "log_message", "upsert_contact"]  # inquiry + reply
    assert sent.status == "sent" and sent.provider_id.startswith("console-sms")


def test_retried_turn_does_not_call_the_model_or_send_again(factory, settings, senders):
    lead = _new_lead(factory)
    client = FakeAnthropic(reply("Hi Sam!"))  # only one scripted response
    agent = LeasingAgent(client, settings)
    first = respond(_request(lead.lead_id), agent=agent, senders=senders, factory=factory, now=NOW)
    again = respond(_request(lead.lead_id), agent=agent, senders=senders, factory=factory, now=NOW)

    assert first.status == again.status == "sent"
    assert again.message_id == first.message_id
    assert len(client.requests) == 1
    assert len(senders["sms"].sent) == 1


def test_follow_up_waits_out_quiet_hours(factory, settings, senders):
    lead = _new_lead(factory)
    late = datetime(2026, 10, 13, 3, 0, tzinfo=UTC)  # 22:00 in Dallas
    result = respond(
        _request(lead.lead_id, "follow_up", follow_up_number=1, follow_up_total=3),
        agent=LeasingAgent(FakeAnthropic(), settings),
        senders=senders,
        factory=factory,
        now=late,
    )
    assert result.status == "deferred"
    assert result.retry_at == "2026-10-13T14:00:00+00:00"  # 09:00 CDT
    assert senders["sms"].sent == []


def test_follow_up_event_reaches_the_model(factory, settings, senders):
    lead = _new_lead(factory)
    client = FakeAnthropic(reply("Still looking for a 2 bedroom, Sam?"))
    respond(
        _request(lead.lead_id, "follow_up", follow_up_number=2, follow_up_total=3),
        agent=LeasingAgent(client, settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )
    event = client.requests[0]["messages"][-1]["content"]
    assert event.startswith('<event type="no_reply_follow_up" number="2" of="3"')


def test_opted_out_lead_is_skipped(factory, settings, senders):
    lead = _new_lead(factory)
    with market_session("DAL", factory) as session:
        revoke_consent(session, session.get(Lead, lead.lead_id), "sms", "STOP reply", NOW)
    result = respond(
        _request(lead.lead_id),
        agent=LeasingAgent(FakeAnthropic(), settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )
    assert (result.status, result.detail) == ("skipped", "no_consent")


def test_human_owned_conversation_is_skipped(factory, settings, senders):
    lead = _new_lead(factory)
    with market_session("DAL", factory) as session:
        session.get(Conversation, lead.conversation_id).owner = "human"
    result = respond(
        _request(lead.lead_id),
        agent=LeasingAgent(FakeAnthropic(), settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )
    assert (result.status, result.detail) == ("skipped", "human_owned")


def test_accommodation_request_gets_fixed_reply_without_the_model(factory, settings, senders):
    lead = _new_lead(factory, message="Do you have wheelchair accessible units?")
    client = FakeAnthropic()
    result = respond(
        _request(lead.lead_id),
        agent=LeasingAgent(client, settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )
    assert client.requests == []
    assert result.handed_off and result.status == "sent"
    assert "leasing team will reach out personally" in senders["sms"].sent[0][1]
    with market_session("DAL", factory) as session:
        conversation = session.get(Conversation, lead.conversation_id)
        assert conversation.owner == "human"
        assert conversation.handoff_reason == "guardrail_accommodation"


def test_tour_booking_is_reported_to_the_workflow(factory, settings, senders, property_ids):
    lead = _new_lead(factory)
    cedar = property_ids["Cedar Row Residences"]

    def book(params):
        import json

        slots = json.loads(params["messages"][-1]["content"][0]["content"])["slots"]
        return call("book_tour", {"property_id": cedar, "starts_at": slots[0]})

    client = FakeAnthropic(
        call("check_tour_slots", {"property_id": cedar, "day": "2026-10-13"}),
        book,
        reply("Booked!"),
    )
    result = respond(
        _request(lead.lead_id),
        agent=LeasingAgent(client, settings),
        senders=senders,
        factory=factory,
        now=NOW,
    )
    assert result.tour_booked and result.status == "sent"


class _Rejecting:
    channel = "sms"

    def send(self, to, body):
        raise ProviderRejected("21610: unsubscribed recipient")


class _TimingOut:
    channel = "sms"

    def __init__(self):
        self.calls = 0

    def send(self, to, body):
        self.calls += 1
        raise TimeoutError("read timed out")


class _Counting:
    channel = "sms"

    def __init__(self):
        self.calls = 0

    def send(self, to, body):
        self.calls += 1
        return SendReceipt("SM123")


def _draft(factory, key="k-1"):
    lead = _new_lead(factory)
    with market_session("DAL", factory) as session:
        conversation = session.get(Conversation, lead.conversation_id)
        return draft_outbound(session, conversation, "Hello", "agent", key).id


def test_delivery_success_and_duplicate(factory):
    message_id = _draft(factory)
    sender = _Counting()
    assert deliver(factory, "DAL", message_id, {"sms": sender}) == Delivery.SENT
    assert deliver(factory, "DAL", message_id, {"sms": sender}) == Delivery.ALREADY_SENT
    assert sender.calls == 1


def test_provider_rejection_is_marked_failed(factory):
    message_id = _draft(factory)
    assert deliver(factory, "DAL", message_id, {"sms": _Rejecting()}) == Delivery.FAILED
    with market_session("DAL", factory) as session:
        message = session.get(Message, message_id)
        assert (message.status, message.error) == ("failed", "21610: unsubscribed recipient")


def test_ambiguous_failure_is_never_retried(factory):
    message_id = _draft(factory)
    sender = _TimingOut()
    assert deliver(factory, "DAL", message_id, {"sms": sender}) == Delivery.UNKNOWN
    assert deliver(factory, "DAL", message_id, {"sms": sender}) == Delivery.UNKNOWN
    assert sender.calls == 1


def test_crash_between_sending_and_recording_is_not_resent(factory):
    message_id = _draft(factory)
    with market_session("DAL", factory) as session:
        session.get(Message, message_id).status = "sending"  # worker died mid-send
    sender = _Counting()
    assert deliver(factory, "DAL", message_id, {"sms": sender}) == Delivery.UNKNOWN
    assert sender.calls == 0
