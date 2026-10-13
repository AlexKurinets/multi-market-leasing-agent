"""One durable workflow per lead.

It sends the first reply, answers every prospect message, and runs the follow-up cadence when
the prospect goes quiet. State lives in Temporal, so the cadence survives deploys and worker
restarts, and the workflow ends itself the moment a tour is booked, a person takes over or the
prospect opts out.
"""

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from leasing.workflows.types import (
        RESPOND_ACTIVITY,
        ConversationInput,
        RespondRequest,
        RespondResult,
    )

# After the last follow-up, keep listening this long for a late reply before closing.
LISTEN_AFTER_FINAL_FOLLOW_UP = timedelta(days=14)

_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=6,
)


@workflow.defn
class LeadConversationWorkflow:
    def __init__(self) -> None:
        self._inbound: list[int] = []
        self._stop_reason: str | None = None
        self._turn = 0
        self._follow_ups_sent = 0

    @workflow.signal
    def inbound_message(self, message_id: int) -> None:
        self._inbound.append(message_id)

    @workflow.signal
    def stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason

    @workflow.query
    def status(self) -> dict[str, object]:
        return {
            "stop_reason": self._stop_reason,
            "follow_ups_sent": self._follow_ups_sent,
            "pending_inbound": len(self._inbound),
        }

    @workflow.run
    async def run(self, inp: ConversationInput) -> str:
        if inp.greet:
            await self._execute(inp, "new_lead")
        total = len(inp.cadence_hours)
        while self._stop_reason is None:
            if self._follow_ups_sent < total:
                wait = timedelta(hours=inp.cadence_hours[self._follow_ups_sent])
            else:
                wait = LISTEN_AFTER_FINAL_FOLLOW_UP
            if await self._wait_for_prospect(wait):
                if self._inbound:
                    ids, self._inbound = self._inbound, []
                    await self._execute(inp, "inbound", message_ids=ids)
                    self._follow_ups_sent = 0  # the prospect re-engaged: restart the cadence
                continue
            if self._follow_ups_sent >= total:
                return "no_response"
            self._follow_ups_sent += 1
            await self._follow_up(inp, self._follow_ups_sent, total)
        return self._stop_reason

    async def _wait_for_prospect(self, timeout: timedelta) -> bool:
        """True if a message or stop signal arrived, False if ``timeout`` elapsed first."""
        try:
            await workflow.wait_condition(
                lambda: bool(self._inbound) or self._stop_reason is not None, timeout=timeout
            )
        except TimeoutError:
            return False
        return True

    async def _follow_up(self, inp: ConversationInput, number: int, total: int) -> None:
        key = self._next_key()  # reused while deferred, so a deferral never double-sends
        while self._stop_reason is None:
            result = await self._execute(
                inp,
                "follow_up",
                key=key,
                follow_up_number=number,
                follow_up_total=total,
            )
            if result.status != "deferred" or result.retry_at is None:
                return
            delay = datetime.fromisoformat(result.retry_at) - workflow.now()
            if await self._wait_for_prospect(max(delay, timedelta(seconds=1))):
                return  # they wrote in during quiet hours; the main loop answers that instead

    async def _execute(
        self,
        inp: ConversationInput,
        trigger: str,
        *,
        key: str | None = None,
        message_ids: list[int] | None = None,
        follow_up_number: int = 0,
        follow_up_total: int = 0,
    ) -> RespondResult:
        request = RespondRequest(
            market_id=inp.market_id,
            lead_id=inp.lead_id,
            trigger=trigger,
            idempotency_key=key or self._next_key(),
            message_ids=message_ids or [],
            follow_up_number=follow_up_number,
            follow_up_total=follow_up_total,
        )
        result = await workflow.execute_activity(
            RESPOND_ACTIVITY,
            request,
            result_type=RespondResult,
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=_RETRY,
        )
        if result.tour_booked:
            self.stop("tour_booked")
        elif result.handed_off:
            self.stop("handed_off")
        elif result.status == "skipped" and result.detail in ("human_owned", "no_consent"):
            self.stop(result.detail)
        return result

    def _next_key(self) -> str:
        self._turn += 1
        info = workflow.info()
        return f"{info.workflow_id}/{info.run_id}/turn-{self._turn}"
