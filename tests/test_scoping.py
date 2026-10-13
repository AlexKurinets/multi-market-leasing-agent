import pytest
from sqlalchemy import func, select, update

from leasing.db import CrossMarketWriteError, UnscopedQueryError
from leasing.db.models import Policy, Property, Unit
from leasing.db.session import admin_session, market_session


def test_scoped_session_only_sees_its_market(factory):
    with market_session("PHX", factory) as session:
        markets = set(session.scalars(select(Unit.market_id)))
        names = set(session.scalars(select(Property.name)))
    assert markets == {"PHX"}
    assert names == {"Saguaro Flats", "Rio Salado Commons"}


def test_joins_and_aggregates_are_scoped(factory):
    with market_session("DAL", factory) as session:
        joined = session.execute(select(Unit.id, Property.name).join(Property)).all()
        count = session.scalar(select(func.count()).select_from(Unit))
    assert {name for _, name in joined} == {"Cedar Row Residences", "Elm Street Lofts"}
    assert count == 16


def test_another_markets_row_is_invisible_by_primary_key(factory, property_ids):
    with market_session("PHX", factory) as session:
        assert session.get(Property, property_ids["Cedar Row Residences"]) is None
        assert session.get(Property, property_ids["Saguaro Flats"]) is not None


def test_same_topic_resolves_per_market(factory):
    amounts = {}
    for market in ("PHX", "DAL", "ATL"):
        with market_session(market, factory) as session:
            amounts[market] = session.scalar(select(Policy.amount).where(Policy.topic == "pets"))
    assert amounts == {"PHX": 350, "DAL": 500, "ATL": 400}


def test_unscoped_session_refuses_market_data(factory):
    session = factory()
    try:
        with pytest.raises(UnscopedQueryError):
            session.scalars(select(Unit)).all()
        with pytest.raises(UnscopedQueryError):
            session.scalar(select(func.count()).select_from(Unit))
    finally:
        session.close()


def test_writes_take_the_session_market_and_reject_others(factory, property_ids):
    with market_session("ATL", factory) as session:
        policy = Policy(topic="storage", summary="Storage lockers are $25 per month.")
        session.add(policy)
        session.flush()
        assert policy.market_id == "ATL"

    with pytest.raises(CrossMarketWriteError), market_session("ATL", factory) as session:
        session.add(Policy(market_id="PHX", topic="storage", summary="x"))
        session.flush()


def test_bulk_update_is_scoped(factory):
    with market_session("PHX", factory) as session:
        session.execute(update(Unit).values(status="offline"))
    with admin_session(factory) as session:
        statuses = dict(
            session.execute(
                select(Unit.market_id, func.count())
                .where(Unit.status == "offline")
                .group_by(Unit.market_id)
            ).all()
        )
    assert statuses == {"PHX": 16}
