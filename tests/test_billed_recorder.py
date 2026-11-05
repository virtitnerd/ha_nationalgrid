"""Check billed statistics against Home Assistant's real statistics recorder."""

from datetime import date
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    get_metadata,
)
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.national_grid_us.statistics import (
    _import_daily_series,
    _resolve_billed_stat_info,
)

DAILY = {date(2026, 1, 6): 2.0, date(2026, 1, 7): 2.0, date(2026, 1, 8): 1.0}


async def _import(hass, *, is_cost: bool) -> tuple[str, dict, float]:
    stat_id, name, unit, unit_class = _resolve_billed_stat_info(
        "200", "acct1", is_gas=True, is_cost=is_cost
    )
    await _import_daily_series(hass, stat_id, name, unit, unit_class, DAILY)
    await async_wait_recording_done(hass)
    instance = get_instance(hass)
    meta = await instance.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={stat_id})
    )
    last = await instance.async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    return stat_id, meta[stat_id][1], last[stat_id][0]["sum"]


async def test_billed_gas_series_are_stored_in_therms(hass) -> None:
    """Billed gas usage is therms in the energy class and cost carries no unit."""
    _, usage_meta, usage_sum = await _import(hass, is_cost=False)
    assert usage_meta["unit_of_measurement"] == "thm"
    assert usage_meta["unit_class"] == "energy"
    assert usage_sum == 5.0

    _, cost_meta, cost_sum = await _import(hass, is_cost=True)
    assert cost_meta["unit_of_measurement"] is None
    assert cost_meta["unit_class"] is None
    assert cost_sum == 5.0


async def test_repeat_refresh_leaves_billed_sums_unchanged(hass) -> None:
    """Rewriting the same window on every refresh does not double count."""
    await _import(hass, is_cost=False)
    _, _, usage_sum = await _import(hass, is_cost=False)
    assert usage_sum == 5.0
