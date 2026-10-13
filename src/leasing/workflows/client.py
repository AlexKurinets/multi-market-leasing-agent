from contextlib import suppress
from typing import Protocol

from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from leasing.config import Settings
from leasing.workflows.briefs import WeeklyBriefsWorkflow
from leasing.workflows.conversation import LeadConversationWorkflow
from leasing.workflows.types import ConversationInput


def workflow_id(market_id: str, lead_id: int) -> str:
    return f"lead-{market_id}-{lead_id}"


class ConversationStarter(Protocol):
    """How the API hands work to the follow-up engine."""

    async def start(self, market_id: str, lead_id: int) -> str: ...

    async def signal_inbound(self, market_id: str, lead_id: int, message_id: int) -> str: ...

    async def stop(self, market_id: str, lead_id: int, reason: str) -> None: ...


class TemporalStarter:
    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def _input(self, market_id: str, lead_id: int, greet: bool) -> ConversationInput:
        return ConversationInput(market_id, lead_id, list(self._settings.cadence_hours), greet)

    async def start(self, market_id: str, lead_id: int) -> str:
        wid = workflow_id(market_id, lead_id)
        # A duplicate webhook delivery finds the workflow already running; that run owns it.
        with suppress(WorkflowAlreadyStartedError):
            await self._client.start_workflow(
                LeadConversationWorkflow.run,
                self._input(market_id, lead_id, greet=True),
                id=wid,
                task_queue=self._settings.temporal_task_queue,
            )
        return wid

    async def signal_inbound(self, market_id: str, lead_id: int, message_id: int) -> str:
        # Signal-with-start: delivered to the running workflow, or starts a fresh run for a
        # prospect who writes back after their cadence ended.
        wid = workflow_id(market_id, lead_id)
        await self._client.start_workflow(
            LeadConversationWorkflow.run,
            self._input(market_id, lead_id, greet=False),
            id=wid,
            task_queue=self._settings.temporal_task_queue,
            start_signal="inbound_message",
            start_signal_args=[message_id],
        )
        return wid

    async def stop(self, market_id: str, lead_id: int, reason: str) -> None:
        handle = self._client.get_workflow_handle(workflow_id(market_id, lead_id))
        try:
            await handle.signal(LeadConversationWorkflow.stop, reason)
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise


async def connect(settings: Settings) -> Client:
    return await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)


async def create_brief_schedule(client: Client, settings: Settings) -> None:
    """Mondays at 11:00 UTC, early morning in every US time zone."""
    await client.create_schedule(
        "weekly-market-briefs",
        Schedule(
            action=ScheduleActionStartWorkflow(
                WeeklyBriefsWorkflow.run,
                id="weekly-market-briefs",
                task_queue=settings.temporal_task_queue,
            ),
            spec=ScheduleSpec(cron_expressions=["0 11 * * 1"]),
        ),
    )
