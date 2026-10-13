from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool
from twilio.request_validator import RequestValidator

from leasing.api import schemas
from leasing.channels import Sender, build_senders
from leasing.channels.delivery import deliver, draft_outbound
from leasing.compliance import OPT_IN_KEYWORDS, OPT_OUT_KEYWORDS, record_consent, revoke_consent
from leasing.config import Settings, get_settings
from leasing.crm.outbox import enqueue
from leasing.db.models import (
    Brief,
    Conversation,
    Lead,
    Market,
    Message,
    Policy,
    Property,
    Tour,
)
from leasing.db.scoping import MarketSession
from leasing.db.session import admin_session, get_session_factory, market_session
from leasing.ingestion import IngestResult, NewLead, NotContactable, ingest_lead
from leasing.ingestion.normalize import normalize_phone
from leasing.ingestion.parsers import parse_lead_email
from leasing.workflows.client import ConversationStarter, TemporalStarter, connect

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@dataclass
class AppDeps:
    settings: Settings
    factory: sessionmaker[MarketSession]
    starter: ConversationStarter
    senders: dict[str, Sender]


def get_deps(request: Request) -> AppDeps:
    return request.app.state.deps


def create_app(deps: AppDeps | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is None:
            settings = get_settings()
            app.state.deps = AppDeps(
                settings=settings,
                factory=get_session_factory(),
                starter=TemporalStarter(await connect(settings), settings),
                senders=build_senders(settings),
            )
        else:
            app.state.deps = deps
        yield

    app = FastAPI(title="Multi-market leasing agent", lifespan=lifespan)
    origin = (deps.settings if deps else get_settings()).dashboard_origin
    app.add_middleware(
        CORSMiddleware, allow_origins=[origin], allow_methods=["*"], allow_headers=["*"]
    )
    _register_routes(app)
    return app


def _now() -> datetime:
    return datetime.now(UTC)


def _name(lead: Lead) -> str:
    return f"{lead.first_name} {lead.last_name}".strip() or "Unknown"


# --- lead intake -----------------------------------------------------------------------------


def _ingest(deps: AppDeps, market_id: str, data: NewLead) -> IngestResult:
    with market_session(market_id, deps.factory) as session:
        if session.get(Market, market_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown market {market_id}")
        if data.property_id is not None and session.get(Property, data.property_id) is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Property {data.property_id} is not in market {market_id}",
            )
        try:
            return ingest_lead(session, data, _now())
        except NotContactable as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


async def _accept(deps: AppDeps, market_id: str, data: NewLead) -> schemas.LeadAccepted:
    result = await run_in_threadpool(_ingest, deps, market_id, data)
    if result.is_new:
        wid = await deps.starter.start(market_id, result.lead_id)
    else:
        wid = await deps.starter.signal_inbound(
            market_id, result.lead_id, result.inbound_message_id
        )

    def remember_workflow() -> None:
        with market_session(market_id, deps.factory) as session:
            session.get(Conversation, result.conversation_id).workflow_id = wid

    await run_in_threadpool(remember_workflow)
    return schemas.LeadAccepted(
        lead_id=result.lead_id,
        conversation_id=result.conversation_id,
        is_new=result.is_new,
        workflow_id=wid,
    )


def _resolve_market(deps: AppDeps, property_hint: str | None) -> tuple[str, int] | None:
    if not property_hint:
        return None
    with admin_session(deps.factory) as session:
        row = session.execute(
            select(Property.market_id, Property.id).where(
                func.lower(Property.name) == property_hint.strip().lower()
            )
        ).first()
    return (row[0], row[1]) if row else None


# --- inbound SMS -----------------------------------------------------------------------------


def _record_inbound_sms(
    deps: AppDeps, phone: str, body: str, sid: str
) -> tuple[str, int, str, int | None] | None:
    """Store an inbound text. Returns (market_id, lead_id, action, message_id) or None."""
    with admin_session(deps.factory) as session:
        # A number can belong to leads in several markets; the latest conversation wins.
        row = session.execute(
            select(Lead.market_id, Lead.id)
            .join(Conversation, Conversation.lead_id == Lead.id)
            .where(Lead.phone == phone)
            .order_by(Conversation.id.desc())
            .limit(1)
        ).first()
    if row is None:
        return None
    market_id, lead_id = row
    now = _now()
    with market_session(market_id, deps.factory) as session:
        key = f"twilio:{sid}"
        if session.scalar(select(Message.id).where(Message.idempotency_key == key)):
            return market_id, lead_id, "duplicate", None
        lead = session.get(Lead, lead_id)
        conversation = session.scalar(select(Conversation).where(Conversation.lead_id == lead_id))
        message = Message(
            conversation_id=conversation.id,
            direction="inbound",
            channel="sms",
            author="lead",
            body=body,
            status="received",
            provider_id=sid,
            idempotency_key=key,
        )
        session.add(message)
        session.flush()
        enqueue(session, lead, "log_message", {"message_id": message.id})

        keyword = body.strip().lower()
        if keyword in OPT_OUT_KEYWORDS:
            revoke_consent(session, lead, "sms", f"{keyword.upper()} reply ({sid})", now)
            lead.status = "opted_out"
            enqueue(session, lead, "upsert_contact")
            return market_id, lead_id, "stop", message.id
        if keyword in OPT_IN_KEYWORDS:
            record_consent(session, lead, "sms", f"{keyword.upper()} reply ({sid})", now)
            return market_id, lead_id, "none", message.id
        if conversation.owner != "agent":
            return market_id, lead_id, "none", message.id  # a person is handling it
        return market_id, lead_id, "signal", message.id


# --- dashboard -------------------------------------------------------------------------------


def _summary(session: MarketSession, conversation: Conversation) -> dict:
    lead = conversation.lead
    last = session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.id.desc())
        .limit(1)
    ).first()
    return {
        "id": conversation.id,
        "lead_id": lead.id,
        "lead_name": _name(lead),
        "contact": lead.phone or lead.email,
        "lead_status": lead.status,
        "owner": conversation.owner,
        "channel": conversation.channel,
        "handoff_reason": conversation.handoff_reason,
        "last_message": last.body if last else None,
        "last_message_at": last.created_at if last else None,
    }


