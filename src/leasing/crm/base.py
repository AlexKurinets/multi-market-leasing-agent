from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ContactData:
    first_name: str
    last_name: str
    email: str | None
    phone: str | None
    market: str
    property_name: str | None
    source: str
    lead_status: str


class CrmError(Exception):
    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class CrmAdapter(Protocol):
    """Everything the platform needs from a CRM. Adding a CRM means implementing these two."""

    def upsert_contact(self, contact: ContactData, crm_id: str | None) -> str: ...

    def log_note(self, crm_id: str, body: str, timestamp: datetime) -> None: ...
