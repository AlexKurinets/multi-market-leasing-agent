from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from leasing.workflows.types import (
        GENERATE_BRIEFS_ACTIVITY,
        LIST_MARKETS_ACTIVITY,
        BriefsRequest,
    )


@workflow.defn
class WeeklyBriefsWorkflow:
    """Runs on a Temporal schedule: one brief per property per week, market by market."""

    @workflow.run
    async def run(self) -> int:
        today = workflow.now().date()
        week_of = today - timedelta(days=today.weekday())
        markets = await workflow.execute_activity(
            LIST_MARKETS_ACTIVITY,
            start_to_close_timeout=timedelta(seconds=30),
            result_type=list[str],
        )
        written = 0
        for market_id in markets:
            written += await workflow.execute_activity(
                GENERATE_BRIEFS_ACTIVITY,
                BriefsRequest(market_id, week_of.isoformat()),
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
                result_type=int,
            )
        return written
