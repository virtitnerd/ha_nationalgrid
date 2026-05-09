"""Tests for the National Grid statistics module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.national_grid.coordinator import (
    MeterData,
    NationalGridCoordinatorData,
)
from custom_components.national_grid.statistics import (
    _bucket_interval_reads,
    _parse_ami_datetime,
    async_import_all_statistics,
    async_import_meter_statistics,
)


def _make_coordinator_data(
    *,
    ami_usages: dict | None = None,
    meters: dict | None = None,
    is_first_refresh: bool = True,
) -> NationalGridCoordinatorData:
    """Build mock coordinator data.

    Args:
        ami_usages: AMI 15-min usage data by service point
        meters: Meter data by service point
        is_first_refresh: Whether this is the first refresh (imports all data)
                          or incremental

    """
    return NationalGridCoordinatorData(
        accounts={"acct1": {"billingAccountId": "acct1"}},
        meters=meters or {},
        usages={},
        costs={},
        ami_usages=ami_usages or {},
        is_first_refresh=is_first_refresh,
    )


def _make_meter_data(fuel_type: str = "Electric") -> MeterData:
    return MeterData(
        account_id="acct1",
        meter={"fuelType": fuel_type, "servicePointNumber": "SP1"},
        billing_account={"billingAccountId": "acct1"},
    )


async def test_import_all_statistics_no_data(hass) -> None:
    """Test no error when coordinator data is None."""
    coordinator = MagicMock()
    coordinator.data = None
    await async_import_all_statistics(hass, coordinator)


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats(mock_get_instance, mock_add_stats, hass) -> None:
    """Test 15-min AMI stats import for electric meter."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [
        {"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0},
        {"date": "2025-01-15T10:15:00.000Z", "quantity": 3.0},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )

    await async_import_all_statistics(hass, coordinator)
    assert mock_add_stats.called
    metadata = mock_add_stats.call_args[0][1]
    stats = mock_add_stats.call_args[0][2]
    assert metadata["statistic_id"] == "national_grid:acct1_SP1_electric_hourly_usage"
    # Both readings fall within the same clock hour so they are bucketed into one stat
    assert len(stats) == 1
    assert stats[0]["state"] == 8.0  # 5.0 + 3.0 aggregated into the 10:00 bucket
    assert stats[0]["sum"] == 8.0


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_gas(mock_get_instance, mock_add_stats, hass) -> None:
    """Test gas usage values are imported directly as CCF."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [{"date": "2025-01-15T10:00:00.000Z", "quantity": 10.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Gas")},
    )

    await async_import_all_statistics(hass, coordinator)
    assert mock_add_stats.called
    stats = mock_add_stats.call_args[0][2]
    assert stats[0]["state"] == 10.0


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_with_existing_sum(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test AMI stats continues from last imported sum on incremental updates."""
    # Return existing statistics with a sum and timestamp
    existing = {
        "national_grid:acct1_SP1_electric_hourly_usage": [
            {"sum": 10.0, "start": 1736935200.0}  # 2025-01-15T10:00:00 UTC
        ]
    }
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(
        return_value=existing
    )

    readings = [
        {"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0},  # should be skipped
        {"date": "2025-01-15T11:00:00.000Z", "quantity": 3.0},  # should be included
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
        is_first_refresh=False,  # Incremental update - use existing sum
    )

    await async_import_all_statistics(hass, coordinator)
    assert mock_add_stats.called
    stats = mock_add_stats.call_args[0][2]
    assert len(stats) == 1
    assert stats[0]["sum"] == 13.0  # 10.0 + 3.0


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_skips_empty_date(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test readings with empty date are skipped."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [{"date": "", "quantity": 5.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )

    await async_import_all_statistics(hass, coordinator)
    assert not mock_add_stats.called


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_skips_bad_date(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test readings with unparseable date are skipped."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [{"date": "not-a-date", "quantity": 5.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )

    await async_import_all_statistics(hass, coordinator)
    assert not mock_add_stats.called


async def test_import_all_statistics_skips_missing_meter(hass) -> None:
    """Test AMI usages for unknown service points are skipped."""
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={
            "SP_UNKNOWN": [{"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0}]
        },
        meters={},  # no meters
    )
    # Should not raise
    await async_import_all_statistics(hass, coordinator)


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_imports_all_new_data(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test all new readings are imported regardless of age."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    # Both recent and old readings should be imported (no cutoff)
    now = datetime.now(tz=UTC)
    recent_time = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00.000Z")
    old_time = (now - timedelta(hours=72)).strftime("%Y-%m-%dT%H:00:00.000Z")

    readings = [
        {"date": old_time, "quantity": 5.0},
        {"date": recent_time, "quantity": 3.0},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
        is_first_refresh=False,
    )

    await async_import_all_statistics(hass, coordinator)

    assert mock_add_stats.called
    stats = mock_add_stats.call_args[0][2]
    assert len(stats) == 2
    assert stats[0]["state"] == 5.0
    assert stats[1]["state"] == 3.0
    assert stats[1]["sum"] == 8.0


def test_parse_ami_datetime_preserves_minutes() -> None:
    """Test that _parse_ami_datetime keeps 15-min boundaries, not just top-of-hour."""
    dt = _parse_ami_datetime("2026-01-15T12:15:00.000Z")
    assert dt is not None
    assert dt.minute == 15, (
        "15-min timestamp should preserve minute=15, not truncate to 0"
    )
    assert dt.hour == 12
    assert dt.second == 0

    dt_30 = _parse_ami_datetime("2026-01-15T12:30:00.000Z")
    assert dt_30 is not None
    assert dt_30.minute == 30

    dt_45 = _parse_ami_datetime("2026-01-15T12:45:00.000Z")
    assert dt_45 is not None
    assert dt_45.minute == 45


def test_parse_ami_datetime_top_of_hour() -> None:
    """Test that top-of-hour timestamps still parse correctly."""
    dt = _parse_ami_datetime("2026-01-31T23:00:00.000Z")
    assert dt is not None
    assert dt.minute == 0
    assert dt.hour == 23
    assert dt.tzinfo is not None


def test_parse_ami_datetime_bad_input() -> None:
    """Test that unparseable dates return None."""
    assert _parse_ami_datetime("not-a-date") is None
    assert _parse_ami_datetime("") is None


# ---------------------------------------------------------------------------
# async_import_meter_statistics tests
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_meter_statistics_electric(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test async_import_meter_statistics imports electric stats for one SP."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [{"date": "2025-01-15T10:00:00.000Z", "quantity": 7.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )

    await async_import_meter_statistics(hass, coordinator, "SP1", force_import_all=True)

    assert mock_add_stats.called
    metadata = mock_add_stats.call_args[0][1]
    stats = mock_add_stats.call_args[0][2]
    assert metadata["statistic_id"] == "national_grid:acct1_SP1_electric_hourly_usage"
    assert stats[0]["state"] == 7.0


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_meter_statistics_gas(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test async_import_meter_statistics imports gas stats for one SP."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [{"date": "2025-01-15T10:00:00.000Z", "quantity": 4.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Gas")},
    )

    await async_import_meter_statistics(hass, coordinator, "SP1", force_import_all=True)

    assert mock_add_stats.called
    metadata = mock_add_stats.call_args[0][1]
    assert metadata["statistic_id"] == "national_grid:acct1_SP1_gas_hourly_usage"


async def test_import_meter_statistics_no_data(hass) -> None:
    """Test async_import_meter_statistics is a no-op when coordinator has no data."""
    coordinator = MagicMock()
    coordinator.data = None

    # Should not raise
    await async_import_meter_statistics(hass, coordinator, "SP1")


async def test_import_meter_statistics_no_readings(hass) -> None:
    """Test async_import_meter_statistics is a no-op when AMI readings are absent."""
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={},
        meters={"SP1": _make_meter_data("Electric")},
    )

    # Should not raise and should not call add_external_statistics
    with patch(
        "custom_components.national_grid.statistics.async_add_external_statistics"
    ) as mock_add:
        await async_import_meter_statistics(hass, coordinator, "SP1")
        assert not mock_add.called


async def test_import_meter_statistics_unknown_sp(hass) -> None:
    """Test async_import_meter_statistics is a no-op for unknown service point."""
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": [{"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0}]},
        meters={},  # SP1 not in meters
    )

    with patch(
        "custom_components.national_grid.statistics.async_add_external_statistics"
    ) as mock_add:
        await async_import_meter_statistics(hass, coordinator, "SP1")
        assert not mock_add.called


