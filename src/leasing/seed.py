"""Synthetic demo data: three markets with deliberately different policies, so any cross-market
leak shows up as a wrong number. All properties, addresses, people and figures are made up."""

import random
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from leasing.db.models import Comp, Market, Policy, PolicyChunk, Property, Unit
from leasing.db.scoping import MarketSession
from leasing.db.session import admin_session
from leasing.embeddings import Embedder, HashingEmbedder

MARKETS = [
    ("PHX", "Phoenix", "America/Phoenix"),
    ("DAL", "Dallas", "America/Chicago"),
    ("ATL", "Atlanta", "America/New_York"),
]

# (name, address, submarket, one-bedroom base rent)
PROPERTIES = {
    "PHX": [
        ("Saguaro Flats", "1200 Sample Ave, Phoenix, AZ", "Midtown Phoenix", 1250),
        ("Rio Salado Commons", "300 Example Blvd, Tempe, AZ", "Tempe", 1325),
    ],
    "DAL": [
        ("Cedar Row Residences", "2400 Sample St, Dallas, TX", "Uptown", 1475),
        ("Elm Street Lofts", "800 Example Ave, Dallas, TX", "Deep Ellum", 1390),
    ],
    "ATL": [
        ("Peachtree Terrace", "1500 Sample Rd NE, Atlanta, GA", "Midtown Atlanta", 1550),
        ("Oakhurst Commons", "450 Example St, Decatur, GA", "Decatur", 1420),
    ],
}

# topic -> (summary, amount, unit)
POLICIES: dict[str, dict[str, tuple[str, int | float | None, str | None]]] = {
    "PHX": {
        "pets": (
            "Up to 2 pets (cats or dogs), 75 lb combined weight limit. $350 refundable pet "
            "deposit per pet plus $35 monthly pet rent per pet. Assistance animals are not pets "
            "and carry no pet deposit or pet rent.",
            350,
            "deposit per pet",
        ),
        "application_fee": ("$50 per applicant age 18 or older, non-refundable.", 50, "per adult"),
        "admin_fee": ("$150 one-time administrative fee, due at lease signing.", 150, "one-time"),
        "security_deposit": (
            "Security deposit starts at $300 with approved screening and can rise to one "
            "month's rent depending on screening results.",
            300,
            "minimum",
        ),
        "income_requirement": (
            "Household gross monthly income of at least 2.5 times the monthly rent.",
            2.5,
            "x monthly rent",
        ),
        "parking": (
            "One uncovered space included per home. Covered parking is $40 per month, "
            "subject to availability.",
            40,
            "per month",
        ),
        "lease_terms": (
            "Lease terms from 6 to 15 months. Terms under 12 months add $125 per month.",
            125,
            "per month under 12 months",
        ),
        "utilities": (
            "Residents set up electricity directly. Water, sewer and trash are a flat $65 per "
            "month.",
            65,
            "per month",
        ),
    },
    "DAL": {
        "pets": (
            "Up to 2 pets, 80 lb weight limit per pet. $500 pet deposit per pet ($250 of it "
            "refundable) plus $25 monthly pet rent per pet. Assistance animals are not pets and "
            "carry no pet deposit or pet rent.",
            500,
            "deposit per pet",
        ),
        "application_fee": ("$65 per applicant age 18 or older, non-refundable.", 65, "per adult"),
        "admin_fee": ("$200 one-time administrative fee, due at lease signing.", 200, "one-time"),
        "security_deposit": (
            "Security deposit starts at $400 with approved screening and can rise to one "
            "month's rent depending on screening results.",
            400,
            "minimum",
        ),
        "income_requirement": (
            "Household gross monthly income of at least 3 times the monthly rent.",
            3,
            "x monthly rent",
        ),
        "parking": (
            "Gated garage parking with one space included. A second space is $50 per month.",
            50,
            "per month",
        ),
        "lease_terms": (
            "Lease terms of 12 to 15 months. 6-month terms are available for $200 more per month.",
            200,
            "per month for 6-month terms",
        ),
        "utilities": (
            "Residents set up electricity directly. Water, sewer, trash and pest control are a "
            "flat $85 per month.",
            85,
            "per month",
        ),
    },
    "ATL": {
        "pets": (
            "1 pet maximum, 60 lb weight limit. $400 non-refundable pet fee plus $30 monthly "
            "pet rent. Assistance animals are not pets and carry no pet fee or pet rent.",
            400,
            "one-time fee",
        ),
        "application_fee": ("$75 per applicant age 18 or older, non-refundable.", 75, "per adult"),
        "admin_fee": ("$175 one-time administrative fee, due at lease signing.", 175, "one-time"),
        "security_deposit": (
            "Security deposit starts at $500 with approved screening and can rise to one "
            "month's rent depending on screening results.",
            500,
            "minimum",
        ),
        "income_requirement": (
            "Household gross monthly income of at least 3 times the monthly rent.",
            3,
            "x monthly rent",
        ),
        "parking": (
            "Structured garage parking at $60 per space per month. No spaces are included.",
            60,
            "per month",
        ),
        "lease_terms": ("Lease terms of 12 or 14 months only.", None, None),
        "utilities": (
            "Residents set up electricity and internet directly. Water, sewer and trash are a "
            "flat $90 per month.",
            90,
            "per month",
        ),
    },
}

