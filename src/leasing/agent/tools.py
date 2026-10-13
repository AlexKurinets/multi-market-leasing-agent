"""Agent tools. Each runs inside the conversation's market-scoped session, so a tool cannot
return another market's units or policies even if the model asks for them by id."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from leasing.crm.outbox import enqueue
from leasing.db.models import Conversation, Lead, Market, Policy, Property, Tour, Unit
from leasing.db.scoping import MarketSession
from leasing.embeddings import Embedder
from leasing.retrieval import search_policy_chunks

PolicyTopic = Literal[
    "pets",
    "application_fee",
    "admin_fee",
    "security_deposit",
    "income_requirement",
    "parking",
    "lease_terms",
    "utilities",
]

MAX_TOUR_DAYS_AHEAD = 14

log = logging.getLogger(__name__)


class ToolError(Exception):
    """A problem the model can recover from; returned to it as an error tool result."""


@dataclass
class ToolContext:
    session: MarketSession
    market: Market
    lead: Lead
    conversation: Conversation
    embedder: Embedder
    now: datetime
    tour_booked: Tour | None = None
    handoff_reason: str | None = None
    calls: list[str] = field(default_factory=list)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchUnitsInput(_Input):
    property_id: int | None = Field(description="One property's id, or null for all properties.")
    bedrooms: int | None = Field(description="Exact bedroom count (0 = studio), or null.")
    max_rent: int | None = Field(description="Maximum monthly rent in dollars, or null.")
    available_by: date | None = Field(
        description="Latest acceptable move-in date (YYYY-MM-DD), or null."
    )


class GetPolicyInput(_Input):
    topic: PolicyTopic = Field(description="The policy to look up.")


class SearchPolicyDocumentsInput(_Input):
    query: str = Field(description="What the prospect wants to know, in plain words.")


class CheckTourSlotsInput(_Input):
    property_id: int = Field(description="Property to tour.")
    day: date = Field(description="Day to check (YYYY-MM-DD), within the next two weeks.")


class BookTourInput(_Input):
    property_id: int = Field(description="Property to tour.")
    starts_at: str = Field(
        description="Slot start exactly as returned by check_tour_slots (YYYY-MM-DDTHH:MM)."
    )


class HandoffInput(_Input):
    reason: Literal[
        "accommodation_request",
        "application_or_lease_question",
        "complaint",
        "pricing_exception",
        "unknown_answer",
        "prospect_requested_person",
        "other",
    ]
    summary: str = Field(description="Two sentences for the leasing team: context and the ask.")


def _search_units(ctx: ToolContext, inp: SearchUnitsInput) -> dict[str, Any]:
    stmt = (
        select(Unit, Property)
        .join(Property, Unit.property_id == Property.id)
        .where(Unit.status == "available")
        .order_by(Unit.rent)
        .limit(8)
    )
    if inp.property_id is not None:
        stmt = stmt.where(Unit.property_id == inp.property_id)
    if inp.bedrooms is not None:
        stmt = stmt.where(Unit.bedrooms == inp.bedrooms)
    if inp.max_rent is not None:
        stmt = stmt.where(Unit.rent <= inp.max_rent)
    if inp.available_by is not None:
        stmt = stmt.where(Unit.available_on <= inp.available_by)
    units = [
        {
            "unit_id": unit.id,
            "unit_number": unit.unit_number,
            "property_id": prop.id,
            "property": prop.name,
            "bedrooms": unit.bedrooms,
            "bathrooms": str(unit.bathrooms.normalize()),
            "sqft": unit.sqft,
            "monthly_rent": f"${unit.rent:,}",
            "available_on": unit.available_on.isoformat(),
        }
        for unit, prop in ctx.session.execute(stmt).all()
    ]
    if not units:
        return {"units": [], "note": "No available units match these filters."}
    return {"units": units}


def _get_policy(ctx: ToolContext, inp: GetPolicyInput) -> dict[str, Any]:
    policy = ctx.session.scalar(select(Policy).where(Policy.topic == inp.topic))
    if policy is None:
        raise ToolError(f"No {inp.topic} policy is on file for this market.")
    return {"topic": policy.topic, "policy": policy.summary}


def _search_policy_documents(ctx: ToolContext, inp: SearchPolicyDocumentsInput) -> dict[str, Any]:
    chunks = search_policy_chunks(ctx.session, ctx.embedder, inp.query, k=2)
    return {"results": [{"title": c.title, "text": c.body} for c in chunks]}


def _property(ctx: ToolContext, property_id: int) -> Property:
    prop = ctx.session.get(Property, property_id)
    if prop is None:  # includes properties that exist in other markets
        raise ToolError(f"There is no property {property_id} in this market.")
    return prop


def _open_slots(ctx: ToolContext, prop: Property, day: date) -> list[datetime]:
    zone = ZoneInfo(ctx.market.timezone)
    today = ctx.now.astimezone(zone).date()
    if not today <= day <= today + timedelta(days=MAX_TOUR_DAYS_AHEAD):
        raise ToolError(f"Tours can be booked from today up to {MAX_TOUR_DAYS_AHEAD} days ahead.")
    start = datetime(day.year, day.month, day.day, prop.tour_start_hour, tzinfo=zone)
    end = datetime(day.year, day.month, day.day, prop.tour_end_hour, tzinfo=zone)
    taken = set(
        ctx.session.scalars(
            select(Tour.starts_at).where(
                Tour.property_id == prop.id,
                Tour.status == "scheduled",
                Tour.starts_at >= start,
                Tour.starts_at < end,
            )
        )
    )
    earliest = ctx.now + timedelta(hours=1)
    slots, slot = [], start
    while slot < end:
        if slot > earliest and slot not in taken:
            slots.append(slot)
        slot += timedelta(minutes=prop.tour_slot_minutes)
    return slots


def _check_tour_slots(ctx: ToolContext, inp: CheckTourSlotsInput) -> dict[str, Any]:
    prop = _property(ctx, inp.property_id)
    slots = _open_slots(ctx, prop, inp.day)
    return {
        "property": prop.name,
        "date": inp.day.isoformat(),
        "timezone": ctx.market.timezone,
        "slots": [s.strftime("%Y-%m-%dT%H:%M") for s in slots],
    }


def _book_tour(ctx: ToolContext, inp: BookTourInput) -> dict[str, Any]:
    prop = _property(ctx, inp.property_id)
    try:
        local = datetime.strptime(inp.starts_at, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ToolError("starts_at must look like 2026-10-14T10:30.") from exc
    starts_at = local.replace(tzinfo=ZoneInfo(ctx.market.timezone))
    if starts_at not in _open_slots(ctx, prop, starts_at.date()):
        raise ToolError("That time is not open. Call check_tour_slots and offer an open slot.")
    tour = Tour(lead_id=ctx.lead.id, property_id=prop.id, starts_at=starts_at)
    try:
        with ctx.session.begin_nested():
            ctx.session.add(tour)
    except IntegrityError as exc:
        raise ToolError("Someone just booked that slot. Offer another open time.") from exc
    ctx.lead.status = "tour_booked"
    ctx.tour_booked = tour
    enqueue(ctx.session, ctx.lead, "log_tour", {"tour_id": tour.id})
    return {
        "booked": True,
        "property": prop.name,
        "starts_at": inp.starts_at,
        "display": _display_time(starts_at),
    }


def _display_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    minutes = f":{dt.minute:02d}" if dt.minute else ""
    return f"{dt:%A, %B} {dt.day} at {hour}{minutes} {'AM' if dt.hour < 12 else 'PM'}"


def _handoff(ctx: ToolContext, inp: HandoffInput) -> dict[str, Any]:
    ctx.conversation.owner = "human"
    ctx.conversation.handoff_reason = f"{inp.reason}: {inp.summary}"
    ctx.lead.status = "handed_off"
    ctx.handoff_reason = inp.reason
    return {
        "handed_off": True,
        "instruction": "Tell the prospect a team member will follow up. Do not promise a time.",
    }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_model: type[_Input]
    run: Callable[[ToolContext, Any], dict[str, Any]]


TOOL_SPECS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            "search_units",
            "Search currently available units in the prospect's market, cheapest first.",
            SearchUnitsInput,
            _search_units,
        ),
        ToolSpec(
            "get_policy",
            "Get the market's policy on one topic, including any fees or amounts.",
            GetPolicyInput,
            _get_policy,
        ),
        ToolSpec(
            "search_policy_documents",
            "Search the market's longer policy documents for details not covered by get_policy.",
            SearchPolicyDocumentsInput,
            _search_policy_documents,
        ),
        ToolSpec(
            "check_tour_slots",
            "List open in-person tour times for a property on one day, in local time.",
            CheckTourSlotsInput,
            _check_tour_slots,
        ),
        ToolSpec(
            "book_tour",
            "Book a tour slot the prospect chose from times you offered.",
            BookTourInput,
            _book_tour,
        ),
        ToolSpec(
            "handoff_to_human",
            "Hand the conversation to the leasing team. Use it for anything you must not or "
            "cannot answer from your tools.",
            HandoffInput,
            _handoff,
        ),
    )
}


def _strict_schema(model: type[_Input]) -> dict[str, Any]:
    """Pydantic JSON schema reshaped for strict tool use: every property required (nullable
    ones are ``anyOf`` with null), no extra keys, no titles or defaults."""

    def clean(node: Any, in_properties: bool = False) -> None:
        if isinstance(node, dict):
            if not in_properties:
                node.pop("title", None)
                node.pop("default", None)
            for key, value in node.items():
                clean(value, in_properties=(key == "properties"))
        elif isinstance(node, list):
            for item in node:
                clean(item)

    schema = model.model_json_schema()
    clean(schema)
    schema["additionalProperties"] = False
    schema["required"] = list(schema.get("properties", {}))
    return schema


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": _strict_schema(spec.input_model),
            "strict": True,
        }
        for spec in TOOL_SPECS.values()
    ]


def execute_tool(ctx: ToolContext, name: str, raw_input: Any) -> tuple[str, bool]:
    """Run a tool call. Returns (JSON result, is_error)."""
    ctx.calls.append(name)
    spec = TOOL_SPECS.get(name)
    if spec is None:
        return json.dumps({"error": f"Unknown tool {name}."}), True
    # The manual loop validates inputs itself rather than trusting the parsed JSON.
    try:
        parsed = spec.input_model.model_validate(raw_input)
    except ValidationError as exc:
        return json.dumps({"error": f"Invalid input: {exc.errors(include_url=False)}"}), True
    try:
        output, is_error = json.dumps(spec.run(ctx, parsed)), False
    except ToolError as exc:
        output, is_error = json.dumps({"error": str(exc)}), True
    log.info("tool %s %s -> %s", name, parsed.model_dump_json(), output[:300])
    return output, is_error
