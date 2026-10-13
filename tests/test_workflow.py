"""Workflow behaviour on Temporal's time-skipping test server: days of cadence run in seconds.
The respond activity is replaced by a recorder, so these tests cover orchestration only."""

import uuid
from collections.abc import AsyncIterator, Callable
from datetime import timedelta

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from leasing.workflows.conversation import LeadConversationWorkflow
from leasing.workflows.types import (
    RESPOND_ACTIVITY,
    ConversationInput,
    RespondRequest,
    RespondResult,
)

CADENCE = [24, 72, 168]


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class Recorder:
    def __init__(self, decide: Callable[[RespondRequest], RespondResult] | None = None) -> None:
        self.requests: list[RespondRequest] = []
        self.decide = decide or (lambda req: RespondResult("sent"))

    def activity(self):
        @activity.defn(name=RESPOND_ACTIVITY)
        async def respond_to_lead(req: RespondRequest) -> RespondResult:
            self.requests.append(req)
            return self.decide(req)

        return respond_to_lead

    @property
    def triggers(self) -> list[str]:
        return [r.trigger for r in self.requests]


async def _run(
    client: Client,
    recorder: Recorder,
    *,
    greet: bool = True,
    before_result: Callable[[WorkflowHandle], object] | None = None,
) -> str:
    queue = f"test-{uuid.uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[LeadConversationWorkflow],
        activities=[recorder.activity()],
    ):
        handle = await client.start_workflow(
            LeadConversationWorkflow.run,
            ConversationInput("PHX", 1, CADENCE, greet),
            id=f"lead-PHX-{uuid.uuid4()}",
            task_queue=queue,
        )
        if before_result is not None:
            await before_result(handle)
        return await handle.result()


@pytest.mark.asyncio(loop_scope="session")
async def test_silent_lead_gets_the_full_cadence_then_closes(env):
    recorder = Recorder()
    assert await _run(env.client, recorder) == "no_response"
    assert recorder.triggers == ["new_lead", "follow_up", "follow_up", "follow_up"]
    assert [r.follow_up_number for r in recorder.requests[1:]] == [1, 2, 3]
    assert len({r.idempotency_key for r in recorder.requests}) == 4


@pytest.mark.asyncio(loop_scope="session")
async def test_reply_is_answered_and_takeover_stops_the_workflow(env):
    recorder = Recorder()

    async def prospect_replies_then_agent_takes_over(handle):
        await handle.signal(LeadConversationWorkflow.inbound_message, 42)
        await env.sleep(timedelta(hours=1))
        await handle.signal(LeadConversationWorkflow.stop, "human_takeover")

    result = await _run(env.client, recorder, before_result=prospect_replies_then_agent_takes_over)
    assert result == "human_takeover"
    assert recorder.triggers == ["new_lead", "inbound"]
    assert recorder.requests[1].message_ids == [42]


@pytest.mark.asyncio(loop_scope="session")
async def test_tour_booking_ends_the_cadence(env):
    recorder = Recorder(lambda req: RespondResult("sent", tour_booked=req.trigger == "inbound"))

    async def prospect_replies(handle):
        await handle.signal(LeadConversationWorkflow.inbound_message, 7)

    assert await _run(env.client, recorder, before_result=prospect_replies) == "tour_booked"
    assert recorder.triggers == ["new_lead", "inbound"]


@pytest.mark.asyncio(loop_scope="session")
async def test_quiet_hours_deferral_retries_the_same_turn(env):
    def decide(req: RespondRequest) -> RespondResult:
        follow_ups = [r for r in recorder.requests if r.trigger == "follow_up"]
        if req.trigger == "follow_up" and len(follow_ups) == 1:
            retry_at = activity.info().scheduled_time + timedelta(hours=10)
            return RespondResult("deferred", "quiet_hours", retry_at=retry_at.isoformat())
        return RespondResult("sent")

    recorder = Recorder(decide)
    await _run(env.client, recorder)
    follow_ups = [r for r in recorder.requests if r.trigger == "follow_up"]
    assert [r.follow_up_number for r in follow_ups] == [1, 1, 2, 3]
    assert follow_ups[0].idempotency_key == follow_ups[1].idempotency_key


@pytest.mark.asyncio(loop_scope="session")
async def test_handoff_or_lost_consent_stops_the_workflow(env):
    recorder = Recorder(lambda req: RespondResult("skipped", "no_consent"))
    assert await _run(env.client, recorder) == "no_consent"
    assert recorder.triggers == ["new_lead"]


@pytest.mark.asyncio(loop_scope="session")
async def test_run_started_by_a_message_skips_the_greeting(env):
    recorder = Recorder()

    async def message_arrives(handle):
        await handle.signal(LeadConversationWorkflow.inbound_message, 9)

    await _run(env.client, recorder, greet=False, before_result=message_arrives)
    assert recorder.triggers[0] == "inbound"
