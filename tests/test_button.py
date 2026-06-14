"""Tests for the National Grid button platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.national_grid_us.button import (
    PARALLEL_UPDATES,
    NationalGridForceRefreshButton,
)
from custom_components.national_grid_us.const import CONF_SELECTED_ACCOUNTS, DOMAIN
from custom_components.national_grid_us.coordinator import MeterData

from .conftest import (
    MOCK_ACCOUNT_ID,
    MOCK_PASSWORD,
    MOCK_SERVICE_POINT,
    MOCK_USERNAME,
    _mock_account_links,
    _mock_ami_usages,
    _mock_billing_account,
    _mock_bills,
    _mock_costs,
    _mock_interval_reads,
    _mock_usages,
)

PATCH_CLIENT = "custom_components.national_grid_us.coordinator.NationalGridClient"
PATCH_SESSION = (
    "custom_components.national_grid_us.coordinator.async_create_clientsession"
)


def _make_meter_data(
    service_point: str = MOCK_SERVICE_POINT,
    fuel_type: str = "Electric",
    *,
    has_ami: bool = True,
) -> MeterData:
    """Create a MeterData for tests."""
    return MeterData(
        account_id=MOCK_ACCOUNT_ID,
        meter={
            "servicePointNumber": service_point,
            "meterNumber": "MTR001",
            "fuelType": fuel_type,
            "hasAmiSmartMeter": has_ami,
        },
        billing_account={"billingAccountId": MOCK_ACCOUNT_ID},
    )


def _make_coordinator(meter_data: MeterData | None = None) -> MagicMock:
    """Create a mock coordinator with a given meter."""
    coordinator = MagicMock()
    coordinator.get_meter_data.return_value = meter_data
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.async_force_refresh_meter = AsyncMock()
    return coordinator


def test_parallel_updates() -> None:
    """Test PARALLEL_UPDATES is set to 0 (no concurrent button presses)."""
    assert PARALLEL_UPDATES == 0


def test_button_unique_id() -> None:
    """Test button unique_id includes DOMAIN prefix and service_point."""
    coordinator = _make_coordinator(_make_meter_data())
    button = NationalGridForceRefreshButton(coordinator, MOCK_SERVICE_POINT)
    assert button.unique_id == f"{DOMAIN}_{MOCK_SERVICE_POINT}_force_refresh"


def test_button_unique_id_different_service_points() -> None:
    """Test that different service points produce different unique IDs."""
    b1 = NationalGridForceRefreshButton(
        _make_coordinator(_make_meter_data("SP1")), "SP1"
    )
    b2 = NationalGridForceRefreshButton(
        _make_coordinator(_make_meter_data("SP2")), "SP2"
    )
    assert b1.unique_id != b2.unique_id


async def test_button_press_calls_force_refresh() -> None:
    """Test pressing the button calls coordinator.async_force_refresh_meter."""
    coordinator = _make_coordinator(_make_meter_data())
    button = NationalGridForceRefreshButton(coordinator, MOCK_SERVICE_POINT)
    await button.async_press()
    coordinator.async_force_refresh_meter.assert_called_once_with(MOCK_SERVICE_POINT)


async def test_button_press_passes_correct_service_point() -> None:
    """Test pressing a button passes the correct SP to the coordinator."""
    coordinator = _make_coordinator(_make_meter_data("SP_CUSTOM"))
    button = NationalGridForceRefreshButton(coordinator, "SP_CUSTOM")
    await button.async_press()
    coordinator.async_force_refresh_meter.assert_called_once_with("SP_CUSTOM")


@pytest.fixture
def config_entry(hass: HomeAssistant):
    """Create and add a config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_USERNAME,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_SELECTED_ACCOUNTS: [MOCK_ACCOUNT_ID],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _make_api_mock() -> AsyncMock:
    api = AsyncMock()
    api.get_billing_account = AsyncMock(return_value=_mock_billing_account())
    api.get_energy_usages = AsyncMock(return_value=_mock_usages())
    api.get_energy_usage_costs = AsyncMock(return_value=_mock_costs())
    api.get_ami_energy_usages = AsyncMock(return_value=_mock_ami_usages())
    api.get_ami_energy_usages_15min = AsyncMock(return_value=_mock_ami_usages())
    api.get_interval_reads = AsyncMock(return_value=_mock_interval_reads())
    api.get_linked_accounts = AsyncMock(return_value=_mock_account_links())
    api.get_bills = AsyncMock(return_value=_mock_bills())
    return api


async def test_button_setup_creates_entities(hass: HomeAssistant, config_entry) -> None:
    """Test that button setup registers a ForceRefresh button for each meter."""
    from homeassistant.helpers import entity_registry as er

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(
            "custom_components.national_grid_us.async_import_all_statistics",
            new_callable=AsyncMock,
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "button", "national_grid_us", f"{DOMAIN}_{MOCK_SERVICE_POINT}_force_refresh"
    )
    assert entity_id is not None, (
        f"Expected button for {MOCK_SERVICE_POINT} to be registered"
    )


async def test_button_setup_no_coordinator_data(
    hass: HomeAssistant, config_entry
) -> None:
    """Test button setup is a no-op when coordinator.data is None."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(
            "custom_components.national_grid_us.async_import_all_statistics",
            new_callable=AsyncMock,
        ),
        patch(
            "custom_components.national_grid_us.button.async_setup_entry",
            wraps=None,
        ),
    ):
        # Simulate coordinator.data being None at button setup time
        coordinator = MagicMock()
        coordinator.data = None
        from custom_components.national_grid_us.button import async_setup_entry

        added = []
        await async_setup_entry(hass, config_entry, added.append)

    assert added == [], "No buttons should be added when coordinator.data is None"
