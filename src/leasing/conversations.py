"""Responding to a lead: the unit of work the follow-up workflow runs as an activity.

Safe to retry. The idempotency key identifies the workflow turn; if a reply was already drafted
for it, the agent is not called again and delivery resumes from the stored draft.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from leasing.agent.loop import LeasingAgent, append_entry, hand_off, load_history
from leasing.agent.prompts import follow_up_event, inbound_event, new_lead_event
from leasing.channels.base import Sender
from leasing.channels.delivery import Delivery, deliver, draft_outbound
from leasing.compliance import has_consent, next_send_time
from leasing.db.models import Conversation, Inquiry, Lead, Market, Message
from leasing.db.scoping import MarketSession
from leasing.db.session import market_session
from leasing.guardrails import screen_inbound
from leasing.workflows.types import RespondRequest, RespondResult


def respond(
    req: RespondRequest,
    *,
    agent: LeasingAgent,
    senders: dict[str, Sender],
    factory: sessionmaker[MarketSession] | None = None,
    now: datetime | None = None,
) -> RespondResult:
    now = now or datetime.now(UTC)
    with market_session(req.market_id, factory) as session:
        outcome = _prepare(session, req, agent, now)
    if isinstance(outcome, RespondResult):
        return outcome
    message_id, tour_booked, handed_off = outcome

    delivery = deliver(factory, req.market_id, message_id, senders)
    status = {
        Delivery.SENT: "sent",
        Delivery.ALREADY_SENT: "sent",
        Delivery.UNKNOWN: "unknown",
        Delivery.FAILED: "failed",
    }[delivery]
    if delivery in (Delivery.UNKNOWN, Delivery.FAILED):
        with market_session(req.market_id, factory) as session:
            conversation = session.scalar(
                select(Conversation).where(Conversation.lead_id == req.lead_id)
            )
            hand_off(conversation, f"delivery_{delivery.value}")
        handed_off = True
    return RespondResult(
        status=status,
        detail=delivery.value,
        tour_booked=tour_booked,
        handed_off=handed_off,
        message_id=message_id,
    )


def _prepare(
    session: MarketSession, req: RespondRequest, agent: LeasingAgent, now: datetime
) -> RespondResult | tuple[int, bool, bool]:
    lead = session.get(Lead, req.lead_id)
    conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead.id))

    drafted = session.scalar(select(Message).where(Message.idempotency_key == req.idempotency_key))
    if drafted is not None:  # retry of a turn that already produced its reply
        return drafted.id, lead.status == "tour_booked", conversation.owner == "human"

    if conversation.owner == "human":
        return RespondResult("skipped", "human_owned")
    if lead.status == "opted_out" or not has_consent(session, lead.id, conversation.channel):
        return RespondResult("skipped", "no_consent")

    market = session.get(Market, lead.market_id)
    if req.trigger == "follow_up":
        send_at = next_send_time(
            now, market.timezone, market.quiet_start_hour, market.quiet_end_hour
        )
        if send_at > now:
            return RespondResult("deferred", "quiet_hours", retry_at=send_at.isoformat())

    event, screened_text = _build_event(session, req, lead, conversation, market, now)
    if event is None:
        return RespondResult("skipped", "nothing_to_answer")

    screen = screen_inbound(screened_text) if screened_text else None
    if screen is not None:
        # Answered by a fixed reply, never by the model; still recorded so later turns have
        # the context.
        history = load_history(session, conversation)
        append_entry(session, conversation, history, "user", event)
        append_entry(session, conversation, history, "assistant", screen.reply)
        if screen.handoff:
            hand_off(conversation, f"guardrail_{screen.category}")
        reply, tour_booked, handed_off = screen.reply, False, screen.handoff
    else:
        turn = agent.run_turn(session, conversation, event, now)
        reply = turn.reply
        tour_booked = turn.tour_booked
        handed_off = turn.handoff_reason is not None
        if reply is None:
            return RespondResult("skipped", "no_reply", tour_booked=tour_booked)

    if lead.status == "new" and req.trigger == "inbound":
        lead.status = "engaged"
    message = draft_outbound(session, conversation, reply, "agent", req.idempotency_key)
    return message.id, tour_booked, handed_off


def _build_event(
    session: MarketSession,
    req: RespondRequest,
    lead: Lead,
    conversation: Conversation,
    market: Market,
    now: datetime,
) -> tuple[str | None, str | None]:
    """The <event> text for this turn, plus the prospect-written text that needs screening."""
    tz = market.timezone
    if req.trigger == "new_lead":
        inquiry = session.scalars(
            select(Inquiry).where(Inquiry.lead_id == lead.id).order_by(Inquiry.id.desc())
        ).first()
        message = inquiry.message if inquiry else ""
        event = new_lead_event(
            source=inquiry.source if inquiry else lead.source,
            channel=conversation.channel,
            market=market.name,
            property_name=lead.property.name if lead.property else None,
            property_id=lead.property_id,
            first_name=lead.first_name,
            message=message,
            now=now,
            tz=tz,
        )
        return event, message
    if req.trigger == "inbound":
        messages = session.scalars(
            select(Message)
            .where(Message.id.in_(req.message_ids), Message.direction == "inbound")
            .order_by(Message.id)
        ).all()
        bodies = [m.body for m in messages if m.body.strip()]
        if not bodies:
            return None, None
        return inbound_event(bodies, now, tz), "\n".join(bodies)
    return follow_up_event(req.follow_up_number, req.follow_up_total, now, tz), None
