"""Tests for the National Grid entity base class."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.national_grid.coordinator import MeterData
from custom_components.national_grid.entity import (
    NationalGridAccountEntity,
    NationalGridEntity,
)


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
    assert ("national_grid", "SP1") in device_info["identifiers"]
    assert device_info["serial_number"] == "MTR001"
    assert device_info["name"] == "Electric Meter acct1-SP1"


def test_entity_device_info_without_meter_data() -> None:
    """Test device info fallback when meter data is None."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info is not None
    assert ("national_grid", "SP1") in device_info["identifiers"]
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


def test_entity_device_info_via_device() -> None:
    """Test that Meter device info includes via_device pointing to account."""
    meter_data = _make_meter_data()
    coordinator = _make_coordinator(meter_data)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device") == ("national_grid", "acct1")


def test_entity_device_info_via_device_fallback() -> None:
    """Test that Meter device info has no via_device when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert "via_device" not in device_info


def test_account_entity_device_info() -> None:
    """Test account entity builds correct device info."""
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    entity = NationalGridAccountEntity(coordinator, "acct1")
    device_info = entity._attr_device_info
    assert device_info is not None
    assert ("national_grid", "acct1") in device_info["identifiers"]
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
