"""Tests for the National Grid sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.national_grid.const import UNIT_CCF, UNIT_KWH
from custom_components.national_grid.coordinator import MeterData
from custom_components.national_grid.sensor import (
    ACCOUNT_SENSOR_DESCRIPTIONS,
    PARALLEL_UPDATES,
    SENSOR_DESCRIPTIONS,
    NationalGridAccountSensor,
    NationalGridSensor,
    _get_current_bill_amount,
    _get_current_bill_attributes,
    _get_energy_cost,
    _get_energy_device_class,
    _get_energy_unit,
    _get_energy_usage,
    _get_next_reading_date,
)


def _make_meter_data(
    fuel_type: str = "Electric", account_id: str = "acct1"
) -> MeterData:
    """Create a MeterData with a given fuel type."""
    return MeterData(
        account_id=account_id,
        meter={
            "fuelType": fuel_type,
            "servicePointNumber": "SP1",
            "hasAmiSmartMeter": True,
        },
        billing_account={"billingAccountId": account_id},
    )


def test_parallel_updates() -> None:
    """Test PARALLEL_UPDATES is set to 1."""
    assert PARALLEL_UPDATES == 1


def test_energy_usage_electric() -> None:
    """Test usage value for electric meter."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_usage.return_value = {
        "usage": 500.0,
        "usageType": "TOTAL_KWH",
    }
    result = _get_energy_usage(coordinator, meter_data)
    assert result == 500.0


def test_energy_usage_gas_returns_value_directly() -> None:
    """Test usage value for gas meter returns API value directly (already CCF)."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_usage.return_value = {"usage": 10.0, "usageType": "CCF"}
    result = _get_energy_usage(coordinator, meter_data)

    assert result == 10.0


def test_energy_usage_none() -> None:
    """Test usage returns None when no data."""
    meter_data = _make_meter_data()
    coordinator = MagicMock()
    coordinator.get_latest_usage.return_value = None
    result = _get_energy_usage(coordinator, meter_data)
    assert result is None


def test_energy_cost() -> None:
    """Test cost value extraction."""
    meter_data = _make_meter_data()
    coordinator = MagicMock()
    coordinator.get_latest_cost.return_value = {"amount": 120.50}
    result = _get_energy_cost(coordinator, meter_data)
    assert result == 120.50


def test_energy_cost_none() -> None:
    """Test cost returns None when no data."""
    meter_data = _make_meter_data()
    coordinator = MagicMock()
    coordinator.get_latest_cost.return_value = None
    result = _get_energy_cost(coordinator, meter_data)
    assert result is None


def test_gas_meter_units() -> None:
    """Test gas meter returns CCF unit."""
    meter_data = _make_meter_data("Gas")
    assert _get_energy_unit(meter_data) == UNIT_CCF


def test_electric_meter_units() -> None:
    """Test electric meter returns kWh unit."""
    meter_data = _make_meter_data("Electric")
    assert _get_energy_unit(meter_data) == UNIT_KWH


def test_gas_device_class() -> None:
    """Test gas meter returns GAS device class."""
    meter_data = _make_meter_data("Gas")
    assert _get_energy_device_class(meter_data) == SensorDeviceClass.GAS


def test_electric_device_class() -> None:
    """Test electric meter returns ENERGY device class."""
    meter_data = _make_meter_data("Electric")
    assert _get_energy_device_class(meter_data) == SensorDeviceClass.ENERGY


def test_sensor_native_value_none_when_no_meter_data() -> None:
    """Test native_value returns None when coordinator has no meter data for this SP."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_meter_data.return_value = meter_data

    sensor = NationalGridSensor(coordinator, "SP1", SENSOR_DESCRIPTIONS[0], meter_data)

    # Simulate meter data becoming unavailable after entity creation
    coordinator.get_meter_data.return_value = None

    assert sensor.native_value is None


def _make_mock_bill() -> dict:
    return {
        "accountNumber": "acct1",
        "statementDate": "2025-01-01",
        "dueDate": "2025-01-22",
        "status": "UNPAID",
        "currentChargesAmount": 145.50,
        "totalDueAmount": 145.50,
    }


