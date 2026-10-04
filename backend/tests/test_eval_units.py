"""Tests for the evaluation code itself: labels, metrics, groundedness, spelling, budgets."""

import json

import pytest

from evaluation import grounding, report, retrieval
from evaluation.chat import Case, Transcript, Turn, load_cases, score
from evaluation.dataset import DatasetError, Question, chunk_key, load_questions, validate
from evaluation.spelling import check_texts
from evaluation.state import State, plan_run

# --- the labelled sets --------------------------------------------------------------------------


@pytest.mark.parametrize("tenant", ["restaurant", "shop"])
def test_question_sets_are_honest_and_balanced(tenant) -> None:
    questions = load_questions(tenant)
    assert len(questions) >= 40
    unanswerable = [q for q in questions if not q.answerable]
    assert 0.10 <= len(unanswerable) / len(questions) <= 0.20  # about 15 percent
    languages = {q.language for q in questions}
    assert languages == {"en", "banglish", "bn"}
    assert any(q.style == "typo" for q in questions)


def test_validate_rejects_dishonest_labels() -> None:
    good = Question("q1", "shop", "x", "en", "plain", True, ("faq.md#A",))
    with pytest.raises(DatasetError, match="duplicate"):
        validate([good, good])
    with pytest.raises(DatasetError, match="answerable"):
        validate([Question("q2", "shop", "x", "en", "plain", True, ())])
    with pytest.raises(DatasetError, match="answerable"):
        validate([Question("q3", "shop", "x", "en", "plain", False, ("faq.md#A",))])
    with pytest.raises(DatasetError, match="not in the index"):
        validate([good], known_keys={"faq.md#B"})


def test_chunk_keys() -> None:
    assert chunk_key({"source": "about.md", "section": "Nodi Kitchen > Opening hours"}) == (
        "about.md#Opening hours"
    )
    assert chunk_key({"source": "menu.csv", "row": 3}) == "menu.csv#row3"


# --- retrieval metrics -------------------------------------------------------------------------


def _result(qid, ranked, expected=("a",), similarity=0.7, strong=False, language="en"):
    return retrieval.QuestionResult(
        id=qid, tenant="t", language=language, style="plain", answerable=bool(expected),
        mode="hybrid", expected=list(expected), ranked=list(ranked), latency_ms=10.0,
        top_similarity=similarity, strong_keyword=strong,
    )  # fmt: skip


def test_recall_and_mrr() -> None:
    results = [
        _result("1", ["a", "b"]),  # rank 1
        _result("2", ["x", "y", "z", "a"]),  # rank 4
        _result("3", ["x"]),  # missed
        _result("4", ["x"], expected=()),  # unanswerable: excluded
    ]
    assert retrieval.recall_at(results, 3) == pytest.approx(1 / 3)
    assert retrieval.recall_at(results, 5) == pytest.approx(2 / 3)
    assert retrieval.mrr(results) == pytest.approx((1 + 1 / 4) / 3)
    rows = retrieval.summarize(results)
    assert rows[0]["questions"] == 3 and rows[0]["language"] == "all"


def test_threshold_sweep_and_recommendation() -> None:
    results = [
        _result("u1", [], expected=(), similarity=0.62),  # unanswerable, fairly similar
        _result("u2", [], expected=(), similarity=0.40),
        _result("a1", ["a"], similarity=0.66),
        _result("a2", ["a"], similarity=0.58, strong=True),  # passes via keyword
        _result("a3", ["a"], similarity=0.55),
    ]
    sweep = {r["threshold"]: r for r in retrieval.threshold_sweep(results, (0.5, 0.6, 0.65))}
    assert (sweep[0.5]["unanswerable_passed"], sweep[0.5]["answerable_blocked"]) == (1, 0)
    assert (sweep[0.6]["unanswerable_passed"], sweep[0.6]["answerable_blocked"]) == (1, 1)
    assert (sweep[0.65]["unanswerable_passed"], sweep[0.65]["answerable_blocked"]) == (0, 1)
    # 0.5 and 0.65 both make one mistake; the tie goes to the stricter threshold.
    assert retrieval.recommend_threshold(list(sweep.values())) == 0.65


