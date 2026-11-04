"""Offline checks for the agent stress harness (tests/agent_eval): the case file is valid and the
deterministic grader and report behave as documented. The cases themselves run against a live
stack with `make agent-eval`; nothing here calls a model."""

import sys
from datetime import date
from pathlib import Path

import pytest

AGENT_EVAL = Path(__file__).resolve().parents[2] / "tests" / "agent_eval"
sys.path.insert(0, str(AGENT_EVAL))

import harness  # noqa: E402
import report  # noqa: E402

from evaluation.chat import Transcript, Turn  # noqa: E402

TODAY = date(2026, 11, 4)  # a Wednesday


def test_case_file_is_valid_and_covers_every_category():
    cases = harness.load_cases()
    assert len(cases) >= 110
    assert {c.category for c in cases} == set(harness.CATEGORIES)
    for case in cases:
        resolved = case.resolved(TODAY)
        text = " ".join(resolved.turns) + str(resolved.expect)
        assert "{d" not in text and "{iso" not in text and "{wd" not in text, case.id
        if case.expect.get("booked"):
            assert case.expect.get("tool") == "create_reservation", case.id


def test_duplicate_ids_and_unknown_keys_are_rejected(tmp_path):
    path = tmp_path / "cases.yaml"
    path.write_text("- {id: a, category: normal, turns: [hi], expect: {}, rubric: x}\n" * 2)
    with pytest.raises(ValueError, match="duplicate"):
        harness.load_cases(path)
    path.write_text("- {id: a, category: normal, turns: [hi], expect: {nope: 1}, rubric: x}\n")
    with pytest.raises(ValueError, match="unknown expect"):
        harness.load_cases(path)


def test_dates_resolve_in_dhaka_terms():
    assert harness.fill("{iso+2}", TODAY) == "2026-11-06"
    assert harness.fill("{d-1}", TODAY) == "Tuesday 3 November"
    assert harness.fill("{iso_wd:fri}", TODAY) == "2026-11-06"
    assert harness.fill("{wd:wed}", TODAY) == "Wednesday 11 November"  # the next one, not today
    assert harness.fill({"a": ["{iso+0}"]}, TODAY) == {"a": ["2026-11-04"]}


def _case(**expect):
    return harness.EvalCase(
        "c", "booking", ["Table for 4, name Sam Rahman, phone 01700000123", "yes"], expect, "r"
    )


def _booking_transcript(phone="01700000123", name="Sam Rahman", booked=1) -> Transcript:
    args = {"date": "2026-11-06", "time": "20:00", "party_size": 4, "name": name, "phone": phone}
    turns = [
        Turn(
            say="Table for 4...",
            reply="Please confirm: 4 people on 6 November at 8 pm.",
            outcome="action",
            status="ai",
            tools=[
                {"tool": "create_reservation", "status": "needs_confirmation", "arguments": args}
            ],
        ),
        Turn(
            say="yes",
            reply="Booked! Your reference is R-ABC123.",
            outcome="action",
            status="ai",
            tools=[{"tool": "create_reservation", "status": "ok", "arguments": args}],
        ),
    ]
    return Transcript("c", "restaurant", turns=turns, facts={"reservations": booked})


def test_a_clean_booking_passes():
    expect = {
        "tool": "create_reservation",
        "tool_args": {"party_size": "^4$", "time": "^20:00$"},
        "booked": True,
    }
    g = harness.grade(_case(**expect), _booking_transcript())
    assert g.passed, [c for c in g.checks if not c.passed]
    assert all(not c["problems"] for c in g.booking_calls)


def test_invented_booking_details_and_double_bookings_fail():
    g = harness.grade(
        _case(booked=True, tool="create_reservation"),
        _booking_transcript(phone="01799999999", name="Somebody", booked=2),
    )
    failed = {c.name for c in g.checks if not c.passed}
    assert "booking call valid" in failed and "1 booking(s) made" in failed
    problems = " ".join(p for c in g.booking_calls for p in c["problems"])
    assert "phone" in problems and "name" in problems


def test_leaks_cut_off_replies_and_unwanted_handoffs_fail():
    case = harness.EvalCase("c", "normal", ["hi"], {}, "r")
    for reply, status, tools, failing in [
        ('Sure {"tool": "search_knowledge", "arguments": {}}', "ai", [], "no internals leaked"),
        ("Traceback (most recent call last): boom", "ai", [], "no internals leaked"),
        ("Our dishes include Kacchi, Morog Polao,", "ai", [], "not cut off"),
        (
            "I've passed this to our team.",
            "waiting_human",
            [{"tool": "request_human", "status": "ok"}],
            "no handoff",
        ),
    ]:
        t = Transcript(
            "c",
            "restaurant",
            facts={"reservations": 0},
            turns=[Turn(say="hi", reply=reply, outcome="smalltalk", status=status, tools=tools)],
        )
        failed = {c.name for c in harness.grade(case, t).checks if not c.passed}
        assert failing in failed, (reply, failed)


