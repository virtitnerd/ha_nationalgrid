"""Tests for National Grid diagnostics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.national_grid_us.const import CONF_SELECTED_ACCOUNTS, DOMAIN
from custom_components.national_grid_us.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import (
    MOCK_ACCOUNT_ID,
    MOCK_PASSWORD,
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
PATCH_STATISTICS = "custom_components.national_grid_us.async_import_all_statistics"


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


async def test_diagnostics_returns_expected_structure(
    hass: HomeAssistant, config_entry
) -> None:
    """Test diagnostics output contains expected top-level keys."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    assert "entry" in diag
    assert "coordinator" in diag
    assert "accounts" in diag
    assert "meters" in diag


async def test_diagnostics_redacts_credentials(
    hass: HomeAssistant, config_entry
) -> None:
    """Test that username and password are redacted in diagnostics output."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    entry_data = diag["entry"]
    assert entry_data.get("username") == "**REDACTED**"
    assert entry_data.get("password") == "**REDACTED**"


async def test_diagnostics_meter_fields(hass: HomeAssistant, config_entry) -> None:
    """Test diagnostics exposes expected meter fields without sensitive data."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    meters = diag["meters"]
    assert len(meters) > 0

    for meter_info in meters.values():
        assert "fuel_type" in meter_info
        assert "has_ami_smart_meter" in meter_info
        assert (
            "account_id" not in meter_info or meter_info["account_id"] == "**REDACTED**"
        )


async def test_diagnostics_account_fields(hass: HomeAssistant, config_entry) -> None:
    """Test diagnostics exposes account-level summary fields."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    accounts = diag["accounts"]
    assert isinstance(accounts, list)
    assert len(accounts) > 0
    account_info = accounts[0]
    assert "region" in account_info
    assert "meter_count" in account_info
    assert "next_reading_date" in account_info
    assert "current_bill_status" in account_info


async def test_diagnostics_no_data(hass: HomeAssistant, config_entry) -> None:
    """Test diagnostics returns error when coordinator has no data."""
    from unittest.mock import MagicMock

    mock_coordinator = MagicMock()
    mock_coordinator.data = None

    original_runtime_data = getattr(config_entry, "runtime_data", None)
    object.__setattr__(config_entry, "runtime_data", mock_coordinator)

    try:
        diag = await async_get_config_entry_diagnostics(hass, config_entry)
        assert diag == {"error": "No data loaded yet"}
    finally:
        if original_runtime_data is not None:
            object.__setattr__(config_entry, "runtime_data", original_runtime_data)