# ---------------------------------------------------------------------------
# Interval stats tests
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


def _recent_starttime(hours_ago: float = 2.0) -> str:
    """Return an ISO timestamp within yesterday's cutoff window."""
    dt = datetime.now(tz=UTC) - timedelta(hours=hours_ago)
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_interval_stats_electric(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test interval stats are imported when interval_reads are present."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    reads = [
        {"startTime": _recent_starttime(3), "value": 0.5},
        {"startTime": _recent_starttime(2), "value": 0.4},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": [{"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0}]},
        meters={"SP1": _make_meter_data("Electric")},
    )
    coordinator.data.interval_reads = {"SP1": reads}

    await async_import_all_statistics(hass, coordinator)

    assert mock_add_stats.called
    stat_ids = [call[0][1]["statistic_id"] for call in mock_add_stats.call_args_list]
    assert any("interval_usage" in sid for sid in stat_ids)


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_interval_stats_with_negative_values(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test that negative interval reads produce a separate return stats series."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    reads = [
        {"startTime": _recent_starttime(3), "value": 0.5},
        {"startTime": _recent_starttime(2), "value": -0.2},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": [{"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0}]},
        meters={"SP1": _make_meter_data("Electric")},
    )
    coordinator.data.interval_reads = {"SP1": reads}

    await async_import_all_statistics(hass, coordinator)

    stat_ids = [call[0][1]["statistic_id"] for call in mock_add_stats.call_args_list]
    assert any("interval_return_usage" in sid for sid in stat_ids)


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_interval_stats_no_data_within_window(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test that old interval reads (before cutoff) produce no stats."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    old_time = (datetime.now(tz=UTC) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    reads = [{"startTime": old_time, "value": 0.5}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={},
        meters={"SP1": _make_meter_data("Electric")},
    )
    coordinator.data.interval_reads = {"SP1": reads}

    await async_import_all_statistics(hass, coordinator)

    interval_calls = [
        c
        for c in mock_add_stats.call_args_list
        if "interval" in c[0][1]["statistic_id"]
    ]
    assert len(interval_calls) == 0


