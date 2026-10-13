"""Comp metrics, computed in SQL. The model never does arithmetic; it narrates these numbers."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, Numeric, case, cast, func, literal, select

from leasing.db.models import Comp, Property, Unit
from leasing.db.scoping import MarketSession

COMP_WINDOW_DAYS = 60


def _median(column: Any) -> Any:
    return func.percentile_cont(0.5).within_group(column)


def _share(condition: Any) -> Any:
    return case((condition, 1.0), else_=0.0)


def _round(value: Any, places: int = 0) -> int | float | None:
    if value is None:
        return None
    rounded = round(Decimal(str(value)), places)
    return int(rounded) if places == 0 else float(rounded)


def submarket_comps(session: MarketSession, submarket: str, as_of: date) -> list[dict[str, Any]]:
    days_on_market = func.coalesce(Comp.leased_on, literal(as_of, Date)) - Comp.listed_on
    stmt = (
        select(
            Comp.bedrooms,
            func.count().label("comps"),
            _median(Comp.rent).label("median_rent"),
            _median(cast(Comp.rent, Numeric) / Comp.sqft).label("median_rent_per_sqft"),
            _median(days_on_market).label("median_days_on_market"),
            (100 * func.avg(_share(Comp.leased_on.is_not(None)))).label("leased_pct"),
            (100 * func.avg(_share(Comp.concession_weeks > 0))).label("concession_pct"),
        )
        .where(
            Comp.submarket == submarket,
            Comp.listed_on >= as_of - timedelta(days=COMP_WINDOW_DAYS),
            Comp.listed_on <= as_of,
        )
        .group_by(Comp.bedrooms)
        .order_by(Comp.bedrooms)
    )
    return [
        {
            "bedrooms": row.bedrooms,
            "comps": row.comps,
            "median_rent": _round(row.median_rent),
            "median_rent_per_sqft": _round(row.median_rent_per_sqft, 2),
            "median_days_on_market": _round(row.median_days_on_market),
            "leased_pct": _round(row.leased_pct, 1),
            "with_concessions_pct": _round(row.concession_pct, 1),
        }
        for row in session.execute(stmt)
    ]


def property_asking(session: MarketSession, property_id: int) -> list[dict[str, Any]]:
    stmt = (
        select(
            Unit.bedrooms,
            func.count().label("available_units"),
            _median(Unit.rent).label("median_asking_rent"),
        )
        .where(Unit.property_id == property_id, Unit.status == "available")
        .group_by(Unit.bedrooms)
        .order_by(Unit.bedrooms)
    )
    return [
        {
            "bedrooms": row.bedrooms,
            "available_units": row.available_units,
            "median_asking_rent": _round(row.median_asking_rent),
        }
        for row in session.execute(stmt)
    ]


def brief_payload(session: MarketSession, prop: Property, week_of: date) -> dict[str, Any]:
    comps = {c["bedrooms"]: c for c in submarket_comps(session, prop.submarket, week_of)}
    rows = []
    for ours in property_asking(session, prop.id):
        comp = comps.get(ours["bedrooms"])
        row: dict[str, Any] = {**ours, "submarket": comp}
        if comp and comp["median_rent"]:
            gap = 100 * (ours["median_asking_rent"] - comp["median_rent"]) / comp["median_rent"]
            row["asking_vs_submarket_pct"] = _round(gap, 1)
        rows.append(row)
    return {
        "property": prop.name,
        "submarket": prop.submarket,
        "week_of": week_of.isoformat(),
        "comp_window_days": COMP_WINDOW_DAYS,
        "by_bedrooms": rows,
    }
