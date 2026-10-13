from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from temporalio import activity

from leasing.agent.loop import LeasingAgent
from leasing.channels.base import Sender
from leasing.conversations import respond
from leasing.db.models import Market
from leasing.db.scoping import MarketSession
from leasing.db.session import admin_session
from leasing.insights.brief import BriefWriter, generate_market_briefs
from leasing.workflows.types import (
    GENERATE_BRIEFS_ACTIVITY,
    LIST_MARKETS_ACTIVITY,
    RESPOND_ACTIVITY,
    BriefsRequest,
    RespondRequest,
    RespondResult,
)


class LeasingActivities:
    """Synchronous activities (SQLAlchemy and the SDK are sync); the worker runs them in a
    thread pool."""

    def __init__(
        self,
        agent: LeasingAgent,
        senders: dict[str, Sender],
        brief_writer: BriefWriter,
        factory: sessionmaker[MarketSession] | None = None,
    ) -> None:
        self.agent = agent
        self.senders = senders
        self.brief_writer = brief_writer
        self.factory = factory

    @activity.defn(name=RESPOND_ACTIVITY)
    def respond_to_lead(self, req: RespondRequest) -> RespondResult:
        return respond(req, agent=self.agent, senders=self.senders, factory=self.factory)

    @activity.defn(name=LIST_MARKETS_ACTIVITY)
    def list_markets(self) -> list[str]:
        with admin_session(self.factory) as session:
            return list(session.scalars(select(Market.id).order_by(Market.id)))

    @activity.defn(name=GENERATE_BRIEFS_ACTIVITY)
    def generate_market_briefs(self, req: BriefsRequest) -> int:
        return generate_market_briefs(
            self.brief_writer, req.market_id, date.fromisoformat(req.week_of), self.factory
        )
