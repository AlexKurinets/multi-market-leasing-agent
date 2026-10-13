from dataclasses import dataclass, field
from datetime import datetime

from leasing.crm.base import ContactData


@dataclass
class InMemoryCrm:
    """CRM stand-in for local development and tests."""

    contacts: dict[str, ContactData] = field(default_factory=dict)
    notes: dict[str, list[tuple[datetime, str]]] = field(default_factory=dict)

    def upsert_contact(self, contact: ContactData, crm_id: str | None) -> str:
        crm_id = crm_id or f"mem-{len(self.contacts) + 1}"
        self.contacts[crm_id] = contact
        return crm_id

    def log_note(self, crm_id: str, body: str, timestamp: datetime) -> None:
        self.notes.setdefault(crm_id, []).append((timestamp, body))
