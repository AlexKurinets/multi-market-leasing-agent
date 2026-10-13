"""Replay the eval cases against the live model and the seeded demo data.

    python evals/run_evals.py                 # every case
    python evals/run_evals.py --only phx-pet-deposit dal-pet-deposit

Each case runs in a rolled-back transaction, so the database is left unchanged. This calls the
Anthropic API (a few requests per case) and needs ANTHROPIC_API_KEY or an `ant auth login`
profile. Results are written to evals/results/.
"""

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from leasing.config import get_settings
from leasing.db.scoping import MarketSession
from leasing.db.session import init_db
from leasing.evals import RecordingAgent, load_cases, run_case
from leasing.llm import get_client
from leasing.seed import seed

HERE = Path(__file__).parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=HERE / "cases.jsonl")
    parser.add_argument("--only", nargs="*", help="Case ids to run.")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    init_db(engine)
    seed(sessionmaker(engine, class_=MarketSession))
    client = get_client()

    cases = [c for c in load_cases(args.cases) if not args.only or c.id in args.only]
    outcomes = []
    for case in cases:
        outcome = run_case(case, engine, lambda: RecordingAgent(client, settings))
        outcomes.append(outcome)
        status = "PASS" if outcome.passed else "FAIL"
        print(f"{status}  {case.id:<28} tools: {', '.join(outcome.tool_calls) or '-'}")
        for failure in outcome.failures:
            print(f"      - {failure}")

    passed = sum(o.passed for o in outcomes)
    print(
        f"\n{passed}/{len(outcomes)} passed ({settings.anthropic_model}, "
        f"effort={settings.agent_effort})"
    )
    results = HERE / "results"
    results.mkdir(exist_ok=True)
    path = results / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "model": settings.anthropic_model,
                "effort": settings.agent_effort,
                "passed": passed,
                "total": len(outcomes),
                "cases": [asdict(o) | {"passed": o.passed} for o in outcomes],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"wrote {path}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
