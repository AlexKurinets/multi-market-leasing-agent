from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    pass


class MarketScoped:
    """Mixin for every table whose rows belong to exactly one market.

    Sessions opened with ``market_session(market_id)`` only ever see and write rows for that
    market (see ``leasing.db.scoping``). The filter is applied by the session, not by each
    query, so a forgotten ``WHERE`` clause cannot leak another market's prices or policies.
    """

    @declared_attr
    def market_id(cls) -> Mapped[str]:
        return mapped_column(String(8), ForeignKey("markets.id"), index=True, nullable=False)
