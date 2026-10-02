import pytest

from app.retrieval.fusion import reciprocal_rank_fusion


def test_rrf_arithmetic_on_hand_built_rankings() -> None:
    vector = ["a", "b", "c"]
    keyword = ["c", "a", "d"]
    fused = dict(reciprocal_rank_fusion([vector, keyword], k=60))
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fused["b"] == pytest.approx(1 / 62)
    assert fused["c"] == pytest.approx(1 / 63 + 1 / 61)
    assert fused["d"] == pytest.approx(1 / 63)
    order = [item for item, _ in reciprocal_rank_fusion([vector, keyword], k=60)]
    assert order == ["a", "c", "b", "d"]


def test_rrf_constant_changes_weight_of_top_ranks() -> None:
    # With k=0, being first in one list (1/1) beats being second in both (1/2 + 1/2 = 1.0)
    # only by tie-break; with k=60 appearing in both lists clearly wins.
    rankings = [["x", "both"], ["y", "both"]]
    k0 = dict(reciprocal_rank_fusion(rankings, k=0))
    assert k0["x"] == pytest.approx(1.0)
    assert k0["both"] == pytest.approx(1.0)
    k60 = reciprocal_rank_fusion(rankings, k=60)
    assert k60[0][0] == "both"
    assert k60[0][1] == pytest.approx(2 / 62)


def test_rrf_handles_empty_and_single_rankings() -> None:
    assert reciprocal_rank_fusion([], k=60) == []
    assert reciprocal_rank_fusion([[], []], k=60) == []
    assert [i for i, _ in reciprocal_rank_fusion([["p", "q"], []], k=60)] == ["p", "q"]


def test_rrf_rejects_negative_k() -> None:
    with pytest.raises(ValueError):
        reciprocal_rank_fusion([["a"]], k=-1)
