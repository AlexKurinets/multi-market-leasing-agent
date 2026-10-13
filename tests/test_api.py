from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from twilio.request_validator import RequestValidator

from leasing.api.app import AppDeps, create_app
from leasing.channels import ConsoleSender
from leasing.compliance import has_consent
from leasing.db.models import Conversation, Lead
from leasing.db.session import market_session

from .fakes import FakeStarter

SAMPLES = Path(__file__).parent.parent / "samples"
AUTH_TOKEN = "test-auth-token"
WEBHOOK_URL = "https://leasing.example.com/webhooks/twilio/sms"


@pytest.fixture
def starter():
    return FakeStarter()


@pytest.fixture
def sms():
    return ConsoleSender("sms")


@pytest.fixture
def api(factory, settings, starter, sms):
    settings.twilio_auth_token = AUTH_TOKEN
    settings.public_base_url = "https://leasing.example.com"
    deps = AppDeps(settings, factory, starter, {"sms": sms, "email": ConsoleSender("email")})
    with TestClient(create_app(deps)) as client:
        yield client


def _lead(api, **overrides):
    body = {
        "market_id": "PHX",
        "first_name": "Jordan",
        "phone": "(602) 555-0147",
        "message": "Any 1 bedrooms?",
        "sms_consent": True,
        "consent_evidence": "Website form checkbox v3",
    }
    body.update(overrides)
    return api.post("/leads", json=body)


def _twilio(api, body, sid="SM0001", sign=True):
    params = {"From": "+16025550147", "To": "+16025550100", "Body": body, "MessageSid": sid}
    signature = RequestValidator(AUTH_TOKEN).compute_signature(WEBHOOK_URL, params)
    headers = {"X-Twilio-Signature": signature if sign else "bad"}
    return api.post("/webhooks/twilio/sms", data=params, headers=headers)


def test_new_lead_starts_a_workflow_and_repeat_inquiry_signals_it(api, starter):
    first = _lead(api)
    assert first.status_code == 201 and first.json()["is_new"]
    second = _lead(api, phone="602.555.0147", message="Also, is parking included?")
    assert second.status_code == 201 and not second.json()["is_new"]
    lead_id = first.json()["lead_id"]
    assert starter.started == [("PHX", lead_id)]
    assert [s[:2] for s in starter.signals] == [("PHX", lead_id)]


def test_property_from_another_market_is_rejected(api, property_ids):
    response = _lead(api, property_id=property_ids["Cedar Row Residences"])
    assert response.status_code == 422


def test_listing_email_is_matched_to_its_market(api, starter):
    body = (SAMPLES / "apartments_com_lead.txt").read_text()
    response = api.post("/leads/email", json={"source": "apartments_com", "body": body})
    assert response.status_code == 201
    assert starter.started[0][0] == "DAL"


def test_twilio_webhook_rejects_bad_signatures(api):
    _lead(api)
    assert _twilio(api, "hello", sign=False).status_code == 403


def test_inbound_text_is_stored_once_and_signalled(api, starter):
    lead_id = _lead(api).json()["lead_id"]
    assert _twilio(api, "Is Saturday ok for a tour?").status_code == 200
    assert _twilio(api, "Is Saturday ok for a tour?").status_code == 200  # Twilio retry
    assert len(starter.signals) == 1 and starter.signals[0][:2] == ("PHX", lead_id)


def test_stop_revokes_consent_and_stops_the_workflow(api, starter, factory):
    lead_id = _lead(api).json()["lead_id"]
    _twilio(api, "STOP")
    assert starter.stopped == [("PHX", lead_id, "opted_out")]
    assert starter.signals == []
    with market_session("PHX", factory) as session:
        assert not has_consent(session, lead_id, "sms")
        assert session.get(Lead, lead_id).status == "opted_out"


def test_takeover_then_human_reply(api, starter, sms):
    accepted = _lead(api).json()
    path = f"/markets/PHX/conversations/{accepted['conversation_id']}"
    message = {
        "body": "Hi Jordan, this is Maria from the leasing office.",
        "client_key": "abc12345",
    }

    assert api.post(f"{path}/messages", json=message).status_code == 409
    assert api.post(f"{path}/takeover").status_code == 204
    assert starter.stopped == [("PHX", accepted["lead_id"], "human_takeover")]
    sent = api.post(f"{path}/messages", json=message)
    assert sent.status_code == 201 and sent.json()["delivery"] == "sent"
    again = api.post(f"{path}/messages", json=message)  # double-click
    assert again.json()["delivery"] == "already_sent"
    assert sms.sent == [("+16025550147", message["body"])]

    detail = api.get(path).json()
    assert detail["owner"] == "human"
    assert [m["author"] for m in detail["messages"]] == ["lead", "human"]


def test_conversations_are_not_visible_from_another_market(api):
    conversation_id = _lead(api).json()["conversation_id"]
    assert api.get(f"/markets/PHX/conversations/{conversation_id}").status_code == 200
    assert api.get(f"/markets/DAL/conversations/{conversation_id}").status_code == 404
    assert api.get("/markets/DAL/conversations").json() == []


def test_market_overview_and_policy_edits(api, factory):
    _lead(api)
    markets = {m["id"]: m for m in api.get("/markets").json()}
    assert markets["PHX"]["agent_conversations"] == 1
    assert markets["DAL"]["agent_conversations"] == 0

    new = "Up to 2 pets. $375 refundable pet deposit per pet plus $35 monthly pet rent."
    assert api.put("/markets/PHX/policies/pets", json={"summary": new}).status_code == 200
    policies = {p["topic"]: p["summary"] for p in api.get("/markets/PHX/policies").json()}
    assert policies["pets"] == new
    dal = {p["topic"]: p["summary"] for p in api.get("/markets/DAL/policies").json()}
    assert "$500" in dal["pets"]
    with market_session("PHX", factory) as session:
        assert session.scalar(select(Conversation.owner)) == "agent"