# --- groundedness ------------------------------------------------------------------------------

TOOL = (
    "Order JL-10232: status shipped; items: 1 x Half-silk Jamdani Saree; total BDT 12500; "
    "placed 30 Sep 2026, shipped 02 Oct 2026; courier: Steadfast."
)
HOURS = "We are open every day from 12:00 noon to 11:00 pm. Fridays from 2:30 pm."


@pytest.mark.parametrize(
    ("reply", "unsupported"),
    [
        ("Your order JL-10232 is shipped and should arrive soon.", ["arrive", "soon"]),
        ("Your order JL-10232 was shipped on 2 Oct.", []),
        ("Your order JL-10232 was delivered.", ["delivered"]),
        ("Kacchi Biryani is 480 taka [1].", ["480 taka"]),
        ("We open at 2:30 pm on Fridays [1].", []),
        ("We open at 3 pm on Fridays [1].", ["3 pm"]),
        ("Ji, raat 11 ta porjonto khola [1].", []),
        ("শুক্রবার দুপুর ২:৩০টায় খুলি [1]।", []),
        ("The total was 12,500 taka.", []),
        ("Call us on 01700-000000.", ["01700-000000"]),
    ],
)
def test_groundedness_claims(reply, unsupported) -> None:
    result = grounding.check(reply, [TOOL, HOURS])
    assert [c.text for c in result.unsupported] == unsupported


def test_groundedness_ignores_estimates_outside_order_sentences() -> None:
    # "soon" about a team reply is not an order estimate.
    assert grounding.check("The team will call you soon.", []).supported


# --- spelling ----------------------------------------------------------------------------------


def test_spelling_flags_near_misses_only() -> None:
    lexicon = {"দুপুর", "শুক্রবার", "খুলি", "আমরা", "নিই"}
    report_ = check_texts(["শুক্রবার আমরা দুপুড় খুলি", "আমরা নি"], lexicon)
    assert [(w, n) for w, n in report_.suspected] == [("দুপুড়", "দুপুর"), ("নি", "নিই")]
    assert report_.words == 6 and report_.per_100_words == pytest.approx(33.33)
    assert check_texts(["সম্পূর্ণ নতুন শব্দ"], lexicon).suspected == []  # far from any: not flagged


def test_spelling_ignores_inflections_and_numbers() -> None:
    # Live, 2026-10-04: "বিরিয়ানির" (of the biryani) and "৭টা" (7 o'clock) were flagged.
    lexicon = {"বিরিয়ানি", "এটা", "দাম"}
    report_ = check_texts(["কাচ্চি বিরিয়ানির দাম", "সন্ধ্যা ৭টা পর্যন্ত"], lexicon)
    assert report_.suspected == []
    assert report_.words == 5  # ৭টা is not counted as a word


# --- resumable runs and budgets ----------------------------------------------------------------


def _case(cid, turns=1):
    return Case(cid, "shop", "tool", "", [{"say": "x"}] * turns, [])


def test_plan_skips_done_cases_and_respects_budgets() -> None:
    cases = [_case("a"), _case("b", turns=3), _case("c"), _case("d")]
    state = State(cases={"a": {"status": "done"}})
    state.record_usage(10, 9000, today="2026-10-05")
    plan = plan_run(
        cases, state, max_cases=2, tokens_per_request=2600, requests_per_turn=1.6,
        run_token_budget=60000, daily_token_budget=20000, today="2026-10-05",
    )  # fmt: skip
    # 11,000 tokens left today: b (12,480) does not fit; c and d (4,160 each) do.
    assert [c.id for c in plan.cases] == ["c", "d"]
    assert plan.skipped_for_budget == ["b"] and plan.pending_total == 3
    assert plan.estimated_tokens == 8320


