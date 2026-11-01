"""Check a first or forced full refresh backfills a series that already has data."""

from datetime import UTC, datetime

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.national_grid_us.statistics import (
    _build_statistic_metadata,
    async_import_all_statistics,
)

from .conftest import MOCK_ACCOUNT_ID
from .test_coordinator import _make_api, _make_coordinator

SERVICE_POINT = "sp001"
STAT_ID = f"national_grid_us:{MOCK_ACCOUNT_ID}_{SERVICE_POINT}_electric_hourly_usage"


def _hourly(day: int) -> dict:
    return {
        "date": f"2026-09-{day:02d}T12:00:00.000-04:00",
        "fuelType": "ELECTRIC",
        "quantity": 1.0,
    }


async def _read(hass) -> list[dict]:
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 9, 1, tzinfo=UTC),
        datetime(2026, 10, 1, tzinfo=UTC),
        {STAT_ID},
        "hour",
        None,
        {"sum", "change"},
    )
    return rows[STAT_ID]


@pytest.mark.parametrize("forced", [False, True])
async def test_full_refresh_backfills_older_history(hass, forced) -> None:
    """A series holding only recent hours gains the older ones on a full refresh."""
    api = _make_api()
    api.get_billing_account.return_value["meter"]["nodes"][0]["servicePointNumber"] = (
        SERVICE_POINT
    )
    api.get_ami_energy_usages.return_value = [_hourly(20), _hourly(21), _hourly(22)]
    api.get_ami_energy_usages_15min.return_value = []
    coordinator = _make_coordinator(hass, api)

    async_add_external_statistics(
        hass,
        _build_statistic_metadata(STAT_ID, "Electric Hourly Usage", "kWh", "energy"),
        [{"start": datetime(2026, 9, 22, 16, tzinfo=UTC), "state": 1.0, "sum": 1.0}],
    )
    await async_wait_recording_done(hass)

    if forced:
        coordinator._is_first_refresh = False
        coordinator.reset_to_first_refresh()
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.data.is_first_refresh

    await async_import_all_statistics(hass, coordinator)
    rows = await _read(hass)

    assert [row["sum"] for row in rows] == pytest.approx([1.0, 2.0, 3.0])
    assert [row["change"] for row in rows] == pytest.approx([1.0, 1.0, 1.0])

    coordinator.data = await coordinator._async_update_data()
    assert not coordinator.data.is_first_refresh
