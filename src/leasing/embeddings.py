"""Embeddings for policy retrieval.

``HashingEmbedder`` is deterministic and needs no API key, which keeps the demo and the test
suite self-contained. Any hosted embedding model can replace it by implementing ``Embedder``
and matching ``EMBEDDING_DIM`` (or migrating the column).
"""

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

EMBEDDING_DIM = 256

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    (
        "a an and are as at be by can do does for from has have how i in is it its my of on "
        "or per so that the their there this to was we what when with you your"
    ).split()
)


def _tokens(text: str) -> list[str]:
    words = [w for w in _TOKEN.findall(text.lower()) if w not in _STOPWORDS]
    # Crude plural folding so "pets" matches "pet"; a lexical fallback, not a real stemmer.
    return [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words]


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Signed feature hashing over unigrams and bigrams, L2-normalised."""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = _tokens(text)
        features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
        vec = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