def test_current_bill_amount_returns_current_charges() -> None:
    """Test _get_current_bill_amount returns currentChargesAmount."""
    coordinator = MagicMock()
    coordinator.get_current_bill.return_value = _make_mock_bill()
    assert _get_current_bill_amount(coordinator, "acct1") == 145.50


def test_current_bill_amount_none_when_no_bill() -> None:
    """Test _get_current_bill_amount returns None when no bill available."""
    coordinator = MagicMock()
    coordinator.get_current_bill.return_value = None
    assert _get_current_bill_amount(coordinator, "acct1") is None


def test_current_bill_attributes_returns_due_date_and_status() -> None:
    """Test _get_current_bill_attributes returns expected dict."""
    coordinator = MagicMock()
    coordinator.get_current_bill.return_value = _make_mock_bill()
    attrs = _get_current_bill_attributes(coordinator, "acct1")
    assert attrs["due_date"] == "2025-01-22"
    assert attrs["statement_date"] == "2025-01-01"
    assert attrs["status"] == "UNPAID"
    assert attrs["total_due"] == 145.50


def test_current_bill_attributes_empty_when_no_bill() -> None:
    """Test _get_current_bill_attributes returns empty dict when no bill."""
    coordinator = MagicMock()
    coordinator.get_current_bill.return_value = None
    assert _get_current_bill_attributes(coordinator, "acct1") == {}


def test_account_sensor_extra_state_attributes() -> None:
    """Test NationalGridAccountSensor.extra_state_attributes calls attributes_fn."""
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.get_current_bill.return_value = _make_mock_bill()

    # current_bill_amount is the first description (index 0)
    bill_description = ACCOUNT_SENSOR_DESCRIPTIONS[0]
    sensor = NationalGridAccountSensor(coordinator, "acct1", bill_description)
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["due_date"] == "2025-01-22"


def test_account_sensor_extra_state_attributes_none_when_no_fn() -> None:
    """Test extra_state_attributes returns None when no attributes_fn set."""
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.get_next_reading_date.return_value = None

    # next_reading_date is the second description (index 1) — no attributes_fn
    date_description = ACCOUNT_SENSOR_DESCRIPTIONS[1]
    sensor = NationalGridAccountSensor(coordinator, "acct1", date_description)
    assert sensor.extra_state_attributes is None


def test_next_reading_date_returns_date() -> None:
    """Test _get_next_reading_date parses ISO date string correctly."""
    from datetime import date

    coordinator = MagicMock()
    coordinator.get_next_reading_date.return_value = "2025-06-15"
    result = _get_next_reading_date(coordinator, "acct1")
    assert result == date(2025, 6, 15)


def test_next_reading_date_none_when_missing() -> None:
    """Test _get_next_reading_date returns None when no date is available."""
    coordinator = MagicMock()
    coordinator.get_next_reading_date.return_value = None
    assert _get_next_reading_date(coordinator, "acct1") is None


def test_next_reading_date_none_on_invalid_format() -> None:
    """Test _get_next_reading_date returns None for unparseable strings."""
    coordinator = MagicMock()
    coordinator.get_next_reading_date.return_value = "not-a-date"
    assert _get_next_reading_date(coordinator, "acct1") is None


def test_account_sensor_unique_id() -> None:
    """Test NationalGridAccountSensor unique_id is derived from account_id + key."""
    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    # next_reading_date is index 1; current_bill_amount is index 0
    next_reading_desc = next(
        d for d in ACCOUNT_SENSOR_DESCRIPTIONS if d.key == "next_reading_date"
    )
    sensor = NationalGridAccountSensor(coordinator, "acct1", next_reading_desc)
    assert sensor.unique_id == "national_grid_acct1_next_reading_date"


def test_account_sensor_native_value() -> None:
    """Test NationalGridAccountSensor.native_value calls value_fn with account_id."""
    from datetime import date

    coordinator = MagicMock()
    coordinator.config_entry = MagicMock()
    coordinator.config_entry.entry_id = "test_entry"
    coordinator.get_next_reading_date.return_value = "2025-09-01"

    next_reading_desc = next(
        d for d in ACCOUNT_SENSOR_DESCRIPTIONS if d.key == "next_reading_date"
    )
    sensor = NationalGridAccountSensor(coordinator, "acct1", next_reading_desc)
    assert sensor.native_value == date(2025, 9, 1)
