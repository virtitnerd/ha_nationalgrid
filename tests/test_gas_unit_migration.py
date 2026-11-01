"""Check the gas CCF to therm relabel against Home Assistant's real recorder."""

from datetime import UTC, datetime
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    get_metadata,
)
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.national_grid_us import _async_migrate_gas_ccf_to_therm
from custom_components.national_grid_us.statistics import _build_statistic_metadata

STAT_ID = "national_grid_us:acct_100_gas_hourly_usage"


async def _read(hass):
    await async_wait_recording_done(hass)
    instance = get_instance(hass)
    meta = await instance.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={STAT_ID})
    )
    last = await instance.async_add_executor_job(
        get_last_statistics, hass, 1, STAT_ID, True, {"sum"}
    )
    return meta[STAT_ID][1], last[STAT_ID][0]["sum"]


async def test_ccf_series_is_relabeled_and_keeps_its_history(hass) -> None:
    """A CCF-labeled series becomes therms in place with its sums untouched."""
    old_meta = _build_statistic_metadata(STAT_ID, "Gas Hourly Usage", "CCF", "volume")
    start = datetime(2026, 10, 25, 4, tzinfo=UTC)
    async_add_external_statistics(
        hass, old_meta, [{"start": start, "state": 2.0, "sum": 42.5}]
    )
    meta, total = await _read(hass)
    assert meta["unit_of_measurement"] == "CCF"

    await _async_migrate_gas_ccf_to_therm(hass)
    meta, total = await _read(hass)

    assert meta["unit_of_measurement"] == "thm"
    assert meta["unit_class"] == "energy"
    assert total == 42.5

    await _async_migrate_gas_ccf_to_therm(hass)
    meta, total = await _read(hass)
    assert meta["unit_of_measurement"] == "thm"
    assert total == 42.5
