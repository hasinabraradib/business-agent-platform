"""Reciprocal Rank Fusion (Cormack, Clarke & Buettcher, 2009).

score(d) = sum over rankings r containing d of 1 / (k + rank_r(d)), with rank starting at 1.
It needs only ranks, not scores, so it can merge rankings whose scores are on unrelated scales
(cosine similarity vs. keyword weights). k damps the influence of the very top ranks; 60 is the
value from the paper and a common default.
"""

from collections.abc import Hashable, Sequence


def reciprocal_rank_fusion[K: Hashable](
    rankings: Sequence[Sequence[K]], k: int = 60
) -> list[tuple[K, float]]:
    """Fuse rankings (best first) into one list of (item, score), best first.

    Ties keep the order in which items were first seen, scanning rankings rank by rank.
    """
    if k < 0:
        raise ValueError("k must be >= 0")
    scores: dict[K, float] = {}
    first_seen: dict[K, tuple[int, int]] = {}
    for list_index, ranking in enumerate(rankings):
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position + 1)
            first_seen.setdefault(item, (position, list_index))
    return sorted(scores.items(), key=lambda pair: (-pair[1], first_seen[pair[0]]))