POLICY_DOCUMENTS = {
    "PHX": [
        (
            "Pets and assistance animals",
            "Cats and dogs are welcome, up to 2 per home with a 75 lb combined weight limit. "
            "Each pet needs a $350 refundable deposit and $35 monthly pet rent. Pets must be "
            "registered before move-in with current vaccination records. Assistance animals "
            "are not pets: no deposit, fee or pet rent applies, and requests go to the "
            "leasing team.",
        ),
        (
            "Application and screening",
            "Every adult applicant completes an online application with a $50 fee. Screening "
            "covers identity, rental history, credit and income. Income should be at least "
            "2.5 times the rent. Criminal history is reviewed individually, not by blanket "
            "rules. Decisions usually arrive within two business days.",
        ),
        (
            "Move-in and lease terms",
            "Leases run 6 to 15 months; terms under 12 months add $125 per month. A $150 "
            "admin fee and the security deposit are due at signing. Renters insurance with "
            "at least $100,000 liability coverage is required before keys are released.",
        ),
    ],
    "DAL": [
        (
            "Pets and assistance animals",
            "Up to 2 pets per home, each under 80 lb. Each pet needs a $500 deposit, of which "
            "$250 is refundable, plus $25 monthly pet rent. Pets are registered before "
            "move-in. Assistance animals are not pets: no deposit, fee or pet rent applies, "
            "and requests go to the leasing team.",
        ),
        (
            "Application and screening",
            "Every adult applicant completes an online application with a $65 fee. Screening "
            "covers identity, rental history, credit and income. Income should be at least 3 "
            "times the rent. Criminal history is reviewed individually, not by blanket rules. "
            "Decisions usually arrive within two business days.",
        ),
        (
            "Move-in and lease terms",
            "Leases run 12 to 15 months, with 6-month terms at $200 more per month. A $200 "
            "admin fee and the security deposit are due at signing. Renters insurance with at "
            "least $100,000 liability coverage is required before keys are released.",
        ),
    ],
    "ATL": [
        (
            "Pets and assistance animals",
            "One pet per home, up to 60 lb, with a $400 non-refundable pet fee and $30 monthly "
            "pet rent. Pets are registered before move-in. Assistance animals are not pets: no "
            "deposit, fee or pet rent applies, and requests go to the leasing team.",
        ),
        (
            "Application and screening",
            "Every adult applicant completes an online application with a $75 fee. Screening "
            "covers identity, rental history, credit and income. Income should be at least 3 "
            "times the rent. Criminal history is reviewed individually, not by blanket rules. "
            "Decisions usually arrive within three business days.",
        ),
        (
            "Move-in and lease terms",
            "Leases are 12 or 14 months. A $175 admin fee and the security deposit are due at "
            "signing. Renters insurance with at least $100,000 liability coverage is required "
            "before keys are released.",
        ),
    ],
}

