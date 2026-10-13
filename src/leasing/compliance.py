"""Contact consent and quiet hours.

Consent is recorded per channel with its evidence. Quiet hours apply to automated follow-ups;
a direct reply to a message the prospect just sent goes out immediately.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from leasing.db.models import Consent, Lead
from leasing.db.scoping import MarketSession

# Carrier-standard keywords. Twilio Messaging Services also enforce these upstream; mirroring
# them keeps the consent table, the CRM and the follow-up workflow in agreement.
OPT_OUT_KEYWORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "revoke"}
OPT_IN_KEYWORDS = {"start", "unstop", "yes"}


def in_quiet_hours(now: datetime, tz: str, quiet_start: int, quiet_end: int) -> bool:
    hour = now.astimezone(ZoneInfo(tz)).hour
    if quiet_start > quiet_end:  # window wraps midnight, e.g. 20:00 -> 09:00
        return hour >= quiet_start or hour < quiet_end
    return quiet_start <= hour < quiet_end


def next_send_time(now: datetime, tz: str, quiet_start: int, quiet_end: int) -> datetime:
    """``now`` if a follow-up may go out now, otherwise the end of the quiet window (UTC)."""
    if not in_quiet_hours(now, tz, quiet_start, quiet_end):
        return now
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    day = local.date() if local.hour < quiet_end else local.date() + timedelta(days=1)
    return datetime.combine(day, time(quiet_end), tzinfo=zone).astimezone(UTC)


def has_consent(session: MarketSession, lead_id: int, channel: str) -> bool:
    consent = session.scalar(
        select(Consent).where(Consent.lead_id == lead_id, Consent.channel == channel)
    )
    return bool(consent and consent.granted and consent.revoked_at is None)


def record_consent(
    session: MarketSession, lead: Lead, channel: str, evidence: str, now: datetime
) -> Consent:
    consent = session.scalar(
        select(Consent).where(Consent.lead_id == lead.id, Consent.channel == channel)
    )
    if consent is None:
        consent = Consent(lead_id=lead.id, channel=channel)
        session.add(consent)
    consent.granted = True
    consent.evidence = evidence
    consent.captured_at = now
    consent.revoked_at = None
    return consent


def revoke_consent(
    session: MarketSession, lead: Lead, channel: str, evidence: str, now: datetime
) -> None:
    consent = session.scalar(
        select(Consent).where(Consent.lead_id == lead.id, Consent.channel == channel)
    )
    if consent is None:
        consent = Consent(lead_id=lead.id, channel=channel, granted=False)
        session.add(consent)
    consent.revoked_at = now
    consent.evidence = f"{consent.evidence}\nrevoked: {evidence}".strip()
