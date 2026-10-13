# Multi-market leasing agent

An AI leasing assistant that answers rental prospects by SMS and email across several markets, books tours, follows up when prospects go quiet, writes every interaction back to the CRM, and produces weekly pricing briefs for leasing managers.

This is a reference implementation of the architecture behind a leasing platform I built for a residential property management company operating in several metro areas. It is a clean rebuild written to show the design and the engineering decisions, and it runs on synthetic data: every market, property, person, phone number and figure in this repository is made up.

[![CI](https://github.com/AlexKurinets/multi-market-leasing-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/AlexKurinets/multi-market-leasing-agent/actions/workflows/ci.yml)

## Architecture

```mermaid
flowchart LR
    ILS["Listing sites and web forms"] -->|"POST /leads, /leads/email"| API
    SMS["Twilio inbound SMS"] -->|webhook| API
    API["FastAPI"] -->|"start / signal"| WF["Temporal: one workflow per lead"]
    WF -->|"respond_to_lead activity"| AG["Agent: Claude tool-use loop"]
    AG <-->|"market-scoped tools"| DB[("Postgres + pgvector")]
    AG --> DL["Delivery: draft, then send at most once"]
    DL --> TW["Twilio / SMTP"]
    DB --> OB["CRM outbox"] --> HS["HubSpot"]
    SC["Weekly Temporal schedule"] --> BR["Brief writer"] --> DB
    DASH["Next.js dashboard"] --> API
```

A new inquiry is normalised, deduplicated within its market and stored, then a Temporal workflow is started for the lead. The workflow sends the first reply, answers every message the prospect sends, and runs the follow-up cadence (24 hours, 3 days, 7 days by default) when they go quiet. Each reply is one activity: the agent runs a tool-use loop against the market's data, and the reply is checked, drafted and delivered. A person can take over any conversation from the dashboard, which stops the workflow.

## Design decisions

**Market isolation lives in the database session, not in each query.** Every market-scoped table carries `market_id`, and a session opened with `market_session("PHX")` adds `market_id = 'PHX'` to every ORM statement through `with_loader_criteria`, including joins, aggregates, bulk updates and primary-key lookups. A session without a scope refuses to touch scoped tables at all. A forgotten `WHERE` clause cannot leak Dallas pricing into a Phoenix conversation. The test suite has a case for each statement shape, including one (`select(func.count()).select_from(Unit)`) that slipped past the first version of the filter. See `src/leasing/db/scoping.py`.

**Facts come from tools, and replies are checked before they are sent.** Prices, availability, fees and tour times reach the model only through tools that run inside the conversation's market-scoped session. Before a reply goes out, every dollar amount and percentage in it must appear in a tool result or in something the prospect wrote. A reply that fails is withheld, the prospect gets a holding message, and the conversation is handed to a person. Policy retrieval filters by market before ranking, so a near-identical policy from another city can never outrank the right one.

**Fair Housing guardrails run outside the model.** Inbound messages about accommodations, assistance animals, source of income or who lives in an area get a fixed reply and go to a person without the model being called. Questions about safety or schools get a neutral fixed answer. Outbound replies are screened for steering language ("great for families", "safe neighborhood"). A prompt regression cannot switch any of this off.

**One durable workflow per lead.** Temporal holds the cadence state, so follow-ups survive deploys and worker restarts. The workflow ends itself when a tour is booked, a person takes over, the prospect opts out or the cadence runs out. Prospect replies arrive as signals. A reply from someone whose workflow has already finished uses signal-with-start to begin a new run that answers the message rather than greeting them again. Automated follow-ups respect per-market quiet hours and wait for the morning; direct replies to a prospect's message go out immediately.

**Texts are delivered at most once.** A reply is stored as a draft keyed by the workflow turn before any provider call, and marked `sending` before Twilio is called. A retried activity that finds its draft reuses it instead of calling the model again. One that finds a `sending` row does not resend, because the earlier attempt may have reached the provider; that case is surfaced to the leasing team instead. A missed follow-up is recoverable, and a duplicate text to a prospect is not.

**CRM write-back goes through a transactional outbox.** Contact upserts and activity notes are written in the same transaction as the change they describe and delivered by the worker with exponential backoff, so a CRM outage never blocks or loses a conversation. The HubSpot adapter is two methods behind a `CrmAdapter` protocol, so another CRM is one class.

**Briefs: SQL computes, the model narrates, a validator checks.** Medians, days on market, leased share and concession share come from `percentile_cont` queries over submarket comps. The model writes a short brief from that payload and is told not to calculate anything. Every number in the brief must exist in the payload; if one doesn't, the model gets one correction round, and a brief that still fails is stored as rejected for review rather than shown to managers. The check proves each figure exists in the source data, not that it is attached to the right label, which is what the eval set is for.

**The transcript is append-only.** The model-facing conversation is stored exactly as sent and received, one row per turn, and never edited. Assistant turns keep their thinking blocks unchanged, which the API requires when a conversation continues on the same model. The system prompt is identical for every market and conversation so it stays cacheable. Anything market- or time-specific arrives as an `<event>` in the conversation, and the prospect's own words are wrapped in `<prospect_message>` tags and treated as data, not instructions.

**Model configuration.** Conversations use `claude-opus-5-5` at `medium` effort, because tool selection matters there; briefs use `low` effort, since they narrate precomputed numbers. Requests opt into the API's server-side refusal fallback, and a refusal that still comes back hands the conversation to a person. Tools use `strict: true` schemas generated from Pydantic models, and tool inputs are validated again before they run.

## Running it

### With Docker

```bash
cp .env.example .env        # set ANTHROPIC_API_KEY
docker compose up --build
```

This starts Postgres with pgvector, a Temporal dev server (web UI on http://localhost:8233), the API on http://localhost:8000, the worker, and the dashboard on http://localhost:3000. The `migrate` service creates the schema and loads the synthetic markets.

Send a lead:

```bash
curl -X POST localhost:8000/leads -H 'Content-Type: application/json' -d '{
  "market_id": "PHX", "property_id": 1, "first_name": "Jordan",
  "phone": "+16025550147", "sms_consent": true,
  "consent_evidence": "Website form, SMS consent checkbox",
  "message": "Is the one bedroom still available? What is the pet deposit?"
}'
```

With `SMS_BACKEND=console` (the default) the worker prints outbound texts instead of sending them, and the conversation appears in the dashboard.

The weekly brief schedule is created once, not on every start:

```bash
docker compose exec api leasing schedule-briefs
```

### Without Docker

You need Postgres 16 with the pgvector extension and the Temporal CLI (`temporal server start-dev`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
leasing init-db && leasing seed
leasing api            # terminal 1
leasing worker         # terminal 2
cd dashboard && npm install && npm run dev   # terminal 3
```

### Talk to the agent from a terminal

```bash
leasing chat --market DAL
```

This runs the same reply path as the workflow activity, without Temporal, and logs each tool call so you can see what the agent looked up.

## Tests

```bash
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/leasing_test pytest
```

The suite runs against real Postgres with pgvector (CI uses the `pgvector/pgvector` image), because the isolation guarantees and the comp metrics are SQL behaviour that a mock would not exercise. Workflow tests run on Temporal's time-skipping test server, so weeks of cadence take seconds. Claude is replaced by a scripted fake that returns real SDK `BetaMessage` objects, so the agent code handles exactly the types the live API returns.

## Evals

```bash
python evals/run_evals.py
python evals/run_evals.py --only phx-pet-deposit dal-pet-deposit
```

`evals/cases.jsonl` holds conversations replayed against the live model and the seeded data: per-market policy questions where the other markets' figures must not appear, unit searches, tour scheduling and booking, accommodation requests, steering questions, a pricing exception, a prospect anchoring on another city's price, and a prompt injection. Each case runs in a rolled-back transaction. The runner calls the Anthropic API a few times per case, so it belongs before a prompt, tool or model change ships rather than on every commit.

## Layout

```
src/leasing/
  db/            models, market-scoped sessions
  agent/         system prompt, tools, tool-use loop
  ingestion/     normalisation, dedupe, listing-site email parsing
  channels/      Twilio and SMTP senders, at-most-once delivery
  crm/           CrmAdapter protocol, HubSpot adapter, outbox
  insights/      SQL comp metrics, brief writer
  workflows/     Temporal workflows, activities, worker, schedules
  api/           FastAPI app
  compliance.py  consent and quiet hours
  guardrails.py  Fair Housing screens
  validation.py  number grounding
  evals.py       eval harness
dashboard/       Next.js leasing console
evals/           eval cases and runner
samples/         synthetic listing-site lead emails
tests/
```

## Extending it

A new market is rows, not code: a `markets` row with its time zone and quiet hours, plus its properties, policies and policy documents. A new CRM implements `upsert_contact` and `log_note`. The inventory source is the part that changes most between businesses. Here it is a property management system's unit list; a brokerage would instead sync listings per city from each MLS, typically through the RESO Web API and normalised to the RESO Data Dictionary, with the same market scoping on top.

## Not included

This is a reference build, so a few production pieces are deliberately left out: authentication on the API and dashboard, Alembic migrations (the schema is created with `create_all`), Twilio delivery-status callbacks, Postgres row-level security as a second isolation layer under the session filter, a hosted embedding model (a deterministic hashing embedder keeps the demo keyless), and OpenTelemetry tracing per conversation.

## License

MIT