# ---------------------------------------------------------------------------
# _bucket_interval_reads tests
# ---------------------------------------------------------------------------


def test_bucket_interval_reads_consumption_only() -> None:
    """Test bucketing keeps only positive (consumption) values."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    reads = [
        {"startTime": _recent_starttime(3), "value": 0.5},
        {"startTime": _recent_starttime(2), "value": -0.2},
    ]
    result = _bucket_interval_reads(
        reads,
        cutoff.timestamp(),
        consumption_only=True,
        return_only=False,
        stat_type="consumption",
    )
    assert sum(result.values()) == pytest.approx(0.5)


def test_bucket_interval_reads_return_only() -> None:
    """Test bucketing keeps only negative values, stored as positive."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    reads = [
        {"startTime": _recent_starttime(3), "value": 0.5},
        {"startTime": _recent_starttime(2), "value": -0.2},
    ]
    result = _bucket_interval_reads(
        reads,
        cutoff.timestamp(),
        consumption_only=False,
        return_only=True,
        stat_type="return",
    )
    assert sum(result.values()) == pytest.approx(0.2)


def test_bucket_interval_reads_skips_old_reads() -> None:
    """Test bucketing drops readings older than cutoff."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    old_time = (datetime.now(tz=UTC) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    result = _bucket_interval_reads(
        [{"startTime": old_time, "value": 1.0}],
        cutoff.timestamp(),
        consumption_only=True,
        return_only=False,
        stat_type="consumption",
    )
    assert result == {}


def test_bucket_interval_reads_bad_timestamp() -> None:
    """Test bucketing skips reads with unparseable startTime."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = _bucket_interval_reads(
        [{"startTime": "not-a-timestamp", "value": 1.0}],
        cutoff.timestamp(),
        consumption_only=True,
        return_only=False,
        stat_type="consumption",
    )
    assert result == {}


