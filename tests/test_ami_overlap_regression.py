"""Regression tests for inclusive AMI date ranges at different resolutions."""

from datetime import date, timedelta

import pytest
from homeassistant.core import HomeAssistant
from py_nationalgrid.exceptions import NationalGridError

from custom_components.national_grid_us.statistics import _build_hourly_stat_list

from .conftest import MOCK_SERVICE_POINT, _mock_billing_account
from .test_coordinator import _make_api, _make_coordinator


@pytest.mark.parametrize("first_refresh", [True, False])
@pytest.mark.parametrize("direction", [1, -1], ids=["import", "export"])
@pytest.mark.parametrize("bulk_fails", [False, True])
@pytest.mark.parametrize(
    "today",
    [date(2026, 10, 5), date(2026, 1, 3), date(2024, 3, 3), date(2026, 3, 10)],
    ids=["normal", "year-boundary", "leap-day", "dst-week"],
)
async def test_ami_boundary_energy_counted_once(
    hass: HomeAssistant,
    first_refresh: bool,
    direction: int,
    bulk_fails: bool,
    today: date,
) -> None:
    """Keep both boundary days once, including the historical fallback path."""
    api = _make_api()
    cutoff = today - timedelta(days=3)

    def response(quarter_hour: bool, *, date_from: date, date_to: date, **_kw):
        return [
            {
                "date": f"{day}T12:{minute:02d}:00.000-04:00",
                "fuelType": "ELECTRIC",
                "quantity": direction / (4 if quarter_hour else 1),
            }
            for day in [cutoff - timedelta(days=1), cutoff]
            if date_from <= day <= date_to
            for minute in ([0, 15, 30, 45] if quarter_hour else [0])
        ]

    api.get_ami_energy_usages.side_effect = (
        NationalGridError("bulk unavailable")
        if bulk_fails
        else lambda **kw: response(False, **kw)
    )
    api.get_ami_energy_usages_15min.side_effect = lambda **kw: response(True, **kw)
    coordinator = _make_coordinator(hass, api)
    meter = _mock_billing_account()["meter"]["nodes"][0]
    readings = {}

    await coordinator._fetch_ami_graphql_data(
        meter,
        "PREM001",
        MOCK_SERVICE_POINT,
        today,
        readings,
        is_first_refresh=first_refresh,
    )

    stats, total = _build_hourly_stat_list(
        readings[MOCK_SERVICE_POINT],
        0.0,
        0.0,
        consumption_only=direction > 0,
        return_only=direction < 0,
    )
    # Incremental bulk failure has no historical fallback; the recent pass
    # must still provide the boundary day once. All other paths cover both days.
    expected_days = 1 if bulk_fails and not first_refresh else 2
    assert len(stats) == expected_days
    assert total == pytest.approx(expected_days)
    assert all(row["state"] == pytest.approx(1.0) for row in stats)


@pytest.mark.parametrize("first_refresh", [True, False])
async def test_recent_ami_failure_preserves_successful_bulk(hass, first_refresh):
    """A failed recent request must not discard the successful history."""
    api = _make_api()
    bulk = [{"date": "2026-10-01T12:00:00-04:00", "quantity": 1.0}]
    api.get_ami_energy_usages.return_value = bulk
    api.get_ami_energy_usages_15min.side_effect = NationalGridError(
        "recent unavailable"
    )
    coordinator = _make_coordinator(hass, api)
    readings = {}
    await coordinator._fetch_ami_graphql_data(
        _mock_billing_account()["meter"]["nodes"][0],
        "PREM001",
        MOCK_SERVICE_POINT,
        date(2026, 10, 5),
        readings,
        is_first_refresh=first_refresh,
    )
    assert readings[MOCK_SERVICE_POINT] == bulk
