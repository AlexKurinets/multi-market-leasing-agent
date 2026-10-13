"""HubSpot CRM adapter (CRM v3 objects API, private-app access token).

``leasing_market`` and ``leasing_property`` are custom contact properties; create them in the
HubSpot portal before enabling this adapter.
"""

from datetime import datetime
from typing import Any

import httpx

from leasing.crm.base import ContactData, CrmError

# Platform lead status -> HubSpot's built-in hs_lead_status options.
_LEAD_STATUS = {
    "new": "NEW",
    "engaged": "CONNECTED",
    "tour_booked": "OPEN_DEAL",
    "handed_off": "IN_PROGRESS",
    "opted_out": "UNQUALIFIED",
    "closed": "UNQUALIFIED",
}
_NOTE_TO_CONTACT = 202  # HUBSPOT_DEFINED association type id


class HubSpotCrm:
    def __init__(self, access_token: str, client: httpx.Client | None = None) -> None:
        self._http = client or httpx.Client(
            base_url="https://api.hubapi.com",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )

    def upsert_contact(self, contact: ContactData, crm_id: str | None) -> str:
        properties = {
            "firstname": contact.first_name,
            "lastname": contact.last_name,
            "hs_lead_status": _LEAD_STATUS.get(contact.lead_status, "OPEN"),
            "leasing_market": contact.market,
            "leasing_property": contact.property_name or "",
        }
        if contact.email:
            properties["email"] = contact.email
        if contact.phone:
            properties["phone"] = contact.phone

        crm_id = crm_id or self._find_contact(contact)
        if crm_id:
            self._request("PATCH", f"/crm/v3/objects/contacts/{crm_id}", {"properties": properties})
            return crm_id
        created = self._request("POST", "/crm/v3/objects/contacts", {"properties": properties})
        return str(created["id"])

    def log_note(self, crm_id: str, body: str, timestamp: datetime) -> None:
        self._request(
            "POST",
            "/crm/v3/objects/notes",
            {
                "properties": {"hs_timestamp": timestamp.isoformat(), "hs_note_body": body},
                "associations": [
                    {
                        "to": {"id": crm_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": _NOTE_TO_CONTACT,
                            }
                        ],
                    }
                ],
            },
        )

    def _find_contact(self, contact: ContactData) -> str | None:
        for prop, value in (("email", contact.email), ("phone", contact.phone)):
            if not value:
                continue
            found = self._request(
                "POST",
                "/crm/v3/objects/contacts/search",
                {
                    "filterGroups": [
                        {"filters": [{"propertyName": prop, "operator": "EQ", "value": value}]}
                    ],
                    "limit": 1,
                },
            )
            if found.get("results"):
                return str(found["results"][0]["id"])
        return None

    def _request(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, json=body)
        except httpx.TransportError as exc:
            raise CrmError(f"HubSpot unreachable: {exc}", retryable=True) from exc
        if response.status_code == 429 or response.status_code >= 500:
            raise CrmError(f"HubSpot {response.status_code}: {response.text}", retryable=True)
        if response.status_code >= 400:
            raise CrmError(f"HubSpot {response.status_code}: {response.text}", retryable=False)
        return response.json() if response.content else {}
