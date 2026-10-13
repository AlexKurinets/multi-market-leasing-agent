import json
import statistics
from datetime import timedelta

from sqlalchemy import select

from leasing.db.models import Brief, Comp, Property
from leasing.db.session import market_session
from leasing.insights.brief import BriefWriter, generate_market_briefs
from leasing.insights.metrics import brief_payload, submarket_comps

from .conftest import SEED_DAY
from .fakes import FakeAnthropic, message, reply


def test_sql_comp_metrics_match_a_python_reference(factory):
    with market_session("PHX", factory) as session:
        metrics = {m["bedrooms"]: m for m in submarket_comps(session, "Tempe", SEED_DAY)}
        comps = session.scalars(
            select(Comp).where(
                Comp.submarket == "Tempe", Comp.listed_on >= SEED_DAY - timedelta(days=60)
            )
        ).all()
    for bedrooms in range(4):
        rows = [c for c in comps if c.bedrooms == bedrooms]
        expected_dom = statistics.median(
            ((c.leased_on or SEED_DAY) - c.listed_on).days for c in rows
        )
        assert metrics[bedrooms]["comps"] == len(rows)
        assert metrics[bedrooms]["median_rent"] == round(statistics.median(c.rent for c in rows))
        assert metrics[bedrooms]["median_days_on_market"] == round(expected_dom)
        leased = 100 * sum(c.leased_on is not None for c in rows) / len(rows)
        assert metrics[bedrooms]["leased_pct"] == round(leased, 1)


def _payload(factory):
    with market_session("DAL", factory) as session:
        prop = session.scalar(select(Property).where(Property.name == "Elm Street Lofts"))
        return brief_payload(session, prop, SEED_DAY)


def test_payload_carries_precomputed_comparisons(factory):
    payload = _payload(factory)
    one_bed = next(r for r in payload["by_bedrooms"] if r["bedrooms"] == 1)
    expected = round(
        100
        * (one_bed["median_asking_rent"] - one_bed["submarket"]["median_rent"])
        / one_bed["submarket"]["median_rent"],
        1,
    )
    assert one_bed["asking_vs_submarket_pct"] == expected


def _one_bed_median(payload):
    return next(r for r in payload["by_bedrooms"] if r["bedrooms"] == 1)["median_asking_rent"]


def test_brief_with_invented_numbers_is_retried_then_approved(factory, settings):
    payload = _payload(factory)
    rent = _one_bed_median(payload)
    assert "41.3" not in json.dumps(payload)
    client = FakeAnthropic(
        reply(f"One-bedrooms ask ${rent:,}, about 41.3% above the submarket."),
        reply(f"One-bedrooms ask ${rent:,}, in line with the submarket."),
    )
    result = BriefWriter(client, settings).write(payload)
    assert (result.status, result.attempts) == ("approved", 2)
    correction = client.requests[1]["messages"][-1]["content"]
    assert correction.startswith("These figures are not in the payload: 41.3%")
    assert client.requests[0]["output_config"] == {"effort": "low"}


def test_brief_that_keeps_inventing_numbers_is_rejected(factory, settings):
    payload = _payload(factory)
    assert "57.5" not in json.dumps(payload)
    client = FakeAnthropic(reply("Rents rose 57.5%."), reply("Rents rose 57.5%."))
    result = BriefWriter(client, settings).write(payload)
    assert result.status == "rejected"
    assert result.problems == ["not in payload: 57.5%"]


def test_refused_brief_is_rejected(factory, settings):
    result = BriefWriter(FakeAnthropic(message(stop_reason="refusal")), settings).write(
        _payload(factory)
    )
    assert (result.status, result.problems) == ("rejected", ["model_refusal"])


def test_weekly_generation_is_idempotent(factory, settings):
    def echo_rent(params):
        payload = json.loads(
            params["messages"][0]["content"].split("<payload>")[1].split("</payload>")[0]
        )
        return reply(f"One-bedroom median asking rent is ${_one_bed_median(payload):,}.")

    writer = BriefWriter(FakeAnthropic(echo_rent, echo_rent), settings)
    assert generate_market_briefs(writer, "ATL", SEED_DAY, factory) == 2
    assert generate_market_briefs(writer, "ATL", SEED_DAY, factory) == 0
    with market_session("ATL", factory) as session:
        statuses = session.scalars(select(Brief.status)).all()
    assert statuses == ["approved", "approved"]
