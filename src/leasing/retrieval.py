from dataclasses import dataclass

from sqlalchemy import select

from leasing.db.models import PolicyChunk
from leasing.db.scoping import MarketSession, UnscopedQueryError
from leasing.embeddings import Embedder


@dataclass(frozen=True)
class RetrievedChunk:
    title: str
    body: str
    distance: float


def search_policy_chunks(
    session: MarketSession, embedder: Embedder, query: str, k: int = 3
) -> list[RetrievedChunk]:
    """Exact cosine search over one market's policy text.

    The market filter is applied before ranking (the session adds ``market_id = :scope``), so
    a near-duplicate policy from another market can never outrank the right one. Per-market
    corpora are small enough for an exact scan; at larger sizes use a per-market partial HNSW
    index or pgvector's iterative index scans so filtering does not starve the result set.
    """
    if session.market_id is None:
        raise UnscopedQueryError("Policy search requires a market-scoped session.")
    query_vec = embedder.embed([query])[0]
    distance = PolicyChunk.embedding.cosine_distance(query_vec).label("distance")
    rows = session.execute(select(PolicyChunk, distance).order_by(distance).limit(k)).all()
    return [RetrievedChunk(c.title, c.body, float(d)) for c, d in rows]