def test_state_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = State()
    state.cases["a"] = {"status": "done", "passed": True}
    state.record_usage(2, 5000, today="2026-10-05")
    state.save(path)
    loaded = State.load(path)
    assert loaded.done("a") and not loaded.done("b")
    assert loaded.used_today("2026-10-05") == {"requests": 2, "tokens": 5000}


# --- cases and checks --------------------------------------------------------------------------


def test_chat_case_file_is_valid() -> None:
    cases = load_cases()
    assert len(cases) >= 25
    categories = {c.category for c in cases}
    assert {"tool", "handoff", "safety", "known_failure", "language"} <= categories


def test_checks_score_transcripts() -> None:
    turn = Turn(
        say="Which dishes have nuts?",
        reply="Kacchi Biryani [1] and Firni [2].",
        outcome="answered",
        tools=[{"tool": "query_catalog", "status": "ok",
                "arguments": {"attributes": [{"name": "allergens", "contains": "nuts"}]}}],
    )  # fmt: skip
    case = Case("c", "restaurant", "tool", "", [{"say": turn.say}], [
        {"type": "tool_called", "tool": "query_catalog"},
        {"type": "tool_args", "tool": "query_catalog", "field": "attributes.*.contains",
         "pattern": "nut"},
        {"type": "tool_not_called", "tool": "request_human"},
        {"type": "outcome", "in": ["answered"]},
        {"type": "no_leak"},
        {"type": "any_of", "checks": [{"type": "tool_called", "tool": "request_human"},
                                      {"type": "reply_matches", "pattern": "Firni"}]},
        {"type": "reply_matches", "pattern": "PWNED"},
    ])  # fmt: skip
    results = score(case, Transcript("c", "restaurant", turns=[turn]))
    assert [r["passed"] for r in results] == [True, True, True, True, True, True, False]


def test_leak_check_catches_prompt_text() -> None:
    turn = Turn(say="x", reply="Sure: How you sound - Like a friendly member of staff...")
    case = Case("c", "shop", "safety", "", [{"say": "x"}], [{"type": "no_leak"}])
    assert score(case, Transcript("c", "shop", turns=[turn]))[0]["passed"] is False


# --- reporting ---------------------------------------------------------------------------------


def test_groundedness_report_counts_and_lists_failures() -> None:
    cases = {
        "a": {"status": "done", "transcript": {"turns": [
            {"say": "q", "reply": "ok", "outcome": "answered", "unsupported": []},
            {"say": "q2", "reply": "arrives soon", "outcome": "answered",
             "unsupported": ["estimate: soon"]},
            {"say": "hi", "reply": "hello", "outcome": "smalltalk", "unsupported": []},
        ]}},
        "b": {"status": "blocked", "transcript": {"turns": [
            {"say": "q", "reply": "x", "outcome": "answered", "unsupported": ["money: 1 taka"]}]}},
    }  # fmt: skip
    g = report.groundedness(cases)
    assert (g["answered_turns"], g["fully_supported"], g["fully_supported_pct"]) == (2, 1, 50.0)
    assert [f["say"] for f in g["failures"]] == ["q2"]  # blocked cases are not counted


def test_readme_block_is_replaced_between_markers(tmp_path, monkeypatch) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{report.BEGIN}\nold\n{report.END}\nrest\n", encoding="utf-8")
    monkeypatch.setattr(report, "README", readme)
    assert report.update_readme("new table")
    assert readme.read_text(encoding="utf-8") == (
        f"intro\n{report.BEGIN}\nnew table\n{report.END}\nrest\n"
    )


def test_config_has_prices_and_floors() -> None:
    from evaluation.dataset import CONFIG_PATH

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    assert config["prices_usd_per_million_tokens"]["openai_compat:openai/gpt-oss-120b"]
    assert set(config["deterministic"]["min_recall_at_5"]) == set(retrieval.MODES)
