import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.worker import Worker

from leasing.agent.loop import LeasingAgent
from leasing.channels import build_senders
from leasing.config import Settings
from leasing.crm import CrmAdapter, build_crm
from leasing.crm.outbox import drain
from leasing.insights.brief import BriefWriter
from leasing.llm import get_client
from leasing.workflows.activities import LeasingActivities
from leasing.workflows.briefs import WeeklyBriefsWorkflow
from leasing.workflows.client import connect
from leasing.workflows.conversation import LeadConversationWorkflow

log = logging.getLogger(__name__)


async def _deliver_crm_events(crm: CrmAdapter, interval_seconds: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    while True:
        try:
            stats = await loop.run_in_executor(None, drain, None, crm)
            if stats.delivered or stats.retried or stats.failed:
                log.info("crm outbox: %s", stats)
        except Exception:
            log.exception("crm outbox drain failed")
        await asyncio.sleep(interval_seconds)


async def run_worker(settings: Settings) -> None:
    client = await connect(settings)
    anthropic_client = get_client()
    activities = LeasingActivities(
        agent=LeasingAgent(anthropic_client, settings),
        senders=build_senders(settings),
        brief_writer=BriefWriter(anthropic_client, settings),
    )
    crm_task = asyncio.create_task(_deliver_crm_events(build_crm(settings)))
    try:
        with ThreadPoolExecutor(max_workers=16) as executor:
            worker = Worker(
                client,
                task_queue=settings.temporal_task_queue,
                workflows=[LeadConversationWorkflow, WeeklyBriefsWorkflow],
                activities=[
                    activities.respond_to_lead,
                    activities.list_markets,
                    activities.generate_market_briefs,
                ],
                activity_executor=executor,
            )
            log.info("worker listening on task queue %r", settings.temporal_task_queue)
            await worker.run()
    finally:
        crm_task.cancel()
