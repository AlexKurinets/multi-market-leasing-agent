import json

import pytest
from sqlalchemy import select

from leasing.agent import HOLDING_REPLY, LeasingAgent
from leasing.agent.prompts import SYSTEM_PROMPT
from leasing.db.models import Conversation, ConversationEntry, CrmEvent, Tour
from leasing.db.session import market_session
from leasing.ingestion import NewLead, ingest_lead
from leasing.llm import FALLBACK_BETA

from .conftest import NOW
from .fakes import FakeAnthropic, call, message, reply, text


@pytest.fixture
def phx_conversation(factory, property_ids):
    with market_session("PHX", factory) as session:
        result = ingest_lead(
            session,
            NewLead(
                source="website",
                first_name="Jordan",
                phone="(602) 555-0147",
                property_id=property_ids["Saguaro Flats"],
                message="Hi, what's the pet deposit?",
                sms_consent=True,
            ),
            NOW,
        )
    return result.conversation_id


def _turn(factory, client, settings, conversation_id, event="<event/>"):
    agent = LeasingAgent(client, settings)
    with market_session("PHX", factory) as session:
        conversation = session.get(Conversation, conversation_id)
        result = agent.run_turn(session, conversation, event, NOW)
        owner = conversation.owner
    return result, owner


def test_request_shape(factory, settings, phx_conversation):
    client = FakeAnthropic(reply("Hi Jordan!"))
    _turn(factory, client, settings, phx_conversation)
    request = client.requests[0]
    assert request["model"] == "claude-opus-5-5"
    assert request["fallbacks"] == "default" and request["betas"] == [FALLBACK_BETA]
    assert request["output_config"] == {"effort": "medium"}
    assert request["system"] == [{"type": "text", "text": SYSTEM_PROMPT}]
    assert all(tool["strict"] for tool in request["tools"])
    assert "tool_choice" not in request  # forced tool use is rejected on this model


def test_tools_only_return_the_conversations_market(factory, settings, phx_conversation):
    client = FakeAnthropic(
        call(
            "search_units",
            {"property_id": None, "bedrooms": 1, "max_rent": None, "available_by": None},
        ),
        call("get_policy", {"topic": "pets"}),
        reply("The pet deposit is $350 per pet, plus $35 a month in pet rent."),
    )
    result, owner = _turn(factory, client, settings, phx_conversation)

    units = json.loads(client.tool_results(1)[0]["content"])["units"]
    assert units and {u["property"] for u in units} <= {"Saguaro Flats", "Rio Salado Commons"}
    assert "$350" in client.tool_results(2)[0]["content"]
    assert result.reply.startswith("The pet deposit is $350")
    assert result.tool_calls == ["search_units", "get_policy"]
    assert owner == "agent"


def test_other_markets_property_ids_are_not_found(
    factory, settings, phx_conversation, property_ids
):
    client = FakeAnthropic(
        call(
            "check_tour_slots",
            {"property_id": property_ids["Cedar Row Residences"], "day": "2026-10-13"},
        ),
        reply("Let me check on that."),
    )
    _turn(factory, client, settings, phx_conversation)
    result = client.tool_results(1)[0]
    assert result["is_error"] is True
    assert "no property" in result["content"]


def test_transcript_is_append_only_and_replays_thinking(factory, settings, phx_conversation):
    client = FakeAnthropic(
        call("get_policy", {"topic": "pets"}),
        reply("It's $350 per pet."),
        reply("You're welcome!"),
    )
    _turn(factory, client, settings, phx_conversation, "<event>first</event>")
    _turn(factory, client, settings, phx_conversation, "<event>second</event>")

    second_turn = client.requests[2]["messages"]
    assert [m["role"] for m in second_turn] == ["user", "assistant", "user", "assistant", "user"]
    assert second_turn[1]["content"][0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "sig-opaque",
    }
    with market_session("PHX", factory) as session:
        seqs = session.scalars(
            select(ConversationEntry.seq)
            .where(ConversationEntry.conversation_id == phx_conversation)
            .order_by(ConversationEntry.seq)
        ).all()
    assert seqs == [0, 1, 2, 3, 4, 5]


def test_reply_with_a_figure_no_tool_returned_is_withheld(factory, settings, phx_conversation):
    client = FakeAnthropic(
        call("get_policy", {"topic": "pets"}),
        reply("The pet deposit is $300."),  # the tool said $350
    )
    result, owner = _turn(factory, client, settings, phx_conversation)
    assert result.reply == HOLDING_REPLY
    assert result.withheld_reply == "The pet deposit is $300."
    assert result.handoff_reason.startswith("unsupported_figures: $300")
    assert owner == "human"


