"""Tests for the National Grid coordinator."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from py_nationalgrid.exceptions import (
    CannotConnectError,
    InvalidAuthError,
    NationalGridError,
)

from custom_components.national_grid.const import (
    _LOGGER,
    CONF_SELECTED_ACCOUNTS,
    DOMAIN,
)
from custom_components.national_grid.coordinator import (
    NationalGridDataUpdateCoordinator,
)

from .conftest import (
    MOCK_ACCOUNT_ID,
    MOCK_SERVICE_POINT,
    _mock_ami_usages,
    _mock_billing_account,
    _mock_costs,
    _mock_usages,
)


def _make_coordinator(
    hass: HomeAssistant, api_mock: AsyncMock
) -> NationalGridDataUpdateCoordinator:
    """Create a coordinator with a mock API client and config entry."""
    with (
        patch(
            "custom_components.national_grid.coordinator.async_create_clientsession",
        ),
        patch(
            "custom_components.national_grid.coordinator.NationalGridClient",
            return_value=api_mock,
        ),
    ):
        coordinator = NationalGridDataUpdateCoordinator(
            hass=hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
            username="test@example.com",
            password="password",
        )
    mock_entry = MagicMock()
    mock_entry.data = {CONF_SELECTED_ACCOUNTS: [MOCK_ACCOUNT_ID]}
    coordinator.config_entry = mock_entry
    return coordinator


def _make_api() -> AsyncMock:
    """Create a mock py_nationalgrid client."""
    api = AsyncMock()
    api.get_billing_account = AsyncMock(return_value=_mock_billing_account())
    api.get_energy_usages = AsyncMock(return_value=_mock_usages())
    api.get_energy_usage_costs = AsyncMock(return_value=_mock_costs())
    api.get_ami_energy_usages = AsyncMock(return_value=_mock_ami_usages())
    api.get_ami_energy_usages_15min = AsyncMock(return_value=_mock_ami_usages())
    return api


async def test_coordinator_fetches_data(hass: HomeAssistant) -> None:
    """Test coordinator fetches and structures data correctly."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)

    data = await coordinator._async_update_data()

    assert MOCK_ACCOUNT_ID in data.accounts
    assert len(data.meters) == 2
    assert MOCK_ACCOUNT_ID in data.usages
    assert MOCK_ACCOUNT_ID in data.costs


async def test_coordinator_auth_error_raises(hass: HomeAssistant) -> None:
    """Test coordinator raises ConfigEntryAuthFailed on auth error."""
    api = _make_api()
    api.get_billing_account = AsyncMock(
        side_effect=InvalidAuthError("Bad creds"),
    )
    coordinator = _make_coordinator(hass, api)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_coordinator_api_error_per_account(hass: HomeAssistant) -> None:
    """Test coordinator handles per-account API errors gracefully."""
    api = _make_api()
    api.get_billing_account = AsyncMock(
        side_effect=NationalGridError("Server error"),
    )
    coordinator = _make_coordinator(hass, api)

    # Per-account errors are caught and the account is skipped
    data = await coordinator._async_update_data()
    assert len(data.accounts) == 0
    assert len(data.meters) == 0


async def test_get_latest_usage(hass: HomeAssistant) -> None:
    """Test get_latest_usage filters by fuel type and returns most recent."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)

    # Populate coordinator data
    coordinator.data = await coordinator._async_update_data()

    # Electric usage (TOTAL_KWH)
    usage = coordinator.get_latest_usage(MOCK_ACCOUNT_ID, fuel_type="Electric")
    assert usage is not None
    assert usage["usageType"] == "TOTAL_KWH"
    assert usage["usageYearMonth"] == 202501

    # Gas usage (THERMS)
    usage = coordinator.get_latest_usage(MOCK_ACCOUNT_ID, fuel_type="Gas")
    assert usage is not None
    assert usage["usageType"] == "THERMS"

    # No filter
    usage = coordinator.get_latest_usage(MOCK_ACCOUNT_ID)
    assert usage is not None
    assert usage["usageYearMonth"] == 202501


async def test_get_latest_cost(hass: HomeAssistant) -> None:
    """Test get_latest_cost filters by fuel type."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)

    coordinator.data = await coordinator._async_update_data()

    cost = coordinator.get_latest_cost(MOCK_ACCOUNT_ID, fuel_type="Electric")
    assert cost is not None
    assert cost["fuelType"] == "ELECTRIC"

    cost = coordinator.get_latest_cost(MOCK_ACCOUNT_ID, fuel_type="Gas")
    assert cost is not None
    assert cost["fuelType"] == "GAS"