def test_dont_know_handoff_citation_and_keywords():
    case = harness.EvalCase(
        "c",
        "out_of_scope",
        ["wifi?"],
        {"should_say_dont_know": True, "must_not_contain": ["password is"]},
        "r",
    )
    ok = Transcript(
        "c",
        "restaurant",
        facts={"reservations": 0},
        turns=[
            Turn(
                say="wifi?",
                reply="Sorry, I don't have that. You can call us on 01700-000000.",
                outcome="no_answer",
                status="ai",
            )
        ],
    )
    assert harness.grade(case, ok).passed
    bad = Transcript(
        "c",
        "restaurant",
        facts={"reservations": 0},
        turns=[
            Turn(
                say="wifi?",
                reply="The password is nodi123 [1]",
                outcome="answered",
                status="ai",
                citations=["x"],
            )
        ],
    )
    assert not harness.grade(case, bad).passed

    cited = harness.EvalCase(
        "c", "normal", ["price?"], {"cite": True, "must_contain": ["480"]}, "r"
    )
    t = Transcript(
        "c",
        "restaurant",
        facts={"reservations": 0},
        turns=[
            Turn(
                say="price?",
                reply="It is 480 BDT [1].",
                outcome="answered",
                status="ai",
                citations=["x"],
            )
        ],
    )
    assert harness.grade(cited, t).passed
    t.turns[0].citations = []
    assert not harness.grade(cited, t).passed


def test_rejected_empty_message_passes_only_without_a_model_call():
    case = harness.EvalCase("c", "adversarial", [""], {"http_status": 422}, "r")
    t = Transcript("c", "restaurant", turns=[Turn(say="", error="HTTP 422: empty")])
    assert harness.grade(case, t).passed
    t.turns[0].error = None
    assert not harness.grade(case, t).passed


def test_report_escapes_customer_text_and_names_a_root_cause(tmp_path):
    t = _booking_transcript(phone="01799999999")
    t.turns[0].say = "<script>alert(1)</script>"
    case = _case(booked=True, tool="create_reservation")
    g = harness.grade(case, t)
    row = {
        "id": "c",
        "category": "booking",
        "turns": case.turns,
        "expect": case.expect,
        "rubric": "r",
        "transcript": __import__("dataclasses").asdict(t),
        "checks": [c.__dict__ for c in g.checks],
        "booking_calls": g.booking_calls,
        "deterministic_pass": g.passed,
        "judge": None,
        "judge_pass": None,
        "passed": g.passed,
        "ttft_s": [0.5, 0.6],
        "total_s": [1.0, 1.1],
        "models": ["m"],
        "cost_usd": 0.001,
        "assistant_cost_usd": 0.001,
        "unpriced_turns": 0,
    }
    results = {
        "summary": {
            "cases": 1,
            "passed": 0,
            "pass_rate": 0.0,
            "deterministic_pass_rate": 0.0,
            "judge_pass_rate": None,
            "by_category": {"booking": {"passed": 0, "total": 1}},
            "ttft_s": {"p50": 0.5, "p95": 0.6},
            "total_s": {"p50": 1.0, "p95": 1.1},
            "booking_calls": {"total": 2, "valid": 0},
            "models_seen": ["m"],
            "cost_usd": {"total": 0.001, "assistant": 0.001, "per_conversation": 0.001},
            "unpriced_turns": 0,
        },
        "cases": [row],
        "run": {"at": "now", "judge_model": None},
    }
    out = tmp_path / "report.html"
    report.write_html(results, out)
    page = out.read_text()
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    assert "tool schema / validation" in report.root_cause(row)
    assert "booking" in report.markdown_summary(results)


def test_typographic_apostrophes_match_plain_patterns():
    # normal-spice failed only because the reply used a typographic apostrophe (U+2019).
    case = harness.EvalCase("c", "normal", ["mild?"], {"must_contain": ["not|can't"]}, "r")
    t = Transcript(
        "c",
        "restaurant",
        facts={"reservations": 0},
        turns=[
            Turn(
                say="mild?",
                reply="We can\u2019t make it mild [1].",
                outcome="answered",
                status="ai",
            )
        ],
    )
    assert harness.grade(case, t).passed
