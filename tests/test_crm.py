import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from leasing.crm import ContactData, CrmError, InMemoryCrm
from leasing.crm.hubspot import HubSpotCrm
from leasing.crm.outbox import drain
from leasing.db.models import CrmEvent, Lead
from leasing.db.session import market_session
from leasing.ingestion import NewLead, ingest_lead

from .conftest import NOW


def _later(seconds: int = 60) -> datetime:
    # Outbox rows default next_attempt_at to the database clock, so drain "now" must be real.
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _ingest(factory):
    with market_session("ATL", factory) as session:
        return ingest_lead(
            session,
            NewLead(
                source="zillow",
                first_name="Riley",
                email="riley@example.com",
                message="Is parking included?",
            ),
            NOW,
        )


def test_drain_creates_the_contact_then_logs_notes(factory):
    lead = _ingest(factory)
    crm = InMemoryCrm()
    stats = drain(factory, crm, now=_later())
    assert stats.delivered == 2
    with market_session("ATL", factory) as session:
        crm_id = session.get(Lead, lead.lead_id).crm_id
    assert crm.contacts[crm_id].market == "Atlanta"
    assert crm.notes[crm_id][0][1] == "Prospect via inquiry: Is parking included?"


class _Flaky(InMemoryCrm):
    def __init__(self, error: CrmError):
        super().__init__()
        self.error = error

    def upsert_contact(self, contact, crm_id):
        raise self.error


def test_retryable_errors_back_off(factory):
    _ingest(factory)
    now = _later()
    stats = drain(factory, _Flaky(CrmError("503", retryable=True)), now=now)
    assert stats.retried == 2
    with market_session("ATL", factory) as session:
        event = session.scalars(select(CrmEvent).order_by(CrmEvent.id)).first()
    assert event.status == "pending" and event.attempts == 1
    assert event.next_attempt_at == now + timedelta(seconds=30)
    # Not due yet: nothing is attempted.
    assert drain(factory, InMemoryCrm(), now=now + timedelta(seconds=5)).delivered == 0
    assert drain(factory, InMemoryCrm(), now=now + timedelta(seconds=31)).delivered == 2


def test_permanent_errors_fail_the_event(factory):
    _ingest(factory)
    stats = drain(factory, _Flaky(CrmError("400", retryable=False)), now=_later())
    assert stats.failed == 2


def _hubspot(handler):
    client = httpx.Client(base_url="https://api.hubapi.com", transport=httpx.MockTransport(handler))
    return HubSpotCrm("token", client=client)


CONTACT = ContactData(
    "Riley", "Chen", "riley@example.com", None, "Atlanta", "Peachtree Terrace", "zillow", "engaged"
)


def test_hubspot_creates_a_contact_when_search_finds_none():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(201, json={"id": "901"})

    assert _hubspot(handler).upsert_contact(CONTACT, None) == "901"
    method, path, body = calls[-1]
    assert (method, path) == ("POST", "/crm/v3/objects/contacts")
    assert body["properties"]["hs_lead_status"] == "CONNECTED"
    assert body["properties"]["email"] == "riley@example.com"


def test_hubspot_updates_a_known_contact_and_attaches_notes():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"id": "901"})

    crm = _hubspot(handler)
    crm.upsert_contact(CONTACT, "901")
    crm.log_note("901", "Prospect via sms: hi", datetime(2026, 10, 12, tzinfo=UTC))
    assert calls[0][:2] == ("PATCH", "/crm/v3/objects/contacts/901")
    note = calls[1][2]
    assert note["associations"][0]["to"] == {"id": "901"}
    assert note["associations"][0]["types"][0]["associationTypeId"] == 202


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (502, True), (400, False)])
def test_hubspot_error_classification(status, retryable):
    crm = _hubspot(lambda request: httpx.Response(status, text="nope"))
    with pytest.raises(CrmError) as caught:
        crm.log_note("901", "x", datetime(2026, 10, 12, tzinfo=UTC))
    assert caught.value.retryable is retryable
