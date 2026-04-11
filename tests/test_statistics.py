"""Tests for the National Grid statistics module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.national_grid.coordinator import (
    MeterData,
    NationalGridCoordinatorData,
)
from custom_components.national_grid.statistics import (
    _parse_ami_datetime,
    async_import_all_statistics,
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
    assert metadata["statistic_id"] == "national_grid:SP1_electric_hourly_usage"
    assert len(stats) == 2
    assert stats[0]["state"] == 5.0
    assert stats[1]["sum"] == 8.0


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
        "national_grid:SP1_electric_hourly_usage": [
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