def test_bucket_interval_reads_missing_starttime() -> None:
    """Test bucketing skips reads with no startTime key."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    result = _bucket_interval_reads(
        [{"value": 1.0}],
        cutoff.timestamp(),
        consumption_only=True,
        return_only=False,
        stat_type="consumption",
    )
    assert result == {}


def test_bucket_interval_reads_aggregates_into_hourly() -> None:
    """Test that multiple reads in the same hour are summed into one bucket."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    base = datetime.now(tz=UTC) - timedelta(hours=2)
    h0 = base.replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%S+00:00"
    reads = [
        {"startTime": h0.strftime(fmt), "value": 0.3},
        {"startTime": (h0 + timedelta(minutes=15)).strftime(fmt), "value": 0.4},
        {"startTime": (h0 + timedelta(minutes=30)).strftime(fmt), "value": 0.2},
    ]
    result = _bucket_interval_reads(
        reads,
        cutoff.timestamp(),
        consumption_only=False,
        return_only=False,
        stat_type="consumption",
    )
    assert len(result) == 1
    assert sum(result.values()) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Midnight refresh tests
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_midnight_refresh_continues_from_existing_sum(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test midnight refresh queries pre-window stats and continues the sum."""
    existing = {
        "national_grid:acct1_SP1_electric_hourly_usage": [
            {"sum": 50.0, "start": 1736848800.0}
        ]
    }
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(
        return_value=existing
    )
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    readings = [
        {"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0},
        {"date": "2025-01-15T11:00:00.000Z", "quantity": 3.0},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
        is_first_refresh=False,
    )
    coordinator.data.is_midnight_refresh = True
    coordinator.data.interval_reads = {}

    await async_import_all_statistics(hass, coordinator)

    assert mock_add_stats.called
    stats = mock_add_stats.call_args[0][2]
    assert stats[0]["sum"] == pytest.approx(55.0)


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_midnight_refresh_no_existing_stats(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test midnight refresh with no pre-window stats starts sum from 0."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    readings = [{"date": "2025-01-15T10:00:00.000Z", "quantity": 7.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
        is_first_refresh=False,
    )
    coordinator.data.is_midnight_refresh = True
    coordinator.data.interval_reads = {}

    await async_import_all_statistics(hass, coordinator)

    assert mock_add_stats.called
    stats = mock_add_stats.call_args[0][2]
    assert stats[0]["sum"] == pytest.approx(7.0)


# ---------------------------------------------------------------------------
# Electric return (has_negative) AMI stats
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_hourly_stats_with_negative_returns_two_series(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test negative AMI readings produce both consumption and return series."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    readings = [
        {"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0},
        {"date": "2025-01-15T11:00:00.000Z", "quantity": -2.0},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )
    coordinator.data.interval_reads = {}

    await async_import_all_statistics(hass, coordinator)

    stat_ids = [call[0][1]["statistic_id"] for call in mock_add_stats.call_args_list]
    assert any(
        "electric_hourly_usage" in sid and "return" not in sid for sid in stat_ids
    )
    assert any("return_hourly_usage" in sid for sid in stat_ids)


# ---------------------------------------------------------------------------
# Midnight refresh — readings with no parseable date
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_midnight_refresh_readings_with_no_date(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test midnight refresh where all readings have no date skips the window query.

    Covers the 'if not date_str: continue' branch inside the midnight refresh
    earliest-timestamp loop (statistics.py _get_last_sum_and_ts).
    """
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})
    mock_get_instance.return_value.async_clear_statistics = MagicMock()

    # Readings with no "date" key — earliest_ts will remain None after the loop
    readings = [{"quantity": 5.0}]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
        is_first_refresh=False,
    )
    coordinator.data.is_midnight_refresh = True
    coordinator.data.interval_reads = {}

    await async_import_all_statistics(hass, coordinator)

    # No parseable dates → no stats to import
    assert not mock_add_stats.called


# ---------------------------------------------------------------------------
# _bucket_interval_reads — mixed old and recent reads
# ---------------------------------------------------------------------------


def test_bucket_interval_reads_mixed_old_and_recent() -> None:
    """Test skipped_old log branch fires when some reads are older than cutoff."""
    cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    old_time = (datetime.now(tz=UTC) - timedelta(days=10)).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00"
    )
    recent_time = _recent_starttime(2)
    result = _bucket_interval_reads(
        [
            {"startTime": old_time, "value": 1.0},
            {"startTime": recent_time, "value": 0.5},
        ],
        cutoff.timestamp(),
        consumption_only=True,
        return_only=False,
        stat_type="consumption",
    )
    # Only the recent read should appear in buckets
    assert sum(result.values()) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# data.py module coverage
# ---------------------------------------------------------------------------


def test_data_module_importable() -> None:
    """Test the data module can be imported (covers the type alias definition)."""
    from custom_components.national_grid import data

    assert hasattr(data, "NationalGridConfigEntry")


@patch("custom_components.national_grid.statistics.async_add_external_statistics")
@patch("custom_components.national_grid.statistics.get_instance")
async def test_import_ami_stats_with_negative_creates_return_series(
    mock_get_instance, mock_add_stats, hass
) -> None:
    """Test negative AMI readings produce a separate electric return stat series."""
    mock_get_instance.return_value.async_add_executor_job = AsyncMock(return_value={})

    readings = [
        {"date": "2025-01-15T10:00:00.000Z", "quantity": 5.0},
        {"date": "2025-01-15T11:00:00.000Z", "quantity": -1.5},
    ]
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={"SP1": readings},
        meters={"SP1": _make_meter_data("Electric")},
    )

    await async_import_all_statistics(hass, coordinator)

    stat_ids = [call[0][1]["statistic_id"] for call in mock_add_stats.call_args_list]
    assert any("electric_return_hourly_usage" in sid for sid in stat_ids)
    assert any("acct1_SP1" in sid for sid in stat_ids)


async def test_import_all_statistics_skips_interval_reads_with_unknown_sp(hass) -> None:
    """Test interval_reads for unknown service points are skipped gracefully."""
    coordinator = MagicMock()
    coordinator.data = _make_coordinator_data(
        ami_usages={},
        meters={},  # no meters registered
    )
    # interval_reads references a SP that has no meter entry
    coordinator.data.interval_reads = {
        "SP_UNKNOWN": [{"startTime": "2025-01-15T10:00:00+00:00", "value": 0.5}]
    }

    with patch(
        "custom_components.national_grid.statistics.async_add_external_statistics"
    ) as mock_add:
        await async_import_all_statistics(hass, coordinator)
        assert not mock_add.called
