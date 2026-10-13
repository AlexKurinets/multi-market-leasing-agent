"""Parsing lead notification emails from listing sites.

Each source formats its notification differently, but nearly all of them use labelled fields.
Rather than one brittle regex per source, sources map their labels onto canonical fields. The
samples in ``samples/`` are synthetic; real feeds are configured by adding aliases.
"""

import re
from dataclasses import dataclass

from leasing.ingestion.normalize import split_name

_CANONICAL_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "renter name", "lead name", "contact name"),
    "email": ("email", "email address", "renter email"),
    "phone": ("phone", "phone number", "renter phone", "mobile"),
    "property": ("property", "listing", "property name", "community"),
    "move_in": ("move-in date", "move in date", "desired move-in", "move-in"),
    "message": ("message", "comments", "renter message", "questions"),
}

_SOURCE_EXTRA_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "apartments_com": {"message": ("additional comments",)},
    "zillow": {"name": ("from",)},
}

_LABEL_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z \-]{1,40}):\s*(.*)$")


@dataclass(frozen=True)
class ParsedLead:
    source: str
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    property_hint: str | None
    move_in: str | None
    message: str


def _label_map(source: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for field, aliases in _CANONICAL_ALIASES.items():
        for alias in aliases + _SOURCE_EXTRA_ALIASES.get(source, {}).get(field, ()):
            mapping[alias] = field
    return mapping


def parse_lead_email(source: str, body: str) -> ParsedLead:
    labels = _label_map(source)
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _LABEL_LINE.match(line)
        field = labels.get(match.group(1).strip().lower()) if match else None
        if match and field:
            current = field
            fields.setdefault(field, [])
            if match.group(2).strip():
                fields[field].append(match.group(2).strip())
        elif current == "message" and line.strip():
            # Messages are the only field that wraps across lines.
            fields["message"].append(line.strip())
        elif not line.strip():
            continue
        else:
            current = None

    def get(field: str) -> str | None:
        values = fields.get(field)
        return " ".join(values) if values else None

    first, last = split_name(get("name"))
    return ParsedLead(
        source=source,
        first_name=first,
        last_name=last,
        email=get("email"),
        phone=get("phone"),
        property_hint=get("property"),
        move_in=get("move_in"),
        message=get("message") or "",
    )
