from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import sessionmaker

from leasing.config import get_settings
from leasing.db.base import Base
from leasing.db.scoping import MarketSession


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


@lru_cache
def get_session_factory(url: str | None = None) -> sessionmaker[MarketSession]:
    return sessionmaker(get_engine(url), class_=MarketSession, expire_on_commit=False)


@contextmanager
def market_session(
    market_id: str, factory: sessionmaker[MarketSession] | None = None
) -> Iterator[MarketSession]:
    """A unit of work that can only see and write rows for one market."""
    session = (factory or get_session_factory())(info={"market_id": market_id})
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session(factory: sessionmaker[MarketSession] | None = None) -> Iterator[MarketSession]:
    """An explicit cross-market session for seeding, scheduling and reporting jobs."""
    session = (factory or get_session_factory())(info={"cross_market": True})
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    from leasing.db import models  # noqa: F401  (register tables)

    engine = engine or get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
