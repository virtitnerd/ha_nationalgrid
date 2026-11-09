"""Tests for AMI hourly cost statistics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    get_metadata,
)
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.national_grid_us.coordinator import (
    MeterData,
    NationalGridCoordinatorData,
)
from custom_components.national_grid_us.statistics import (
    Bill,
    RateTable,
    _bills_from_history,
    async_import_all_statistics,
    async_import_cost_statistics,
    build_cost_rows,
    cost_statistic_id,
)

BILLS = [
    Bill(date(2026, 7, 10), date(2026, 8, 9), cost=60.0, usage=300.0),  # 0.20
    Bill(date(2026, 8, 9), date(2026, 9, 10), cost=75.0, usage=300.0),  # 0.25
    Bill(date(2026, 9, 12), date(2026, 10, 10), cost=90.0, usage=300.0),  # 0.30
]

USAGE_ID = "national_grid_us:acct1_sp1_electric_hourly_usage"
COST_ID = "national_grid_us:acct1_sp1_electric_hourly_cost"
STATS = "custom_components.national_grid_us.statistics"


# --- Rate lookup and row building ---------------------------------------------


def test_rate_inside_period() -> None:
    """A day inside a billing period uses that bill's blended rate."""
    rates = RateTable(BILLS)
    assert rates.rate_for(date(2026, 7, 10)) == pytest.approx(0.20)
    assert rates.rate_for(date(2026, 8, 8)) == pytest.approx(0.20)
    assert rates.rate_for(date(2026, 8, 9)) == pytest.approx(0.25)


def test_rate_after_newest_bill_uses_recent_estimate() -> None:
    """Days after the newest bill use the recent-bills blended rate."""
    rates = RateTable(BILLS)
    # Total charges 225 over 900 kWh across the three bills
    assert rates.rate_for(date(2026, 11, 1)) == pytest.approx(0.25)


def test_rate_before_oldest_bill_uses_oldest() -> None:
    """Days before the oldest bill use the oldest bill's rate."""
    assert RateTable(BILLS).rate_for(date(2026, 1, 1)) == pytest.approx(0.20)


def test_rate_in_gap_uses_preceding_bill() -> None:
    """Days in a gap between bills use the preceding bill's rate."""
    assert RateTable(BILLS).rate_for(date(2026, 9, 11)) == pytest.approx(0.25)


def test_unusable_bills_are_ignored() -> None:
    """Zero-usage and zero-length bills cannot be priced."""
    rates = RateTable(
        [
            Bill(date(2026, 1, 1), date(2026, 2, 1), cost=10.0, usage=0.0),
            Bill(date(2026, 2, 1), date(2026, 2, 1), cost=10.0, usage=5.0),
        ]
    )
    assert not rates
    with pytest.raises(ValueError, match="no usable bills"):
        rates.rate_for(date(2026, 1, 15))


def test_build_cost_rows_continues_sum() -> None:
    """Cost rows price each hour and continue the given sum."""
    rates = RateTable(BILLS)
    usage = [
        (datetime(2026, 7, 20, 16, tzinfo=UTC), 1.0),
        (datetime(2026, 7, 20, 17, tzinfo=UTC), 2.0),
    ]
    rows = build_cost_rows(usage, rates, start_sum=10.0)
    assert [r["state"] for r in rows] == pytest.approx([0.20, 0.40])
    assert [r["sum"] for r in rows] == pytest.approx([10.20, 10.60])


def test_cost_statistic_id() -> None:
    """Usage statistic IDs map to their cost twins."""
    assert cost_statistic_id(USAGE_ID) == COST_ID
    assert (
        cost_statistic_id("national_grid_us:acct1_sp1_electric_interval_usage")
        == "national_grid_us:acct1_sp1_electric_interval_cost"
    )
    with pytest.raises(ValueError, match="Not a usage statistic"):
        cost_statistic_id("national_grid_us:acct1_sp1_electric_hourly")


