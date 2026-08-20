"""Tests for the National Grid sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.sensor import SensorDeviceClass

from custom_components.national_grid_us.const import UNIT_CCF, UNIT_KWH
from custom_components.national_grid_us.coordinator import MeterData
from custom_components.national_grid_us.sensor import (
    ACCOUNT_SENSOR_DESCRIPTIONS,
    PARALLEL_UPDATES,
    SENSOR_DESCRIPTIONS,
    NationalGridAccountSensor,
    NationalGridSensor,
    _get_avg_daily_usage,
    _get_bill_history_record,
    _get_cost_per_unit,
    _get_cost_per_unit_unit,
    _get_current_bill_amount,
    _get_current_bill_attributes,
    _get_energy_device_class,
    _get_energy_unit,
    _get_energy_usage,
    _get_next_reading_date,
    _get_supplier_charges,
    _get_total_charges,
    _get_utility_charges,
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
    """Test energy_cost sensor uses totalCharges from bill history."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    assert _get_total_charges(coordinator, meter_data) == 145.50


def test_energy_cost_none() -> None:
    """Test energy_cost sensor returns None when no bill history record."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_electric_bill_record.return_value = None
    assert _get_total_charges(coordinator, meter_data) is None


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
    assert sensor.unique_id == "national_grid_us_acct1_next_reading_date"


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


def _make_meter_data_with_fuel(fuel_type: str) -> MeterData:
    return MeterData(
        account_id="acct1",
        meter={
            "fuelType": fuel_type,
            "servicePointNumber": "SP1",
            "hasAmiSmartMeter": True,
        },
        billing_account={"billingAccountId": "acct1"},
    )


def test_cost_per_unit_electric() -> None:
    """Test electric meter blends last 3 bill history records."""
    meter_data = _make_meter_data_with_fuel("Electric")
    coordinator = MagicMock()
    # Records returned newest-first from the business portal.
    coordinator.get_all_electric_bill_records.return_value = [
        {"totalCharges": 80.0, "totalKwh": 400.0},
        {"totalCharges": 100.0, "totalKwh": 500.0},
        {"totalCharges": 120.0, "totalKwh": 600.0},
        {"totalCharges": 140.0, "totalKwh": 700.0},
    ]
    # Window = 3 most recent: 80+100+120=300, 400+500+600=1500
    result = _get_cost_per_unit(coordinator, meter_data)
    assert result == round(300.0 / 1500.0, 4)


def test_cost_per_unit_gas() -> None:
    """Test gas meter uses totalTherms from bill history."""
    meter_data = _make_meter_data_with_fuel("Gas")
    coordinator = MagicMock()
    coordinator.get_all_gas_bill_records.return_value = [
        {"totalCharges": 75.0, "totalTherms": 50.0},
    ]
    result = _get_cost_per_unit(coordinator, meter_data)
    assert result == round(75.0 / 50.0, 4)


def test_cost_per_unit_skips_zero_kwh() -> None:
    """Test that periods with zero kWh (e.g. full net-metering months) are skipped."""
    meter_data = _make_meter_data_with_fuel("Electric")
    coordinator = MagicMock()
    coordinator.get_all_electric_bill_records.return_value = [
        {"totalCharges": 43.63, "totalKwh": 0.0},  # full net-metering month, skipped
        {"totalCharges": 80.0, "totalKwh": 400.0},
        {"totalCharges": 100.0, "totalKwh": 500.0},
    ]
    # Zero-kWh record skipped; window = remaining 2 records
    result = _get_cost_per_unit(coordinator, meter_data)
    assert result == round(180.0 / 900.0, 4)


def test_cost_per_unit_no_data_returns_zero() -> None:
    """Test cost per unit returns 0.0 when no bill history is available."""
    meter_data = _make_meter_data_with_fuel("Electric")
    coordinator = MagicMock()
    coordinator.get_all_electric_bill_records.return_value = []
    assert _get_cost_per_unit(coordinator, meter_data) == 0.0


def test_cost_per_unit_unknown_fuel_returns_zero() -> None:
    """Test cost per unit returns 0.0 for unknown fuel types."""
    meter_data = _make_meter_data_with_fuel("Steam")
    coordinator = MagicMock()
    assert _get_cost_per_unit(coordinator, meter_data) == 0.0


def test_cost_per_unit_unit_electric() -> None:
    """Test cost per unit label for electric meter uses ISO currency format."""
    assert _get_cost_per_unit_unit(_make_meter_data_with_fuel("Electric")) == "USD/kWh"


def test_cost_per_unit_unit_gas() -> None:
    """Test cost per unit label for gas meter uses ISO currency format."""
    assert _get_cost_per_unit_unit(_make_meter_data_with_fuel("Gas")) == "USD/CCF"


def test_cost_per_unit_in_sensor_descriptions() -> None:
    """Test that cost_per_unit appears in SENSOR_DESCRIPTIONS."""
    keys = [d.key for d in SENSOR_DESCRIPTIONS]
    assert "cost_per_unit" in keys


# ---------------------------------------------------------------------------
# Bill history sensor tests
# ---------------------------------------------------------------------------


def _make_electric_bill_record() -> dict:
    return {
        "readDate": "2025-01-28",
        "readFromDate": "2024-12-28",
        "totalKwh": 520.0,
        "utilityCharges": 98.40,
        "supplierCharges": 47.10,
        "totalCharges": 145.50,
        "avgDailyUsage": 16.77,
    }


def _make_gas_bill_record() -> dict:
    return {
        "readDate": "2025-01-28",
        "readFromDate": "2024-12-28",
        "totalTherms": 32.0,
        "utilityCharges": 28.80,
        "supplierCharges": 16.20,
        "totalCharges": 45.00,
        "avgDailyUsage": 1.03,
    }


def test_get_bill_history_record_electric() -> None:
    """Test _get_bill_history_record returns electric record for electric meter."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    result = _get_bill_history_record(coordinator, meter_data)
    assert result is not None
    assert result["utilityCharges"] == 98.40