def test_figures_from_the_prospect_are_allowed(factory, settings, phx_conversation):
    client = FakeAnthropic(reply("Got it, a budget of $1,400. Let me look."))
    result, _ = _turn(
        factory,
        client,
        settings,
        phx_conversation,
        "<prospect_message>My budget is $1,400</prospect_message>",
    )
    assert result.reply == "Got it, a budget of $1,400. Let me look."


def test_steering_language_is_withheld(factory, settings, phx_conversation):
    client = FakeAnthropic(reply("Saguaro Flats is in a safe neighborhood!"))
    result, owner = _turn(factory, client, settings, phx_conversation)
    assert result.reply == HOLDING_REPLY
    assert result.handoff_reason.startswith("steering_language")
    assert owner == "human"


def test_refusal_hands_off_without_recording_the_refused_turn(factory, settings, phx_conversation):
    client = FakeAnthropic(message(stop_reason="refusal"))
    result, owner = _turn(factory, client, settings, phx_conversation)
    assert (result.reply, result.handoff_reason, owner) == (HOLDING_REPLY, "model_refusal", "human")
    with market_session("PHX", factory) as session:
        roles = session.scalars(
            select(ConversationEntry.role).where(
                ConversationEntry.conversation_id == phx_conversation
            )
        ).all()
    assert roles == ["user"]


def test_handoff_tool_lets_the_model_tell_the_prospect(factory, settings, phx_conversation):
    client = FakeAnthropic(
        call(
            "handoff_to_human",
            {"reason": "pricing_exception", "summary": "Asked to waive the admin fee."},
        ),
        reply("I've passed that to our leasing team; someone will follow up."),
    )
    result, owner = _turn(factory, client, settings, phx_conversation)
    assert result.handoff_reason == "pricing_exception"
    assert result.reply.startswith("I've passed that")
    assert owner == "human"


def test_booking_a_tour(factory, settings, phx_conversation, property_ids):
    saguaro = property_ids["Saguaro Flats"]

    def book_first_offered(params):
        slots = json.loads(params["messages"][-1]["content"][0]["content"])["slots"]
        return call("book_tour", {"property_id": saguaro, "starts_at": slots[0]})

    client = FakeAnthropic(
        call("check_tour_slots", {"property_id": saguaro, "day": "2026-10-13"}),
        book_first_offered,
        reply("You're booked for Tuesday at 10 AM."),
    )
    result, _ = _turn(factory, client, settings, phx_conversation)

    slots = json.loads(client.tool_results(1)[0]["content"])["slots"]
    assert slots[0] == "2026-10-13T10:00"
    booking = json.loads(client.tool_results(2)[0]["content"])
    assert booking["display"] == "Tuesday, October 13 at 10 AM"
    assert result.tour_booked
    with market_session("PHX", factory) as session:
        tour = session.scalar(select(Tour))
        kinds = session.scalars(select(CrmEvent.kind)).all()
    assert tour.starts_at.isoformat() == "2026-10-13T17:00:00+00:00"  # 10:00 in Phoenix
    assert "log_tour" in kinds


def test_booking_a_time_that_was_not_offered_fails(
    factory, settings, phx_conversation, property_ids
):
    client = FakeAnthropic(
        call(
            "book_tour",
            {"property_id": property_ids["Saguaro Flats"], "starts_at": "2026-10-13T22:00"},
        ),
        reply("Let me find another time."),
    )
    result, _ = _turn(factory, client, settings, phx_conversation)
    assert client.tool_results(1)[0]["is_error"] is True
    assert not result.tour_booked


def test_invalid_tool_input_is_returned_as_an_error(factory, settings, phx_conversation):
    client = FakeAnthropic(
        call("get_policy", {"topic": "smoking"}),
        reply("Let me check with the team."),
    )
    _turn(factory, client, settings, phx_conversation)
    result = client.tool_results(1)[0]
    assert result["is_error"] is True and "Invalid input" in result["content"]


def test_tool_loop_limit_hands_off(factory, settings, phx_conversation):
    settings.agent_max_iterations = 2
    client = FakeAnthropic(
        call("get_policy", {"topic": "pets"}),
        call("get_policy", {"topic": "parking"}),
    )
    result, owner = _turn(factory, client, settings, phx_conversation)
    assert (result.reply, result.handoff_reason, owner) == (
        HOLDING_REPLY,
        "tool_loop_limit",
        "human",
    )


def test_text_only_blocks_are_joined(factory, settings, phx_conversation):
    client = FakeAnthropic(message(text("Hi "), text("Jordan!")))
    result, _ = _turn(factory, client, settings, phx_conversation)
    assert result.reply == "Hi Jordan!"
