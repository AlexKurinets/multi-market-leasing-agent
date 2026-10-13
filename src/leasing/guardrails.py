"""Fair Housing guardrails.

Inbound screening catches questions a leasing assistant should not answer on its own: who
lives in an area, safety and schools (classic steering vectors), accommodation requests and
source-of-income questions. Outbound screening blocks replies that describe a property or area
as suited to a type of person. Both run outside the model, so a prompt regression cannot turn
them off.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ScreenResult:
    category: str
    reply: str
    handoff: bool


_HANDOFF_REPLY = (
    "Thanks for asking. A member of our leasing team will follow up with you personally on that."
)

_INBOUND_RULES: list[tuple[str, re.Pattern[str], str, bool]] = [
    (
        "accommodation",
        re.compile(
            r"\b(wheelchair|disabilit\w*|accessib\w*|service (animal|dog)|emotional support|"
            r"assistance animal|reasonable accommodation)\b",
            re.I,
        ),
        "Thanks for letting us know. A member of our leasing team will reach out personally "
        "to help with that request.",
        True,
    ),
    (
        "source_of_income",
        re.compile(r"\b(section 8|housing choice voucher|housing voucher|voucher)\b", re.I),
        _HANDOFF_REPLY,
        True,
    ),
    (
        "demographics",
        re.compile(
            r"\b(demographic\w*|ethnic\w*|race|racial|religio\w*|church|mosque|synagogue|"
            r"(what|which) kind of (people|neighbors|residents)|who lives)\b",
            re.I,
        ),
        "We welcome everyone and don't describe our communities by who lives there. "
        "A member of our leasing team will follow up with you.",
        True,
    ),
    (
        "familial_status",
        re.compile(
            r"\b((family|kid|child)[- ]friendly|adults? only|"
            r"(lots of|many|any|mostly) (families|kids|children|students|seniors|retirees))\b",
            re.I,
        ),
        "We welcome everyone and don't describe our communities by who lives there. "
        "A member of our leasing team will follow up with you.",
        True,
    ),
    (
        "safety",
        re.compile(r"\b(safe|safety|crime|dangerous|sketchy|bad area)\b", re.I),
        "Good question. We don't characterize neighborhoods, but your local police department "
        "publishes crime data you can review. Happy to help with anything about the apartments.",
        False,
    ),
    (
        "schools",
        re.compile(r"\b(schools?|school district)\b", re.I),
        "The local school district is the best source for school information, so we don't "
        "rate schools. Happy to help with anything about the apartments.",
        False,
    ),
]

_OUTBOUND_STEERING = re.compile(
    r"\b(perfect|great|ideal|popular) (for|with) (families|kids|singles|young professionals|"
    r"students|retirees|seniors|couples)|(safe|quiet|good) (neighborhood|area)|good schools?\b",
    re.I,
)


def screen_inbound(text: str) -> ScreenResult | None:
    for category, pattern, reply, handoff in _INBOUND_RULES:
        if pattern.search(text):
            return ScreenResult(category, reply, handoff)
    return None


def steering_language(text: str) -> str | None:
    match = _OUTBOUND_STEERING.search(text)
    return match.group(0) if match else None
