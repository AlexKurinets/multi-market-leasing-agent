"""Pure-function checks: number grounding, Fair Housing screens, quiet hours, normalisation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from leasing.compliance import next_send_time
from leasing.guardrails import screen_inbound, steering_language
from leasing.ingestion.normalize import normalize_email, normalize_phone, split_name
from leasing.validation import extract_numbers, unsupported_numbers


def test_extract_numbers_reads_currency_percent_and_plain():
    tokens = extract_numbers("Rent is $1,450.00, 2.5% above comps, 12 days on market.")
    assert [(t.value, t.kind) for t in tokens] == [
        (Decimal("1450.00"), "currency"),
        (Decimal("2.5"), "percent"),
        (Decimal("12"), "number"),
    ]


def test_unsupported_numbers_matches_values_not_formatting():
    source = '{"median_rent": 1450, "gap_pct": -2.1, "units": 3}'
    assert unsupported_numbers("Median rent is $1,450 across 3 units.", [source]) == []
    bad = unsupported_numbers("Median rent is $1,475, up 4% this week.", [source])
    assert [t.raw for t in bad] == ["$1,475", "4%"]


def test_unsupported_numbers_can_check_only_money():
    bad = unsupported_numbers("Unit 204 is $1,299.", ['{"rent": "$1,299"}'], kinds=["currency"])
    assert bad == []


@pytest.mark.parametrize(
    ("text", "category", "handoff"),
    [
        ("Do you have wheelchair accessible units?", "accommodation", True),
        ("I have an emotional support animal, is that ok?", "accommodation", True),
        ("Do you take Section 8?", "source_of_income", True),
        ("What kind of people live there?", "demographics", True),
        ("Is it a family-friendly building?", "familial_status", True),
        ("Is the area safe at night?", "safety", False),
        ("How are the schools nearby?", "schools", False),
    ],
)
def test_inbound_screen_catches_steering_topics(text, category, handoff):
    result = screen_inbound(text)
    assert result is not None
    assert (result.category, result.handoff) == (category, handoff)


@pytest.mark.parametrize(
    "text",
    [
        "Do you have 2 bedrooms under $1,600?",
        "Hi, I'm Ada. Can I tour on Saturday?",
        "I have two kids and a dog, what's the pet policy?",
        "Is parking included?",
    ],
)
def test_inbound_screen_lets_ordinary_questions_through(text):
    assert screen_inbound(text) is None


def test_outbound_steering_language_is_caught():
    assert steering_language("It's perfect for young professionals!") is not None
    assert steering_language("It's a quiet neighborhood near the park.") is not None
    assert steering_language("Unit 204 has a balcony and in-unit laundry.") is None


def test_quiet_hours_defer_to_morning_without_dst():
    # 21:30 in Phoenix (UTC-7 all year) -> 09:00 next day
    now = datetime(2026, 10, 13, 4, 30, tzinfo=UTC)
    assert next_send_time(now, "America/Phoenix", 20, 9) == datetime(
        2026, 10, 13, 16, 0, tzinfo=UTC
    )


def test_quiet_hours_early_morning_waits_for_same_day():
    now = datetime(2026, 10, 12, 13, 0, tzinfo=UTC)  # 06:00 Phoenix
    assert next_send_time(now, "America/Phoenix", 20, 9) == datetime(
        2026, 10, 12, 16, 0, tzinfo=UTC
    )


def test_quiet_hours_across_dst_change():
    # 22:30 EDT on Oct 31; clocks fall back overnight, so 09:00 the next day is EST (UTC-5).
    now = datetime(2026, 11, 1, 2, 30, tzinfo=UTC)
    assert next_send_time(now, "America/New_York", 20, 9) == datetime(
        2026, 11, 1, 14, 0, tzinfo=UTC
    )


def test_daytime_sends_immediately():
    now = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)  # 13:00 Dallas
    assert next_send_time(now, "America/Chicago", 20, 9) == now


def test_normalization():
    # 555-0100 to 555-0199 is the range reserved for fictional use; every number in this repo
    # is from it.
    assert normalize_phone("(602) 555-0147") == "+16025550147"
    assert normalize_phone("214.555.0199") == "+12145550199"
    assert normalize_phone("+1 480 555 0123") == "+14805550123"
    assert normalize_phone("12345") is None
    assert normalize_email("  Jordan.Rivera@Example.com ") == "jordan.rivera@example.com"
    assert normalize_email("not an email") is None
    assert split_name("jordan rivera") == ("Jordan", "Rivera")
