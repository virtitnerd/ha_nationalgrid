"""Check AMI refreshes against Home Assistant's actual statistics recorder."""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.national_grid_us.statistics import async_import_all_statistics

from .conftest import MOCK_ACCOUNT_ID
from .test_coordinator import _make_api, _make_coordinator

SERVICE_POINT = "sp001"


async def _read_stats(hass):
    await async_wait_recording_done(hass)
    return await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime(2026, 9, 20, tzinfo=UTC),
        datetime(2026, 10, 6, tzinfo=UTC),
        None,
        "hour",
        None,
        {"state", "sum", "change"},
    )


@pytest.mark.parametrize("repair_old_overlap", [False, True])
async def test_daily_and_hourly_refreshes_count_each_day_once(hass, repair_old_overlap):
    """Twelve cycles preserve totals and repair a previously doubled day."""
    api = _make_api()
    api.get_billing_account.return_value["meter"]["nodes"][0]["servicePointNumber"] = (
        SERVICE_POINT
    )
    coordinator = _make_coordinator(hass, api)
    current = datetime(2026, 9, 24, 4, 18, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    def response(quarter_hour, *, date_from, date_to, **_kwargs):
        result = []
        day = max(date(2026, 9, 20), date_from)
        last_day = min(date_to, current.date() - timedelta(days=2))
        while day <= last_day:
            for hour, quantity in [(12, 1.0), (13, -0.5)]:
                result.extend(
                    {
                        "date": f"{day}T{hour}:{minute:02d}:00.000-04:00",
                        "fuelType": "ELECTRIC",
                        "quantity": quantity / (4 if quarter_hour else 1),
                    }
                    for minute in ([0, 15, 30, 45] if quarter_hour else [0])
                )
            day += timedelta(days=1)
        return result

    api.get_ami_energy_usages.side_effect = lambda **kw: response(False, **kw)
    api.get_ami_energy_usages_15min.side_effect = lambda **kw: response(True, **kw)
    prefix = f"national_grid_us:{MOCK_ACCOUNT_ID}_{SERVICE_POINT}"

    with patch("custom_components.national_grid_us.coordinator.datetime", Clock):
        for cycle in range(12):
            coordinator._is_midnight_refresh = cycle > 0
            coordinator.data = await coordinator._async_update_data()
            coordinator._is_midnight_refresh = False
            assert coordinator.data.is_midnight_refresh == (cycle > 0)

            if repair_old_overlap and cycle == 0:
                # Seed the old bug using synthetic overlapping hourly rows.
                cutoff = current.date() - timedelta(days=3)
                coordinator.data.ami_usages[SERVICE_POINT] += response(
                    False,
                    date_from=cutoff,
                    date_to=cutoff,
                )

            await async_import_all_statistics(hass, coordinator)
            rows = await _read_stats(hass)
            bad_day = "2026-09-21" if repair_old_overlap and cycle == 0 else None
            for suffix, expected in [
                ("electric_hourly_usage", 1.0),
                ("electric_return_hourly_usage", 0.5),
            ]:
                values = rows[f"{prefix}_{suffix}"]
                expected_days = [
                    (date(2026, 9, 20) + timedelta(days=i)).isoformat()
                    for i in range(
                        (current.date() - timedelta(days=2) - date(2026, 9, 20)).days
                        + 1
                    )
                ]
                actual_days = [
                    datetime.fromtimestamp(row["start"], UTC).date().isoformat()
                    for row in values
                ]
                assert actual_days == expected_days
                for row in values:
                    day = datetime.fromtimestamp(row["start"], UTC).date().isoformat()
                    multiplier = 2 if day == bad_day else 1
                    assert row["change"] == pytest.approx(expected * multiplier)
                assert values[-1]["sum"] == pytest.approx(
                    expected * (len(expected_days) + bool(bad_day)),
                )

            # Interval-only refreshes must not stack retained AMI records again.
            call_count = api.get_ami_energy_usages.call_count
            coordinator._interval_only_mode = True
            coordinator.data = await coordinator._async_update_data()
            coordinator._interval_only_mode = False
            await async_import_all_statistics(hass, coordinator)
            assert api.get_ami_energy_usages.call_count == call_count
            assert await _read_stats(hass) == rows
            current += timedelta(days=1)
