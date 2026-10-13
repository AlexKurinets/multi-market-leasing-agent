"""Session-level market isolation.

Every ORM statement that touches a ``MarketScoped`` table gets ``market_id = :scope`` added
through ``with_loader_criteria``. A session without a scope refuses to read or write scoped
tables unless it was explicitly opened as cross-market (seeding, nightly jobs, admin views).

The filter covers ORM statements. ``text()`` SQL and Core statements against raw ``Table``
objects bypass it, so application code uses ORM entities only; Postgres row-level security is
the next layer if that guarantee has to hold against arbitrary SQL.
"""

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from leasing.db.base import MarketScoped


class UnscopedQueryError(RuntimeError):
    """Raised when a session without a market scope queries market-scoped data."""


class CrossMarketWriteError(RuntimeError):
    """Raised when a scoped session tries to write a row that belongs to another market."""


class MarketSession(Session):
    @property
    def market_id(self) -> str | None:
        return self.info.get("market_id")

    @property
    def cross_market(self) -> bool:
        return bool(self.info.get("cross_market"))


@event.listens_for(MarketSession, "do_orm_execute")
def _apply_market_scope(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    # Lazy and deferred loads inherit the criteria of the statement that loaded the parent.
    if state.is_column_load or state.is_relationship_load:
        return

    session = state.session
    market_id = session.info.get("market_id")
    if market_id is None:
        if session.info.get("cross_market") or not _touches_scoped(state):
            return
        raise UnscopedQueryError(
            "Query touches market-scoped tables but the session has no market scope. "
            "Open it with market_session(market_id) or admin_session()."
        )
    # Attached to every statement, not only ones whose entities we can detect: shapes such as
    # select(func.count()).select_from(Unit) report no entities but are still filtered.
    state.statement = state.statement.options(
        with_loader_criteria(
            MarketScoped, lambda cls: cls.market_id == market_id, include_aliases=True
        )
    )


def _touches_scoped(state: ORMExecuteState) -> bool:
    mappers = list(state.all_mappers)
    if state.bind_mapper is not None:
        mappers.append(state.bind_mapper)
    return any(issubclass(m.class_, MarketScoped) for m in mappers)


@event.listens_for(MarketSession, "before_flush")
def _guard_market_writes(session: Session, flush_context, instances) -> None:
    market_id = session.info.get("market_id")
    cross_market = session.info.get("cross_market")
    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, MarketScoped):
            continue
        if market_id is None:
            if cross_market:
                continue
            raise UnscopedQueryError(
                f"Cannot write {type(obj).__name__} from a session without a market scope."
            )
        if obj.market_id is None:
            obj.market_id = market_id
        elif obj.market_id != market_id:
            raise CrossMarketWriteError(
                f"{type(obj).__name__} belongs to market {obj.market_id!r}, "
                f"but this session is scoped to {market_id!r}."
            )
