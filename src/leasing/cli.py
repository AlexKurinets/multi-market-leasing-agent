import argparse
import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta

from leasing.config import get_settings


def _chat(market_id: str, property_id: int | None) -> None:
    """Talk to the agent from the terminal, as a prospect. Runs the same respond() path as the
    workflow activity, without Temporal, and prints replies instead of sending them."""
    from leasing.agent import LeasingAgent
    from leasing.channels import ConsoleSender
    from leasing.conversations import respond
    from leasing.db.models import Message
    from leasing.db.session import market_session
    from leasing.ingestion import NewLead, ingest_lead
    from leasing.llm import get_client
    from leasing.workflows.types import RespondRequest

    settings = get_settings()
    agent = LeasingAgent(get_client(), settings)
    senders = {"sms": ConsoleSender("sms"), "email": ConsoleSender("email")}
    email = f"demo-{uuid.uuid4().hex[:8]}@example.com"
    print(f"Chatting as a new prospect in {market_id} ({email}). Ctrl+C to quit.\n")

    lead_id = conversation_id = None
    while True:
        try:
            text = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        with market_session(market_id) as session:
            if lead_id is None:
                result = ingest_lead(
                    session,
                    NewLead(
                        source="terminal",
                        first_name="Demo",
                        email=email,
                        property_id=property_id,
                        message=text,
                    ),
                    datetime.now(UTC),
                )
                lead_id, conversation_id = result.lead_id, result.conversation_id
                trigger, ids = "new_lead", []
            else:
                message = Message(
                    conversation_id=conversation_id,
                    direction="inbound",
                    channel="email",
                    author="lead",
                    body=text,
                    status="received",
                )
                session.add(message)
                session.flush()
                trigger, ids = "inbound", [message.id]
        outcome = respond(
            RespondRequest(market_id, lead_id, trigger, f"terminal/{uuid.uuid4()}", ids),
            agent=agent,
            senders=senders,
        )
        if outcome.status != "sent":
            print(f"(no reply: {outcome.status} {outcome.detail})")
        if outcome.tour_booked or outcome.handed_off:
            print(
                f"(conversation ended: tour_booked={outcome.tour_booked}, "
                f"handed_off={outcome.handed_off})"
            )
            return


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="leasing")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="Create the schema and the pgvector extension.")
    sub.add_parser("seed", help="Load the synthetic demo markets.")
    api = sub.add_parser("api", help="Run the HTTP API.")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker", help="Run the Temporal worker and the CRM outbox.")
    sub.add_parser("schedule-briefs", help="Create the weekly briefs Temporal schedule.")
    briefs = sub.add_parser("briefs", help="Write this week's briefs for one market now.")
    briefs.add_argument("--market", required=True)
    sub.add_parser("drain-crm", help="Deliver pending CRM events once.")
    chat = sub.add_parser("chat", help="Chat with the agent as a prospect.")
    chat.add_argument("--market", default="PHX")
    chat.add_argument("--property-id", type=int)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = get_settings()

    if args.command == "init-db":
        from leasing.db import init_db

        init_db()
    elif args.command == "seed":
        from leasing.seed import seed

        print("seeded" if seed() else "already seeded")
    elif args.command == "api":
        import uvicorn

        from leasing.api.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.command == "worker":
        from leasing.workflows.worker import run_worker

        asyncio.run(run_worker(settings))
    elif args.command == "schedule-briefs":
        from leasing.workflows.client import connect, create_brief_schedule

        async def schedule() -> None:
            await create_brief_schedule(await connect(settings), settings)

        asyncio.run(schedule())
    elif args.command == "briefs":
        from leasing.insights.brief import BriefWriter, generate_market_briefs
        from leasing.llm import get_client

        today = date.today()
        week_of = today - timedelta(days=today.weekday())
        written = generate_market_briefs(BriefWriter(get_client(), settings), args.market, week_of)
        print(f"wrote {written} brief(s) for {args.market}, week of {week_of}")
    elif args.command == "drain-crm":
        from leasing.crm import build_crm
        from leasing.crm.outbox import drain

        print(drain(None, build_crm(settings)))
    elif args.command == "chat":
        _chat(args.market, args.property_id)


if __name__ == "__main__":
    main()
