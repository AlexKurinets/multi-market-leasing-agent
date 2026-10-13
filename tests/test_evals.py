"""The eval harness itself, driven by the fake model so it runs without an API key."""

from pathlib import Path

from sqlalchemy import func, select

from leasing.db.models import Lead
from leasing.evals import EvalCase, RecordingAgent, load_cases, run_case, score

from .fakes import FakeAnthropic, call, reply

CASES = Path(__file__).parent.parent / "evals" / "cases.jsonl"


def test_case_file_parses_and_ids_are_unique():
    cases = load_cases(CASES)
    assert len(cases) >= 15
    assert len({c.id for c in cases}) == len(cases)
    assert {c.market for c in cases} == {"PHX", "DAL", "ATL"}


def test_scoring_rules():
    case = EvalCase(
        "x",
        "PHX",
        ["hi"],
        {
            "tools_any": ["get_policy"],
            "tools_none": ["book_tour"],
            "handoff": False,
            "must_include": ["$350"],
            "must_not_include": ["$500"],
        },
    )
    assert score(case, ["Deposit is $350."], ["get_policy"], False) == []
    assert score(case, ["Deposit is $500."], ["book_tour"], True) == [
        "handoff=True, expected False",
        "none of ['get_policy'] called (called ['book_tour'])",
        "book_tour should not be called",
        "missing '$350'",
        "contains '$500'",
    ]


def test_run_case_end_to_end_and_leaves_no_rows(engine, settings):
    case = next(c for c in load_cases(CASES) if c.id == "phx-pet-deposit")
    client = FakeAnthropic(
        call("get_policy", {"topic": "pets"}),
        reply("It's a $350 refundable deposit per pet, plus $35 a month."),
    )
    outcome = run_case(case, engine, lambda: RecordingAgent(client, settings))
    assert outcome.passed, outcome.failures
    assert outcome.tool_calls == ["get_policy"]
    with engine.connect() as conn:  # the case ran in a rolled-back transaction
        assert conn.execute(select(func.count()).select_from(Lead.__table__)).scalar() == 0


def test_guardrail_case_is_scored_as_a_handoff(engine, settings):
    case = next(c for c in load_cases(CASES) if c.id == "accommodation-wheelchair")
    outcome = run_case(case, engine, lambda: RecordingAgent(FakeAnthropic(), settings))
    assert outcome.passed, outcome.failures
