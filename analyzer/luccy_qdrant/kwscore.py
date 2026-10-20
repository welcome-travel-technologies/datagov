"""luccy's stemmed keyword score, bit for bit (retrieve.KwStem.scores, pinned in hooks.py).

luccy, per query term t in the order the Qdrant/bm25 query encoder returns it, adds
    float32(ln((N - n_t + 0.5) / (n_t + 0.5) + 1)) * value_d(t)
to a float32 accumulator, with N the bundle's chunk count, n_t the chunks holding t and
value_d(t) the chunk's float32 sparse value. The idf is computed in float64 and rounded to
float32 when numpy multiplies it into the float32 values; every product and every sum is a
float32 operation. `score` repeats exactly those operations for one chunk.

Qdrant's own sparse IDF modifier computes the same formula in float32 (a different rounding
of ln), so its scores differ from luccy's by about one float32 ulp; tests/test_idf.py measures
it. The keyword collection therefore stores raw values without the modifier, a query carries
luccy's idf as its term weights (Qdrant's ranking then equals luccy's up to float rounding),
and the candidates Qdrant returns are rescored here exactly.
"""
from __future__ import annotations

import hashlib
import math

import numpy as np

# A chunk Qdrant did not return can only enter luccy's top-k if its luccy score reaches the
# k-th score; Qdrant's float32 dot product and luccy's sum differ by a few ulps at most
# (relative 1e-6 for the term counts a query has), so this margin is generous.
REL_MARGIN = 1e-5
ABS_MARGIN = 1e-6


def idf32(n_chunks: int, df: int) -> np.float32:
    return np.float32(math.log((n_chunks - df + 0.5) / (df + 0.5) + 1))


def score(terms: list[int], values: dict[int, np.float32], idf: dict[int, np.float32]) -> np.float32:
    """luccy's score for one chunk: `terms` in query order (duplicates repeat, as in luccy),
    `values` the chunk's float32 sparse values, `idf` the float32 idf of every term the
    corpus holds."""
    s = np.float32(0.0)
    for t in terms:
        v = values.get(t)
        w = idf.get(t)
        if v is None or w is None:
            continue
        s = np.float32(s + np.float32(w * v))
    return s


def certain(qdrant_floor: float, kth_score: float) -> bool:
    """True when no chunk scored at or below `qdrant_floor` by Qdrant can reach `kth_score`."""
    return qdrant_floor * (1 + REL_MARGIN) + ABS_MARGIN < kth_score


class Signature:
    """luccy's corpus_signature, fed chunk by chunk in row order (retrieve.corpus_signature)."""

    def __init__(self):
        self.h = hashlib.sha1()

    def add(self, page_id: str, index_text: str) -> None:
        self.h.update(page_id.encode()); self.h.update(b"\0")
        self.h.update(index_text.encode()); self.h.update(b"\0")

    def hexdigest(self) -> str:
        return self.h.hexdigest()[:16]