def test_bills_from_history_reads_billed_fields() -> None:
    """Bills use the same fields as the billed series and skip bad records."""
    history = [
        {
            "readFromDate": "2026-07-10T00:00:00",
            "readDate": "2026-08-09T00:00:00",
            "totalCharges": 60.0,
            "totalKwh": 300,
        },
        {"readFromDate": "2026-08-09", "readDate": "2026-09-10", "totalKwh": 1},
        {"readFromDate": "bad", "readDate": "2026-09-10", "totalCharges": 1},
        {
            "readFromDate": "2026-08-09",
            "readDate": "2026-09-10",
            "totalCharges": "n/a",
            "totalKwh": 1,
        },
    ]
    assert _bills_from_history(history, "totalKwh") == [BILLS[0]]
    assert _bills_from_history(None, "totalKwh") == []


# --- async_import_cost_statistics ---------------------------------------------


def _change_rows(*hours: tuple[datetime, float]) -> dict:
    return {
        USAGE_ID: [
            {"start": start.timestamp(), "change": change} for start, change in hours
        ]
        + [{"start": hours[-1][0].timestamp() + 3600, "change": None}]
    }


@patch(f"{STATS}.async_add_external_statistics")
@patch(f"{STATS}.get_instance")
async def test_cost_import_skips_without_usable_bills(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Nothing is read or written when no bill can be priced."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock()
    await async_import_cost_statistics(
        hass, usage_statistic_id=USAGE_ID, name="Cost", bills=[], full_refresh=True
    )
    mock_get_instance.return_value.async_add_executor_job.assert_not_called()
    mock_add_stats.assert_not_called()


@patch(f"{STATS}.async_add_external_statistics")
@patch(f"{STATS}.get_instance")
async def test_cost_import_full_refresh(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """A full refresh prices every stored usage hour from a zero sum."""
    executor = AsyncMock(
        return_value=_change_rows(
            (datetime(2026, 7, 20, 16, tzinfo=UTC), 1.0),
            (datetime(2026, 8, 20, 16, tzinfo=UTC), 2.0),
        )
    )
    mock_get_instance.return_value.async_add_executor_job = executor

    await async_import_cost_statistics(
        hass,
        usage_statistic_id=USAGE_ID,
        name="acct1-sp1 Electric Hourly Cost",
        bills=BILLS,
        full_refresh=True,
    )

    # Only the usage read; no sum lookup on a full refresh
    assert executor.await_count == 1
    assert executor.await_args.args[0].args[1] == datetime.fromtimestamp(0, tz=UTC)
    metadata = mock_add_stats.call_args[0][1]
    rows = mock_add_stats.call_args[0][2]
    assert metadata["statistic_id"] == COST_ID
    assert metadata["name"] == "acct1-sp1 Electric Hourly Cost"
    assert metadata["unit_of_measurement"] is None
    assert metadata["unit_class"] is None
    assert metadata["has_sum"] is True
    assert [r["state"] for r in rows] == pytest.approx([0.20, 0.50])
    assert [r["sum"] for r in rows] == pytest.approx([0.20, 0.70])
    mock_get_instance.return_value.async_clear_statistics.assert_not_called()


@patch(f"{STATS}.async_add_external_statistics")
@patch(f"{STATS}.get_instance")
async def test_cost_import_incremental_continues_sum(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """A normal refresh rewrites the reprice window from the prior cost sum."""
    hour = (datetime.now(tz=UTC) - timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0
    )
    executor = AsyncMock(
        side_effect=[
            {COST_ID: [{"sum": 5.0}, {"sum": 12.5}]},  # last cost sum before window
            _change_rows((hour, 4.0)),
        ]
    )
    mock_get_instance.return_value.async_add_executor_job = executor

    await async_import_cost_statistics(
        hass, usage_statistic_id=USAGE_ID, name="Cost", bills=BILLS, full_refresh=False
    )

    sum_query = executor.await_args_list[0].args[0]
    window_start = sum_query.args[2]
    assert sum_query.args[3] == {COST_ID}
    assert timedelta(days=74) < datetime.now(tz=UTC) - window_start < timedelta(days=76)
    assert executor.await_args_list[1].args[0].args[1] == window_start
    rows = mock_add_stats.call_args[0][2]
    # After the newest bill: recent-bills estimate of 0.25/kWh
    assert rows[0]["state"] == pytest.approx(1.0)
    assert rows[0]["sum"] == pytest.approx(13.5)


@patch(f"{STATS}.async_add_external_statistics")
@patch(f"{STATS}.get_instance")
async def test_cost_import_replace_series_clears_and_rebuilds(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """The interval cost series is cleared and rebuilt from a zero sum."""
    interval_usage = "national_grid_us:acct1_sp1_electric_interval_usage"
    hour = datetime(2026, 9, 1, 12, tzinfo=UTC)
    executor = AsyncMock(
        return_value={interval_usage: [{"start": hour.timestamp(), "change": 2.0}]}
    )
    mock_get_instance.return_value.async_add_executor_job = executor

    await async_import_cost_statistics(
        hass,
        usage_statistic_id=interval_usage,
        name="Cost",
        bills=BILLS,
        full_refresh=False,
        replace_series=True,
    )

    assert executor.await_count == 1
    mock_get_instance.return_value.async_clear_statistics.assert_called_once_with(
        ["national_grid_us:acct1_sp1_electric_interval_cost"]
    )
    rows = mock_add_stats.call_args[0][2]
    assert rows[0]["sum"] == pytest.approx(0.50)


@patch(f"{STATS}.async_add_external_statistics")
@patch(f"{STATS}.get_instance")
async def test_cost_import_replace_series_without_usage_only_clears(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """An empty interval usage series leaves an empty cost series."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    await async_import_cost_statistics(
        hass,
        usage_statistic_id=USAGE_ID,
        name="Cost",
        bills=BILLS,
        full_refresh=False,
        replace_series=True,
    )
    mock_get_instance.return_value.async_clear_statistics.assert_called_once_with(
        [COST_ID]
    )
    mock_add_stats.assert_not_called()


# --- Wiring into async_import_all_statistics ----------------------------------

ELECTRIC_HISTORY = [
    {
        "readFromDate": "2026-07-10",
        "readDate": "2026-08-09",
        "totalCharges": 60.0,
        "totalKwh": 300,
    }
]
GAS_HISTORY = [
    {
        "readFromDate": "2026-07-10",
        "readDate": "2026-08-09",
        "totalCharges": 30.0,
        "totalTherms": 20,
    }
]


def _meter(sp: str, fuel_type: str) -> MeterData:
    return MeterData(
        account_id="acct1",
        meter={"fuelType": fuel_type, "servicePointNumber": sp},
        billing_account={"billingAccountId": "acct1"},
    )


def _data(meters: dict, **kwargs) -> NationalGridCoordinatorData:
    return NationalGridCoordinatorData(
        accounts={"acct1": {"billingAccountId": "acct1"}},
        meters=meters,
        ami_usages={key: [] for key in meters},
        electric_bill_history={"acct1": ELECTRIC_HISTORY},
        gas_bill_history={"acct1": GAS_HISTORY},
        **kwargs,
    )


@patch(f"{STATS}.async_import_cost_statistics", new_callable=AsyncMock)
@patch(f"{STATS}.get_instance")
async def test_import_all_prices_ami_series(mock_get_instance, mock_cost, hass) -> None:
    """Hourly electric, interval electric and hourly gas series are priced."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_block_till_done = AsyncMock()
    coordinator = MagicMock()
    coordinator.data = _data(
        {"e": _meter("sp1", "Electric"), "g": _meter("sp2", "Gas")},
        interval_reads={"e": []},
        is_first_refresh=True,
    )

    await async_import_all_statistics(hass, coordinator)

    mock_get_instance.return_value.async_block_till_done.assert_awaited_once()
    calls = {
        c.kwargs["usage_statistic_id"]: c.kwargs for c in mock_cost.await_args_list
    }
    assert set(calls) == {
        "national_grid_us:acct1_sp1_electric_hourly_usage",
        "national_grid_us:acct1_sp1_electric_interval_usage",
        "national_grid_us:acct1_sp2_gas_hourly_usage",
    }
    hourly = calls["national_grid_us:acct1_sp1_electric_hourly_usage"]
    assert hourly["name"] == "acct1-sp1 Electric Hourly Cost"
    assert hourly["full_refresh"] is True
    assert hourly["replace_series"] is False
    assert hourly["bills"] == [BILLS[0]]
    interval = calls["national_grid_us:acct1_sp1_electric_interval_usage"]
    assert interval["name"] == "acct1-sp1 Electric Interval Cost"
    assert interval["replace_series"] is True
    gas = calls["national_grid_us:acct1_sp2_gas_hourly_usage"]
    assert gas["name"] == "acct1-sp2 Gas Hourly Cost"
    assert gas["bills"] == [Bill(date(2026, 7, 10), date(2026, 8, 9), 30.0, 20.0)]


@patch(f"{STATS}.async_import_cost_statistics", new_callable=AsyncMock)
@patch(f"{STATS}.get_instance")
async def test_import_all_skips_cost_for_multiple_ami_meters(
    mock_get_instance, mock_cost, hass, caplog
) -> None:
    """Two AMI meters of one fuel on an account get one warning and no cost."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_block_till_done = AsyncMock()
    coordinator = MagicMock()
    coordinator.data = _data(
        {
            "e1": _meter("sp1", "Electric"),
            "e2": _meter("sp2", "Electric"),
            "g": _meter("sp3", "Gas"),
        }
    )

    await async_import_all_statistics(hass, coordinator)

    assert caplog.text.count("skipping cost statistics") == 1
    assert "2 Electric meters with AMI" in caplog.text
    assert [c.kwargs["usage_statistic_id"] for c in mock_cost.await_args_list] == [
        "national_grid_us:acct1_sp3_gas_hourly_usage"
    ]


@patch(f"{STATS}.async_import_cost_statistics", new_callable=AsyncMock)
@patch(f"{STATS}.get_instance")
async def test_import_all_skips_cost_without_bills(
    mock_get_instance, mock_cost, hass
) -> None:
    """Without bill history there is nothing to price or wait for."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    coordinator = MagicMock()
    coordinator.data = _data({"e": _meter("sp1", "Electric")})
    coordinator.data.electric_bill_history = {}

    await async_import_all_statistics(hass, coordinator)

    mock_cost.assert_not_awaited()


# --- Real recorder --------------------------------------------------------------


async def test_cost_series_matches_recorded_usage(hass) -> None:
    """Usage is read back from the recorder and priced per bill period."""
    readings = [
        {"date": "2026-07-20T12:00:00.000-04:00", "quantity": 1.0},
        {"date": "2026-07-20T12:15:00.000-04:00", "quantity": 1.0},
        {"date": "2026-08-20T12:00:00.000-04:00", "quantity": 4.0},
    ]
    history = [
        {
            "readFromDate": b.start.isoformat(),
            "readDate": b.end.isoformat(),
            "totalCharges": b.cost,
            "totalKwh": b.usage,
        }
        for b in BILLS
    ]
    coordinator = MagicMock()
    coordinator.data = NationalGridCoordinatorData(
        accounts={"acct1": {"billingAccountId": "acct1"}},
        meters={"e": _meter("sp1", "Electric")},
        ami_usages={"e": readings},
        electric_bill_history={"acct1": history},
        is_first_refresh=True,
    )

    # Run twice: rewriting the series must not double count
    for _ in range(2):
        await async_import_all_statistics(hass, coordinator)
        coordinator.data.is_first_refresh = False
    await async_wait_recording_done(hass)

    instance = get_instance(hass)
    meta = await instance.async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={COST_ID})
    )
    last = await instance.async_add_executor_job(
        get_last_statistics, hass, 1, COST_ID, True, {"sum"}
    )
    assert meta[COST_ID][1]["unit_of_measurement"] is None
    # 2 kWh at 0.20 + 4 kWh at 0.25
    assert last[COST_ID][0]["sum"] == pytest.approx(1.40)
