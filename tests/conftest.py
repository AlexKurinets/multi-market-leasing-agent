import os
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import sessionmaker

from leasing.config import Settings
from leasing.db.base import Base
from leasing.db.models import Property
from leasing.db.scoping import MarketSession
from leasing.db.session import admin_session, init_db
from leasing.seed import seed

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/leasing_test"
)
SEED_DAY = date(2026, 10, 12)  # a Monday
# 15:00 UTC: 08:00 in Phoenix, 10:00 in Dallas, 11:00 in Atlanta
NOW = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    init_db(engine)
    seed(sessionmaker(engine, class_=MarketSession), today=SEED_DAY)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[MarketSession]]:
    """Each test runs inside one outer transaction that is rolled back afterwards. Commits in
    the code under test become savepoints."""
    connection = engine.connect()
    outer = connection.begin()
    yield sessionmaker(
        bind=connection,
        class_=MarketSession,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    outer.rollback()
    connection.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, cadence_hours=[24, 72, 168])


@pytest.fixture
def property_ids(factory: sessionmaker[MarketSession]) -> dict[str, int]:
    with admin_session(factory) as session:
        return {p.name: p.id for p in session.scalars(select(Property))}
