"""Tests for the National Grid entity base class."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.national_grid_us import entity as entity_module
from custom_components.national_grid_us.const import DOMAIN
from custom_components.national_grid_us.coordinator import MeterData
from custom_components.national_grid_us.entity import (
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


def _register_account_device(hass: HomeAssistant, account_id: str) -> dr.DeviceEntry:
    """Pre-register the account device the way async_setup_entry does."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, account_id)},
        name=f"National Grid {account_id}",
    )


async def test_entity_device_info_via_device_id(hass: HomeAssistant) -> None:
    """Test that Meter device info links to the registered account device by id."""
    account_device = _register_account_device(hass, "acct1")
    coordinator = _make_coordinator(_make_meter_data())
    coordinator.hass = hass
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device_id") == account_device.id
    assert "via_device" not in device_info


async def test_entity_device_info_via_device_when_account_unregistered(
    hass: HomeAssistant,
) -> None:
    """Test the identifier-tuple fallback when the account device is missing."""
    coordinator = _make_coordinator(_make_meter_data())
    coordinator.hass = hass
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device") == ("national_grid_us", "acct1")
    assert "via_device_id" not in device_info


def test_entity_device_info_via_device_without_hass() -> None:
    """Test the identifier-tuple fallback when the coordinator has no hass."""
    coordinator = _make_coordinator(_make_meter_data())
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device") == ("national_grid_us", "acct1")
    assert "via_device_id" not in device_info


async def test_entity_device_info_via_device_on_older_core(
    hass: HomeAssistant, monkeypatch
) -> None:
    """Test the identifier tuple is used on cores without via_device_id."""
    _register_account_device(hass, "acct1")
    monkeypatch.setattr(entity_module, "_core_supports_via_device_id", lambda: False)
    coordinator = _make_coordinator(_make_meter_data())
    coordinator.hass = hass
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert device_info.get("via_device") == ("national_grid_us", "acct1")
    assert "via_device_id" not in device_info


def test_entity_device_info_via_device_fallback() -> None:
    """Test that Meter device info has no account link when no meter data."""
    coordinator = _make_coordinator(None)
    entity = NationalGridEntity(coordinator, "SP1")
    device_info = entity._attr_device_info
    assert "via_device" not in device_info
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
