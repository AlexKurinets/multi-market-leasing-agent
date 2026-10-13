from datetime import date, datetime

from pydantic import BaseModel, Field


class LeadIn(BaseModel):
    market_id: str
    property_id: int | None = None
    first_name: str = ""
    last_name: str = ""
    email: str | None = None
    phone: str | None = None
    source: str = "website"
    message: str = ""
    sms_consent: bool = False
    consent_evidence: str = Field("", description="Where consent was captured, verbatim.")


class LeadEmailIn(BaseModel):
    source: str = Field(description="Listing site key, e.g. 'zillow' or 'apartments_com'.")
    body: str
    market_id: str | None = None


class LeadAccepted(BaseModel):
    lead_id: int
    conversation_id: int
    is_new: bool
    workflow_id: str


class MarketOut(BaseModel):
    id: str
    name: str
    timezone: str
    agent_conversations: int
    human_conversations: int


class ConversationSummary(BaseModel):
    id: int
    lead_id: int
    lead_name: str
    contact: str | None
    lead_status: str
    owner: str
    channel: str
    handoff_reason: str | None
    last_message: str | None
    last_message_at: datetime | None


class MessageOut(BaseModel):
    id: int
    direction: str
    author: str
    channel: str
    body: str
    status: str
    created_at: datetime


class TourOut(BaseModel):
    property: str
    starts_at: datetime
    status: str


class ConversationDetail(ConversationSummary):
    property: str | None
    messages: list[MessageOut]
    tours: list[TourOut]


class HumanMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=1600)
    client_key: str = Field(min_length=8, max_length=64, description="Idempotency key.")


class PolicyOut(BaseModel):
    topic: str
    summary: str


class PolicyUpdate(BaseModel):
    summary: str = Field(min_length=5)


class BriefOut(BaseModel):
    property: str
    week_of: date
    status: str
    body: str
    problems: list[str]