_BEDROOM_PLAN = [0, 1, 1, 2, 2, 2, 3, 1]
_SQFT = {0: 520, 1: 720, 2: 1040, 3: 1310}
_RENT_OFFSET = {0: -180, 1: 0, 2: 390, 3: 720}
_BATHS = {0: Decimal("1"), 1: Decimal("1"), 2: Decimal("2"), 3: Decimal("2")}
_AVAILABLE_IN_DAYS = [0, 7, 14, 21, 30, 45, 10, 3]
_LEASED = {5, 7}


def seed(
    factory: sessionmaker[MarketSession] | None = None,
    today: date | None = None,
    embedder: Embedder | None = None,
) -> bool:
    """Insert demo data once. Returns False if markets already exist."""
    today = today or date.today()
    embedder = embedder or HashingEmbedder()
    with admin_session(factory) as session:
        if session.scalar(select(Market.id).limit(1)):
            return False
        for market_id, name, tz in MARKETS:
            session.add(Market(id=market_id, name=name, timezone=tz))
        session.flush()

        for market_id, properties in PROPERTIES.items():
            rng = random.Random(market_id)
            for name, address, submarket, base in properties:
                prop = Property(
                    market_id=market_id, name=name, address=address, submarket=submarket
                )
                session.add(prop)
                session.flush()
                for i, bedrooms in enumerate(_BEDROOM_PLAN):
                    session.add(
                        Unit(
                            market_id=market_id,
                            property_id=prop.id,
                            unit_number=f"{1 + i // 4}{(i % 4) + 1:02d}",
                            bedrooms=bedrooms,
                            bathrooms=_BATHS[bedrooms],
                            sqft=_SQFT[bedrooms] + rng.randrange(-40, 60, 10),
                            rent=base + _RENT_OFFSET[bedrooms] + rng.randrange(-40, 65, 5),
                            available_on=today + timedelta(days=_AVAILABLE_IN_DAYS[i]),
                            status="leased" if i in _LEASED else "available",
                        )
                    )
                _seed_comps(session, market_id, submarket, base, today)

            for topic, (summary, amount, unit) in POLICIES[market_id].items():
                session.add(
                    Policy(
                        market_id=market_id,
                        topic=topic,
                        summary=summary,
                        amount=Decimal(str(amount)) if amount is not None else None,
                        amount_unit=unit,
                    )
                )
            docs = POLICY_DOCUMENTS[market_id]
            vectors = embedder.embed([f"{title}\n{body}" for title, body in docs])
            for (title, body), vector in zip(docs, vectors, strict=True):
                session.add(
                    PolicyChunk(market_id=market_id, title=title, body=body, embedding=vector)
                )
    return True


def _seed_comps(
    session: MarketSession, market_id: str, submarket: str, base: int, today: date
) -> None:
    rng = random.Random(f"{market_id}:{submarket}")
    for bedrooms in range(4):
        for _ in range(12):
            listed = today - timedelta(days=rng.randint(3, 58))
            leased = listed + timedelta(days=rng.randint(6, 40))
            session.add(
                Comp(
                    market_id=market_id,
                    submarket=submarket,
                    bedrooms=bedrooms,
                    rent=round((base + _RENT_OFFSET[bedrooms]) * rng.uniform(0.92, 1.09) / 5) * 5,
                    sqft=round(_SQFT[bedrooms] * rng.uniform(0.9, 1.1)),
                    listed_on=listed,
                    leased_on=leased if leased <= today and rng.random() < 0.7 else None,
                    concession_weeks=Decimal(rng.choice(["0", "0", "0", "2", "4"])),
                )
            )
