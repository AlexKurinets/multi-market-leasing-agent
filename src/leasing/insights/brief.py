import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from leasing.config import Settings
from leasing.db.models import Brief, Property
from leasing.db.scoping import MarketSession
from leasing.db.session import market_session
from leasing.insights.metrics import brief_payload
from leasing.llm import create_message, dump_content, text_of
from leasing.validation import unsupported_numbers

BRIEF_SYSTEM = """\
You write a short weekly pricing brief for a property's leasing manager.

Use only the numbers in the JSON payload, written exactly as they appear there (you may add \
a $ or % sign). Do not calculate anything: no differences, sums, averages or new percentages. \
If a comparison matters, use the precomputed asking_vs_submarket_pct. A null means no data; \
say so rather than estimating.

Under 120 words of plain prose, no headings or lists. Lead with the most decision-relevant \
point for pricing or concessions this week.
"""

MAX_ATTEMPTS = 2


@dataclass
class BriefResult:
    status: str  # "approved" | "rejected"
    body: str
    problems: list[str] = field(default_factory=list)
    attempts: int = 1


class BriefWriter:
    def __init__(self, client: Any, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def write(self, payload: dict[str, Any]) -> BriefResult:
        source = json.dumps(payload, sort_keys=True)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": f"<payload>{source}</payload>\nWrite this week's brief."}
        ]
        problems: list[str] = []
        body = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            response = create_message(
                self.client,
                model=self.settings.anthropic_model,
                max_tokens=8000,
                system=BRIEF_SYSTEM,
                messages=messages,
                output_config={"effort": self.settings.brief_effort},
            )
            if response.stop_reason == "refusal":
                return BriefResult("rejected", "", ["model_refusal"], attempt)
            body = text_of(response)
            bad = unsupported_numbers(body, [source])
            if not bad:
                return BriefResult("approved", body, [], attempt)
            problems = [f"not in payload: {t.raw}" for t in bad]
            messages += [
                {"role": "assistant", "content": dump_content(response)},
                {
                    "role": "user",
                    "content": "These figures are not in the payload: "
                    + ", ".join(t.raw for t in bad)
                    + ". Rewrite the brief using only payload figures.",
                },
            ]
        # Stored for a person to review; never shown to managers as approved.
        return BriefResult("rejected", body, problems, MAX_ATTEMPTS)


def generate_market_briefs(
    writer: BriefWriter,
    market_id: str,
    week_of: date,
    factory: sessionmaker[MarketSession] | None = None,
) -> int:
    """Write any missing briefs for the week. Idempotent: existing briefs are left alone."""
    with market_session(market_id, factory) as session:
        done = set(session.scalars(select(Brief.property_id).where(Brief.week_of == week_of)))
        properties = session.scalars(select(Property).order_by(Property.id)).all()
        todo = [(p.id, brief_payload(session, p, week_of)) for p in properties if p.id not in done]

    written = 0
    for property_id, payload in todo:
        result = writer.write(payload)
        with market_session(market_id, factory) as session:
            session.add(
                Brief(
                    property_id=property_id,
                    week_of=week_of,
                    status=result.status,
                    body=result.body,
                    payload=payload,
                    problems=result.problems,
                    attempts=result.attempts,
                )
            )
        written += 1
    return written