def test_get_bill_history_record_gas() -> None:
    """Test _get_bill_history_record returns gas record for gas meter."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = _make_gas_bill_record()
    result = _get_bill_history_record(coordinator, meter_data)
    assert result is not None
    assert result["utilityCharges"] == 28.80


def test_get_bill_history_record_unknown_fuel() -> None:
    """Test _get_bill_history_record returns None for unknown fuel type."""
    meter_data = _make_meter_data("Solar")
    coordinator = MagicMock()
    assert _get_bill_history_record(coordinator, meter_data) is None


def test_get_utility_charges_electric() -> None:
    """Test _get_utility_charges returns utilityCharges from electric record."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    assert _get_utility_charges(coordinator, meter_data) == 98.40


def test_get_utility_charges_gas() -> None:
    """Test _get_utility_charges returns utilityCharges from gas record."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = _make_gas_bill_record()
    assert _get_utility_charges(coordinator, meter_data) == 28.80


def test_get_utility_charges_none_when_no_record() -> None:
    """Test _get_utility_charges returns None when no bill history record available."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_electric_bill_record.return_value = None
    assert _get_utility_charges(coordinator, meter_data) is None


def test_get_supplier_charges_electric() -> None:
    """Test _get_supplier_charges returns supplierCharges from electric record."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    assert _get_supplier_charges(coordinator, meter_data) == 47.10


def test_get_supplier_charges_none_when_no_record() -> None:
    """Test _get_supplier_charges returns None when no bill history record available."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_electric_bill_record.return_value = None
    assert _get_supplier_charges(coordinator, meter_data) is None


def test_get_supplier_charges_gas() -> None:
    """Test _get_supplier_charges returns supplierCharges from gas record."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = _make_gas_bill_record()
    assert _get_supplier_charges(coordinator, meter_data) == 16.20


def test_get_supplier_charges_gas_none() -> None:
    """Test _get_supplier_charges returns None when no gas bill history record."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = None
    assert _get_supplier_charges(coordinator, meter_data) is None


def test_get_avg_daily_usage_electric() -> None:
    """Test _get_avg_daily_usage returns avgDailyUsage from electric record."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    assert _get_avg_daily_usage(coordinator, meter_data) == 16.77


def test_get_avg_daily_usage_gas() -> None:
    """Test _get_avg_daily_usage returns avgDailyUsage from gas record."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = _make_gas_bill_record()
    assert _get_avg_daily_usage(coordinator, meter_data) == 1.03


def test_get_avg_daily_usage_none_when_no_record() -> None:
    """Test _get_avg_daily_usage returns None when no bill history record available."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_electric_bill_record.return_value = None
    assert _get_avg_daily_usage(coordinator, meter_data) is None


def test_avg_daily_usage_uses_energy_unit_fn() -> None:
    """Test last_bill_avg_daily_usage sensor uses _get_energy_unit for its unit_fn."""
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "last_bill_avg_daily_usage")
    assert desc.unit_fn is not None
    assert desc.unit_fn(_make_meter_data("Electric")) == UNIT_KWH
    assert desc.unit_fn(_make_meter_data("Gas")) == UNIT_CCF


def test_get_total_charges_electric() -> None:
    """Test _get_total_charges returns totalCharges from electric record."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    electric_rec = _make_electric_bill_record()
    coordinator.get_latest_electric_bill_record.return_value = electric_rec
    assert _get_total_charges(coordinator, meter_data) == 145.50


def test_get_total_charges_gas() -> None:
    """Test _get_total_charges returns totalCharges from gas record."""
    meter_data = _make_meter_data("Gas")
    coordinator = MagicMock()
    coordinator.get_latest_gas_bill_record.return_value = _make_gas_bill_record()
    assert _get_total_charges(coordinator, meter_data) == 45.00


def test_get_total_charges_none_when_no_record() -> None:
    """Test _get_total_charges returns None when no bill history record available."""
    meter_data = _make_meter_data("Electric")
    coordinator = MagicMock()
    coordinator.get_latest_electric_bill_record.return_value = None
    assert _get_total_charges(coordinator, meter_data) is None


def test_bill_history_sensors_in_descriptions() -> None:
    """Test all bill history sensors appear in SENSOR_DESCRIPTIONS."""
    keys = [d.key for d in SENSOR_DESCRIPTIONS]
    assert "last_bill_utility_charges" in keys
    assert "last_bill_supplier_charges" in keys
    assert "last_bill_avg_daily_usage" in keys