def _conversation_or_404(session: MarketSession, conversation_id: int) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:  # also hides other markets' conversations
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/leads", status_code=201, response_model=schemas.LeadAccepted)
    async def create_lead(body: schemas.LeadIn, deps: AppDeps = Depends(get_deps)):
        data = NewLead(**body.model_dump(exclude={"market_id"}))
        return await _accept(deps, body.market_id, data)

    @app.post("/leads/email", status_code=201, response_model=schemas.LeadAccepted)
    async def create_lead_from_email(body: schemas.LeadEmailIn, deps: AppDeps = Depends(get_deps)):
        parsed = parse_lead_email(body.source, body.body)
        resolved = await run_in_threadpool(_resolve_market, deps, parsed.property_hint)
        if body.market_id is None and resolved is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Could not match the listing to a property; pass market_id explicitly.",
            )
        market_id = body.market_id or resolved[0]
        property_id = resolved[1] if resolved and resolved[0] == market_id else None
        data = NewLead(
            source=body.source,
            first_name=parsed.first_name,
            last_name=parsed.last_name,
            email=parsed.email,
            phone=parsed.phone,
            property_id=property_id,
            message=parsed.message,
            sms_consent=False,  # listing-site emails do not carry SMS consent
        )
        return await _accept(deps, market_id, data)

    @app.post("/webhooks/twilio/sms")
    async def twilio_inbound(request: Request, deps: AppDeps = Depends(get_deps)) -> Response:
        params = {k: str(v) for k, v in (await request.form()).items()}
        if deps.settings.twilio_validate_signatures:
            validator = RequestValidator(deps.settings.twilio_auth_token)
            url = deps.settings.public_base_url.rstrip("/") + request.url.path
            if not validator.validate(url, params, request.headers.get("X-Twilio-Signature", "")):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid Twilio signature")
        phone = normalize_phone(params.get("From"))
        recorded = None
        if phone and params.get("MessageSid"):
            recorded = await run_in_threadpool(
                _record_inbound_sms, deps, phone, params.get("Body", ""), params["MessageSid"]
            )
        if recorded:
            market_id, lead_id, action, message_id = recorded
            if action == "stop":
                await deps.starter.stop(market_id, lead_id, "opted_out")
            elif action == "signal" and message_id is not None:
                await deps.starter.signal_inbound(market_id, lead_id, message_id)
        return Response(EMPTY_TWIML, media_type="application/xml")

    @app.get("/markets", response_model=list[schemas.MarketOut])
    def list_markets(deps: AppDeps = Depends(get_deps)):
        with admin_session(deps.factory) as session:
            counts = {
                (market_id, owner): n
                for market_id, owner, n in session.execute(
                    select(Conversation.market_id, Conversation.owner, func.count()).group_by(
                        Conversation.market_id, Conversation.owner
                    )
                )
            }
            return [
                schemas.MarketOut(
                    id=m.id,
                    name=m.name,
                    timezone=m.timezone,
                    agent_conversations=counts.get((m.id, "agent"), 0),
                    human_conversations=counts.get((m.id, "human"), 0),
                )
                for m in session.scalars(select(Market).order_by(Market.name))
            ]

    @app.get("/markets/{market_id}/conversations", response_model=list[schemas.ConversationSummary])
    def list_conversations(
        market_id: str, owner: str | None = None, deps: AppDeps = Depends(get_deps)
    ):
        with market_session(market_id, deps.factory) as session:
            stmt = select(Conversation).order_by(Conversation.id.desc()).limit(200)
            if owner:
                stmt = stmt.where(Conversation.owner == owner)
            return [_summary(session, c) for c in session.scalars(stmt)]

    @app.get(
        "/markets/{market_id}/conversations/{conversation_id}",
        response_model=schemas.ConversationDetail,
    )
    def get_conversation(market_id: str, conversation_id: int, deps: AppDeps = Depends(get_deps)):
        with market_session(market_id, deps.factory) as session:
            conversation = _conversation_or_404(session, conversation_id)
            tours = session.scalars(
                select(Tour).where(Tour.lead_id == conversation.lead_id).order_by(Tour.starts_at)
            )
            return {
                **_summary(session, conversation),
                "property": conversation.lead.property.name if conversation.lead.property else None,
                "messages": [
                    schemas.MessageOut.model_validate(m, from_attributes=True)
                    for m in conversation.messages
                ],
                "tours": [
                    {"property": t.property.name, "starts_at": t.starts_at, "status": t.status}
                    for t in tours
                ],
            }

    @app.post("/markets/{market_id}/conversations/{conversation_id}/takeover", status_code=204)
    async def take_over(market_id: str, conversation_id: int, deps: AppDeps = Depends(get_deps)):
        def mark_human() -> int:
            with market_session(market_id, deps.factory) as session:
                conversation = _conversation_or_404(session, conversation_id)
                conversation.owner = "human"
                conversation.handoff_reason = conversation.handoff_reason or "manual_takeover"
                return conversation.lead_id

        lead_id = await run_in_threadpool(mark_human)
        await deps.starter.stop(market_id, lead_id, "human_takeover")

    @app.post("/markets/{market_id}/conversations/{conversation_id}/messages", status_code=201)
    def send_human_message(
        market_id: str,
        conversation_id: int,
        body: schemas.HumanMessageIn,
        deps: AppDeps = Depends(get_deps),
    ) -> dict[str, object]:
        with market_session(market_id, deps.factory) as session:
            conversation = _conversation_or_404(session, conversation_id)
            if conversation.owner != "human":
                raise HTTPException(status.HTTP_409_CONFLICT, "Take over the conversation first.")
            message = draft_outbound(
                session, conversation, body.body, "human", f"human:{body.client_key}"
            )
            message_id = message.id
        outcome = deliver(deps.factory, market_id, message_id, deps.senders)
        return {"message_id": message_id, "delivery": outcome.value}

    @app.get("/markets/{market_id}/policies", response_model=list[schemas.PolicyOut])
    def list_policies(market_id: str, deps: AppDeps = Depends(get_deps)):
        with market_session(market_id, deps.factory) as session:
            return [
                schemas.PolicyOut(topic=p.topic, summary=p.summary)
                for p in session.scalars(select(Policy).order_by(Policy.topic))
            ]

    @app.put("/markets/{market_id}/policies/{topic}", response_model=schemas.PolicyOut)
    def update_policy(
        market_id: str, topic: str, body: schemas.PolicyUpdate, deps: AppDeps = Depends(get_deps)
    ):
        with market_session(market_id, deps.factory) as session:
            policy = session.scalar(select(Policy).where(Policy.topic == topic))
            if policy is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"No {topic} policy")
            policy.summary = body.summary
            return schemas.PolicyOut(topic=policy.topic, summary=policy.summary)

    @app.get("/markets/{market_id}/briefs", response_model=list[schemas.BriefOut])
    def list_briefs(market_id: str, week_of: date | None = None, deps: AppDeps = Depends(get_deps)):
        with market_session(market_id, deps.factory) as session:
            week = week_of or session.scalar(select(func.max(Brief.week_of)))
            if week is None:
                return []
            briefs = session.scalars(
                select(Brief).where(Brief.week_of == week).order_by(Brief.property_id)
            )
            return [
                schemas.BriefOut(
                    property=b.property.name,
                    week_of=b.week_of,
                    status=b.status,
                    body=b.body,
                    problems=b.problems,
                )
                for b in briefs
            ]
