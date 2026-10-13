"""Workflow and activity payloads. Plain dataclasses with no heavy imports, so the Temporal
workflow sandbox can load them."""

from dataclasses import dataclass, field

RESPOND_ACTIVITY = "respond_to_lead"
LIST_MARKETS_ACTIVITY = "list_markets"
GENERATE_BRIEFS_ACTIVITY = "generate_market_briefs"


@dataclass
class ConversationInput:
    market_id: str
    lead_id: int
    cadence_hours: list[int]
    # False when the run is started by a prospect message (signal-with-start): reply to that
    # message instead of sending a new-lead greeting.
    greet: bool = True


@dataclass
class RespondRequest:
    market_id: str
    lead_id: int
    trigger: str  # "new_lead" | "inbound" | "follow_up"
    idempotency_key: str
    message_ids: list[int] = field(default_factory=list)
    follow_up_number: int = 0
    follow_up_total: int = 0


@dataclass
class RespondResult:
    status: str  # "sent" | "deferred" | "skipped" | "failed" | "unknown"
    detail: str = ""
    retry_at: str | None = None  # ISO-8601 UTC, set when status is "deferred"
    tour_booked: bool = False
    handed_off: bool = False
    message_id: int | None = None


@dataclass
class BriefsRequest:
    market_id: str
    week_of: str  # ISO date of the Monday the brief covers
