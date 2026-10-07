"""Tests for the National Grid entity base class."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from custom_components.national_grid_us.coordinator import MeterData
from custom_components.national_grid_us.entity import (
    NationalGridAccountEntity,
    NationalGridEntity,
)


@pytest.fixture(autouse=True)
def account_device_lookup():
    """Resolve the account device without a real device registry."""
    with patch(
        "custom_components.national_grid_us.entity.dr.async_get_device_id_by_identifier",
        return_value="account-device-id",
    ) as lookup:
        yield lookup


def _make_coordinator(meter_data: MeterData | None = None) -> MagicMock:
    """Create a mock coordinator."""
    coordinator = MagicMock()
    coordinator.get_meter_data.return_value = meter_data
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    return coordinator


def _make_meter_data() -> MeterData:
    return MeterData(
        account_id="acct1",
        meter={
            "servicePointNumber": "SP1",
            "meterNumber": "MTR001",
            "fuelType": "Electric",
            "hasAmiSmartMeter": True,
        },
        billing_account={"billingAccountId": "acct1"},
    )


def test_entity_device_info_with_meter_data() -> None:
    """Test device info is built from meter data."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info is not None
    assert ("national_grid_us", "acct1_SP1") in device_info["identifiers"]
    assert device_info["serial_number"] == "MTR001"
    assert device_info["name"] == "Electric Meter acct1-SP1"


def test_entity_device_info_without_meter_data() -> None:
    """Test device info fallback when meter data is None."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info is not None
    assert ("national_grid_us", "SP1") in device_info["identifiers"]
    assert device_info["serial_number"] == "SP1"
    assert device_info["name"] == "Meter SP1"


def test_entity_account_id() -> None:
    """Test account_id property."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.account_id == "acct1"


def test_entity_account_id_none() -> None:
    """Test account_id returns None when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.account_id is None


def test_entity_meter_property() -> None:
    """Test meter property."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.meter is not None
    assert entity.meter["meterNumber"] == "MTR001"


def test_entity_meter_none() -> None:
    """Test meter returns None when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.meter is None


def test_entity_billing_account() -> None:
    """Test billing_account property."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.billing_account is not None


def test_entity_billing_account_none() -> None:
    """Test billing_account returns None when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    assert entity.billing_account is None


def test_entity_device_info_smart_meter_not_ami() -> None:
    """Test device info model for a smart meter that is not AMI."""
    meter_data = MeterData(
        account_id="acct1",
        meter={
            "servicePointNumber": "SP1",
            "meterNumber": "MTR001",
            "fuelType": "Electric",
            "hasAmiSmartMeter": False,
            "isSmartMeter": True,
        },
        billing_account={"billingAccountId": "acct1"},
    )
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert "Smart Meter" in device_info["model"]
    assert "AMI" not in device_info["model"]


def test_entity_device_info_via_device(account_device_lookup) -> None:
    """Test that Meter device info links to the account device by id."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device_id") == "account-device-id"
    assert "via_device" not in device_info
    account_device_lookup.assert_called_once_with(
        coordinator.hass, ("national_grid_us", "acct1"), config_entry_id="test_entry"
    )


def test_entity_device_info_via_device_fallback() -> None:
    """Test that Meter device info has no via_device when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert "via_device_id" not in device_info


def test_account_entity_device_info() -> None:
    """Test account entity builds correct device info."""
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    entity = NationalGridAccountEntity(coordinator, "acct1")
    device_info = entity._attr_device_info
    assert device_info is not None
    assert ("national_grid_us", "acct1") in device_info["identifiers"]
    assert device_info["name"] == "National Grid acct1"
    assert device_info["manufacturer"] == "National Grid"


def test_entity_device_info_with_service_address() -> None:
    """Test device info extracts suggested_area from service address."""
    meter_data = MeterData(
        account_id="acct1",
        meter={
            "servicePointNumber": "SP1",
            "meterNumber": "MTR001",
            "fuelType": "Electric",
            "hasAmiSmartMeter": True,
        },
        billing_account={
            "billingAccountId": "acct1",
            "serviceAddress": {
                "serviceAddressCompressed": "123 Main St, Albany, NY 12345",
            },
        },
    )
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("suggested_area") == "123 Main St"