async def test_coordinator_logs_unavailable_on_failure(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test WARNING logged on first failure."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    # _fetch_all_data catches per-account errors, so we need to make
    # the coordinator's _fetch_all_data itself raise.
    coordinator._fetch_all_data = AsyncMock(
        side_effect=NationalGridError("Server down"),
    )

    with caplog.at_level(logging.WARNING), pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert "National Grid service unavailable" in caplog.text
    assert coordinator._previous_update_success is False


async def test_coordinator_logs_recovery(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test INFO logged on recovery after failure."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    # Simulate previous failure
    coordinator._previous_update_success = False

    with caplog.at_level(logging.INFO):
        await coordinator._async_update_data()

    assert "National Grid service recovered" in caplog.text
    assert coordinator._previous_update_success is True


async def test_coordinator_no_duplicate_unavailable_log(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test consecutive failures only log WARNING once."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._fetch_all_data = AsyncMock(
        side_effect=NationalGridError("Server down"),
    )

    # First failure
    with caplog.at_level(logging.WARNING), pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    first_count = caplog.text.count("National Grid service unavailable")

    # Second failure
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    second_count = caplog.text.count("National Grid service unavailable")

    assert first_count == 1
    assert second_count == 1  # No additional log


async def test_get_all_usages(hass: HomeAssistant) -> None:
    """Test get_all_usages returns usages for account."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    usages = coordinator.get_all_usages(MOCK_ACCOUNT_ID)
    assert usages is not None
    assert len(usages) > 0


async def test_get_all_costs(hass: HomeAssistant) -> None:
    """Test get_all_costs returns costs for account."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    costs = coordinator.get_all_costs(MOCK_ACCOUNT_ID)
    assert costs is not None
    assert len(costs) > 0


async def test_get_latest_ami_usage(hass: HomeAssistant) -> None:
    """Test get_latest_ami_usage returns AMI data for service point."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    ami = coordinator.get_latest_ami_usage(MOCK_SERVICE_POINT)
    assert ami is not None


async def test_fetch_usages_error_graceful(hass: HomeAssistant) -> None:
    """Test _fetch_all_data handles usages error gracefully."""
    api = _make_api()
    api.get_energy_usages = AsyncMock(
        side_effect=NationalGridError("usage fail"),
    )
    coordinator = _make_coordinator(hass, api)
    data = await coordinator._async_update_data()
    # Usages should be empty list for that account
    assert data.usages[MOCK_ACCOUNT_ID] == []


async def test_fetch_costs_error_graceful(hass: HomeAssistant) -> None:
    """Test _fetch_all_data handles costs error gracefully."""
    api = _make_api()
    api.get_energy_usage_costs = AsyncMock(
        side_effect=NationalGridError("cost fail"),
    )
    coordinator = _make_coordinator(hass, api)
    data = await coordinator._async_update_data()
    assert data.costs[MOCK_ACCOUNT_ID] == []


async def test_fetch_costs_no_region(hass: HomeAssistant) -> None:
    """Test _fetch_all_data handles missing region gracefully."""
    api = _make_api()
    billing = _mock_billing_account()
    billing["region"] = ""
    api.get_billing_account = AsyncMock(return_value=billing)
    coordinator = _make_coordinator(hass, api)
    data = await coordinator._async_update_data()
    assert data.costs[MOCK_ACCOUNT_ID] == []


async def test_fetch_ami_error_graceful(hass: HomeAssistant) -> None:
    """Test _fetch_all_data handles AMI error gracefully when both methods fail."""
    api = _make_api()
    api.get_ami_energy_usages = AsyncMock(side_effect=NationalGridError("ami fail"))
    api.get_ami_energy_usages_15min = AsyncMock(
        side_effect=NationalGridError("15min also fail"),
    )
    coordinator = _make_coordinator(hass, api)
    data = await coordinator._async_update_data()
    # AMI usages should not contain the service point that failed
    assert MOCK_SERVICE_POINT not in data.ami_usages


async def test_fetch_ami_error_incremental_pass2_still_runs(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test bulk AMI failure still provides recent 15-min data via Pass 2."""
    api = _make_api()
    api.get_ami_energy_usages = AsyncMock(
        side_effect=NationalGridError("incremental fail")
    )
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    with caplog.at_level(logging.DEBUG):
        data = await coordinator._async_update_data()

    # Pass 2 (recent 72 h) still ran and provided data
    assert MOCK_SERVICE_POINT in data.ami_usages
    # 15-min was called with the recent ~3-day window, not a 50-day fallback
    call_kwargs = api.get_ami_energy_usages_15min.call_args
    days_back = (datetime.now(UTC).date() - call_kwargs.kwargs["date_from"]).days
    assert 2 <= days_back <= 4, f"Expected ~3-day recent window, got {days_back}"
    assert "Could not fetch bulk AMI for meter" in caplog.text


async def test_fetch_ami_falls_back_to_15min_on_primary_failure(
    hass: HomeAssistant,
) -> None:
    """Test first-refresh fallback: 50-day 15-min call made, then Pass 2 runs."""
    api = _make_api()
    api.get_ami_energy_usages = AsyncMock(side_effect=NationalGridError("daily fail"))
    coordinator = _make_coordinator(hass, api)
    data = await coordinator._async_update_data()

    assert MOCK_SERVICE_POINT in data.ami_usages

    # Two 15-min calls: one 50-day fallback (Pass 1) + one recent 3-day (Pass 2)
    calls = api.get_ami_energy_usages_15min.call_args_list
    assert len(calls) >= 2, f"Expected ≥2 calls (fallback + recent), got {len(calls)}"
    windows = [(datetime.now(UTC).date() - c.kwargs["date_from"]).days for c in calls]
    assert any(45 <= d <= 55 for d in windows), (
        f"Expected a ~50-day fallback call, got windows: {windows}"
    )


async def test_get_meter_data_none_when_no_data(hass: HomeAssistant) -> None:
    """Test get_meter_data returns None when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_meter_data("SP001") is None


async def test_get_latest_usage_none_when_no_data(hass: HomeAssistant) -> None:
    """Test get_latest_usage returns None when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_latest_usage("acct1") is None


async def test_get_latest_usage_none_when_no_usages(hass: HomeAssistant) -> None:
    """Test get_latest_usage returns None when account has no usages."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_latest_usage("nonexistent_account") is None


async def test_get_latest_usage_filtered_empty(hass: HomeAssistant) -> None:
    """Test get_latest_usage returns None when fuel type filter matches nothing."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_latest_usage(MOCK_ACCOUNT_ID, fuel_type="Solar") is None


async def test_get_latest_cost_none_when_no_data(hass: HomeAssistant) -> None:
    """Test get_latest_cost returns None when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_latest_cost("acct1") is None


async def test_get_latest_cost_none_when_no_costs(hass: HomeAssistant) -> None:
    """Test get_latest_cost returns None when account has no costs."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_latest_cost("nonexistent_account") is None


async def test_get_latest_cost_filtered_empty(hass: HomeAssistant) -> None:
    """Test get_latest_cost returns None when fuel type filter matches nothing."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_latest_cost(MOCK_ACCOUNT_ID, fuel_type="Solar") is None


async def test_get_all_usages_none_data(hass: HomeAssistant) -> None:
    """Test get_all_usages returns empty when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_all_usages("acct1") == []


async def test_get_all_usages_no_account(hass: HomeAssistant) -> None:
    """Test get_all_usages returns empty for unknown account."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_all_usages("nonexistent") == []


async def test_get_all_usages_with_fuel_filter(hass: HomeAssistant) -> None:
    """Test get_all_usages filters by fuel type."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    gas_usages = coordinator.get_all_usages(MOCK_ACCOUNT_ID, fuel_type="Gas")
    assert all(u.get("usageType") == "THERMS" for u in gas_usages)


async def test_get_all_costs_none_data(hass: HomeAssistant) -> None:
    """Test get_all_costs returns empty when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_all_costs("acct1") == []


async def test_get_all_costs_no_account(hass: HomeAssistant) -> None:
    """Test get_all_costs returns empty for unknown account."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_all_costs("nonexistent") == []


async def test_get_all_costs_with_fuel_filter(hass: HomeAssistant) -> None:
    """Test get_all_costs filters by fuel type."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    electric_costs = coordinator.get_all_costs(MOCK_ACCOUNT_ID, fuel_type="Electric")
    assert all(c.get("fuelType") == "ELECTRIC" for c in electric_costs)


async def test_get_latest_ami_usage_none_data(hass: HomeAssistant) -> None:
    """Test get_latest_ami_usage returns None when data is None."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None
    assert coordinator.get_latest_ami_usage("SP001") is None


async def test_get_latest_ami_usage_no_readings(hass: HomeAssistant) -> None:
    """Test get_latest_ami_usage returns None when no readings exist."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.get_latest_ami_usage("NONEXISTENT_SP") is None


# ---------------------------------------------------------------------------
# async_initialize tests
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.coordinator.Store")
async def test_async_initialize_skips_first_refresh_when_done(
    mock_store_cls, hass: HomeAssistant
) -> None:
    """Test async_initialize sets _is_first_refresh=False when flag is persisted."""
    mock_store = AsyncMock()
    mock_store.async_load = AsyncMock(return_value={"initial_import_done": True})
    mock_store_cls.return_value = mock_store

    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    assert coordinator._is_first_refresh is True  # default

    await coordinator.async_initialize()

    assert coordinator._is_first_refresh is False


@patch("custom_components.national_grid.coordinator.Store")
async def test_async_initialize_allows_first_refresh_when_not_done(
    mock_store_cls, hass: HomeAssistant
) -> None:
    """Test async_initialize keeps _is_first_refresh=True when flag is absent."""
    mock_store = AsyncMock()
    mock_store.async_load = AsyncMock(return_value={})
    mock_store_cls.return_value = mock_store

    api = _make_api()
    coordinator = _make_coordinator(hass, api)

    await coordinator.async_initialize()

    assert coordinator._is_first_refresh is True


@patch("custom_components.national_grid.coordinator.Store")
async def test_async_initialize_allows_first_refresh_when_store_empty(
    mock_store_cls, hass: HomeAssistant
) -> None:
    """Test async_initialize keeps _is_first_refresh=True when store returns None."""
    mock_store = AsyncMock()
    mock_store.async_load = AsyncMock(return_value=None)
    mock_store_cls.return_value = mock_store

    api = _make_api()
    coordinator = _make_coordinator(hass, api)

    await coordinator.async_initialize()

    assert coordinator._is_first_refresh is True


# ---------------------------------------------------------------------------
# async_force_refresh_meter tests
# ---------------------------------------------------------------------------


@patch(
    "custom_components.national_grid.statistics.async_import_meter_statistics",
    new_callable=AsyncMock,
)
async def test_async_force_refresh_meter_fetches_from_epoch(
    mock_import, hass: HomeAssistant
) -> None:
    """Test force refresh calls primary AMI method with epoch and imports stats."""
    from datetime import date

    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    await coordinator.async_force_refresh_meter(MOCK_SERVICE_POINT)

    call_kwargs = api.get_ami_energy_usages.call_args
    date_from = call_kwargs.kwargs["date_from"]
    assert date_from == date(1970, 1, 1), f"Expected epoch, got {date_from}"

    # Statistics import should have been called for this service point
    mock_import.assert_called_once()
    _, kwargs = mock_import.call_args
    assert kwargs.get("force_import_all") is True


async def test_async_force_refresh_meter_no_data(hass: HomeAssistant) -> None:
    """Test force refresh is a no-op when coordinator has no data."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None

    # Should not raise
    await coordinator.async_force_refresh_meter(MOCK_SERVICE_POINT)
    # API should not have been called a second time
    api.get_ami_energy_usages.assert_not_called()


async def test_async_force_refresh_meter_unknown_sp(hass: HomeAssistant) -> None:
    """Test force refresh is a no-op for unknown service point."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    call_count_before = api.get_ami_energy_usages.call_count
    await coordinator.async_force_refresh_meter("UNKNOWN_SP")
    assert api.get_ami_energy_usages.call_count == call_count_before


# ---------------------------------------------------------------------------
# async_refresh_interval_only tests
# ---------------------------------------------------------------------------


async def test_async_refresh_interval_only_sets_and_clears_mode(
    hass: HomeAssistant,
) -> None:
    """Test interval-only mode flag is set during refresh and cleared after."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    mode_during_refresh = None

    original_fetch = coordinator._fetch_all_data

    async def _capture_mode() -> object:
        nonlocal mode_during_refresh
        mode_during_refresh = coordinator._interval_only_mode
        return await original_fetch()

    coordinator._fetch_all_data = _capture_mode
    await coordinator.async_refresh_interval_only()

    assert mode_during_refresh is True
    assert coordinator._interval_only_mode is False


async def test_async_refresh_interval_only_skips_ami_fetch(
    hass: HomeAssistant,
) -> None:
    """Test interval-only refresh does not call get_ami_energy_usages."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False
    await coordinator.async_refresh_interval_only()

    api.get_ami_energy_usages.assert_not_called()


async def test_async_refresh_interval_only_fetches_interval_reads(
    hass: HomeAssistant,
) -> None:
    """Test interval-only refresh still calls get_interval_reads."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False
    await coordinator.async_refresh_interval_only()

    api.get_interval_reads.assert_called()


# ---------------------------------------------------------------------------
# async_refresh_full_with_clear tests
# ---------------------------------------------------------------------------


async def test_async_refresh_full_with_clear_sets_and_clears_mode(
    hass: HomeAssistant,
) -> None:
    """Test midnight refresh flag is set during refresh and cleared after."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    mode_during_refresh = None

    original_fetch = coordinator._fetch_all_data

    async def _capture_mode() -> object:
        nonlocal mode_during_refresh
        mode_during_refresh = coordinator._is_midnight_refresh
        return await original_fetch()

    coordinator._fetch_all_data = _capture_mode
    await coordinator.async_refresh_full_with_clear()

    assert mode_during_refresh is True
    assert coordinator._is_midnight_refresh is False


async def test_async_refresh_full_with_clear_clears_pending_on_success(
    hass: HomeAssistant,
) -> None:
    """Test pending_full_refresh is cleared after a successful full refresh."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._pending_full_refresh = True

    await coordinator.async_refresh_full_with_clear()

    assert coordinator._pending_full_refresh is False


async def test_async_refresh_full_with_clear_sets_pending_on_failure(
    hass: HomeAssistant,
) -> None:
    """Test pending_full_refresh is set when the full refresh fails."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._fetch_all_data = AsyncMock(
        side_effect=NationalGridError("boom"),
    )

    await coordinator.async_refresh_full_with_clear()

    assert coordinator._pending_full_refresh is True


# ---------------------------------------------------------------------------
# reset_to_first_refresh tests
# ---------------------------------------------------------------------------


@patch("custom_components.national_grid.coordinator.Store")
async def test_reset_to_first_refresh_sets_flag(
    mock_store_cls, hass: HomeAssistant
) -> None:
    """Test reset_to_first_refresh sets _is_first_refresh back to True."""
    mock_store = AsyncMock()
    mock_store.async_load = AsyncMock(return_value={"initial_import_done": True})
    mock_store.async_save = AsyncMock()
    mock_store_cls.return_value = mock_store

    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    await coordinator.async_initialize()
    assert coordinator._is_first_refresh is False

    coordinator.reset_to_first_refresh()
    assert coordinator._is_first_refresh is True


# ---------------------------------------------------------------------------
# _seed_from_previous tests
# ---------------------------------------------------------------------------


async def test_seed_from_previous_returns_empty_when_no_data(
    hass: HomeAssistant,
) -> None:
    """Test _seed_from_previous returns empty data when coordinator.data is None."""
    from custom_components.national_grid.coordinator import NationalGridCoordinatorData

    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = None

    seeded = coordinator._seed_from_previous()
    assert isinstance(seeded, NationalGridCoordinatorData)
    assert seeded.accounts == {}
    assert seeded.meters == {}
    assert seeded.usages == {}
    assert seeded.costs == {}
    assert seeded.ami_usages == {}
    assert seeded.interval_reads == {}


async def test_seed_from_previous_copies_previous_data(hass: HomeAssistant) -> None:
    """Test _seed_from_previous copies data from the previous fetch."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator.data = await coordinator._async_update_data()

    seeded = coordinator._seed_from_previous()
    assert MOCK_ACCOUNT_ID in seeded.accounts
    assert len(seeded.meters) == 2
    assert MOCK_ACCOUNT_ID in seeded.usages
    assert MOCK_ACCOUNT_ID in seeded.costs


# ---------------------------------------------------------------------------
# First refresh date window test
# ---------------------------------------------------------------------------


async def test_incremental_full_refresh_uses_7_day_window(
    hass: HomeAssistant,
) -> None:
    """Test non-first full refresh fetches AMI from today-7d (incremental window)."""
    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    await coordinator._async_update_data()

    call_kwargs = api.get_ami_energy_usages.call_args
    date_from = call_kwargs.kwargs["date_from"]
    days_back = (datetime.now(UTC).date() - date_from).days
    assert 6 <= days_back <= 8, f"Expected 7-day incremental window, got {days_back}"


def test_log_ami_results_no_dates(caplog: pytest.LogCaptureFixture) -> None:
    """Test _log_ami_results when readings exist but have no date field."""
    import logging

    from custom_components.national_grid.coordinator import (
        NationalGridDataUpdateCoordinator,
    )

    readings = [{"quantity": 5.0}]  # no "date" key
    with caplog.at_level(logging.DEBUG):
        NationalGridDataUpdateCoordinator._log_ami_results(readings, "SP_TEST")

    assert "1 AMI records for meter SP_TEST" in caplog.text


def test_log_ami_results_empty(caplog: pytest.LogCaptureFixture) -> None:
    """Test _log_ami_results when ami_data is an empty list."""
    import logging

    from custom_components.national_grid.coordinator import (
        NationalGridDataUpdateCoordinator,
    )

    with caplog.at_level(logging.DEBUG):
        NationalGridDataUpdateCoordinator._log_ami_results([], "SP_TEST")

    assert "No AMI records returned for meter SP_TEST" in caplog.text


async def test_interval_reads_skips_gas_meter(hass: HomeAssistant) -> None:
    """Test _fetch_interval_reads is skipped for AMI-capable Gas meters."""
    api = _make_api()
    gas_billing = {
        "billingAccountId": MOCK_ACCOUNT_ID,
        "region": "KEDNY",
        "premiseNumber": "PREM001",
        "meter": {
            "nodes": [
                {
                    "servicePointNumber": MOCK_SERVICE_POINT,
                    "meterNumber": "MTR001",
                    "meterPointNumber": "MPT001",
                    "fuelType": "Gas",
                    "hasAmiSmartMeter": True,
                }
            ],
        },
    }
    api.get_billing_account = AsyncMock(return_value=gas_billing)
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    await coordinator._async_update_data()

    api.get_interval_reads.assert_not_called()


async def test_interval_reads_log_without_starttime(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _fetch_interval_reads logs when reads have no startTime."""
    import logging

    api = _make_api()
    api.get_interval_reads = AsyncMock(return_value=[{"value": 0.5}])
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_update_data()

    assert "Fetched 1 interval reads for meter" in caplog.text


async def test_ami_fetch_skips_meter_with_no_service_point(
    hass: HomeAssistant,
) -> None:
    """Test _fetch_ami_data skips AMI meters that have no servicePointNumber."""
    api = _make_api()
    billing = {
        "billingAccountId": MOCK_ACCOUNT_ID,
        "region": "KEDNY",
        "premiseNumber": "PREM001",
        "meter": {
            "nodes": [
                {
                    "servicePointNumber": "",
                    "meterNumber": "MTR001",
                    "fuelType": "Electric",
                    "hasAmiSmartMeter": True,
                }
            ],
        },
    }
    api.get_billing_account = AsyncMock(return_value=billing)
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    await coordinator._async_update_data()

    api.get_ami_energy_usages.assert_not_called()
    api.get_interval_reads.assert_not_called()


async def test_interval_reads_empty_response(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _fetch_interval_reads logs when API returns empty list."""
    api = _make_api()
    api.get_interval_reads = AsyncMock(return_value=[])
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_update_data()

    assert "No interval reads returned for meter" in caplog.text


async def test_interval_reads_exception_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test _fetch_interval_reads logs and swallows known API exceptions."""
    api = _make_api()
    api.get_interval_reads = AsyncMock(side_effect=CannotConnectError("timeout"))
    coordinator = _make_coordinator(hass, api)
    coordinator._is_first_refresh = False

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_update_data()

    assert "Could not fetch interval reads for meter" in caplog.text


async def test_first_refresh_ami_uses_epoch(hass: HomeAssistant) -> None:
    """Test first refresh calls primary AMI method with epoch as date_from."""
    from datetime import date

    api = _make_api()
    coordinator = _make_coordinator(hass, api)
    assert coordinator._is_first_refresh is True

    await coordinator._async_update_data()

    call_kwargs = api.get_ami_energy_usages.call_args
    date_from = call_kwargs.kwargs["date_from"]
    assert date_from == date(1970, 1, 1), f"Expected epoch, got {date_from}"
