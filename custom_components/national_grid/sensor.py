"""Sensor platform for national_grid."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory

from .const import _LOGGER, DOMAIN, UNIT_CCF, UNIT_KWH
from .entity import NationalGridAccountEntity, NationalGridEntity

PARALLEL_UPDATES = 1

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import MeterData, NationalGridDataUpdateCoordinator
    from .data import NationalGridConfigEntry


@dataclass(frozen=True, kw_only=True)
class NationalGridAccountSensorEntityDescription(SensorEntityDescription):
    """Describe a National Grid account-level sensor."""

    value_fn: Callable[[NationalGridDataUpdateCoordinator, str], Any]
    attributes_fn: (
        Callable[[NationalGridDataUpdateCoordinator, str], dict[str, Any]] | None
    ) = None


@dataclass(frozen=True, kw_only=True)
class NationalGridSensorEntityDescription(SensorEntityDescription):
    """Describe National Grid sensor entity."""

    value_fn: Callable[[NationalGridDataUpdateCoordinator, MeterData], Any]
    unit_fn: Callable[[MeterData], str | None] | None = None
    device_class_fn: Callable[[MeterData], SensorDeviceClass | None] | None = None
    available_fn: Callable[[MeterData], bool] = lambda _: True


def _get_energy_usage(
    coordinator: NationalGridDataUpdateCoordinator, meter_data: MeterData
) -> float | None:
    """Get the latest energy usage for a meter."""
    fuel_type = meter_data.meter.get("fuelType")
    usage = coordinator.get_latest_usage(meter_data.account_id, fuel_type)
    _LOGGER.debug(
        "Getting usage for account=%s, fuel_type=%s: %s",
        meter_data.account_id,
        fuel_type,
        usage,
    )
    if usage:
        return usage.get("usage")
    return None


def _get_energy_cost(
    coordinator: NationalGridDataUpdateCoordinator, meter_data: MeterData
) -> float | None:
    """Get the latest energy cost for a meter."""
    fuel_type = meter_data.meter.get("fuelType")
    cost = coordinator.get_latest_cost(meter_data.account_id, fuel_type)
    if cost:
        return cost.get("amount")
    return None


def _get_energy_unit(meter_data: MeterData) -> str:
    """Get the appropriate energy unit based on fuel type."""
    fuel_type = meter_data.meter.get("fuelType", "").upper()
    if fuel_type == "GAS":
        return UNIT_CCF
    return UNIT_KWH


def _get_energy_device_class(meter_data: MeterData) -> SensorDeviceClass | None:
    """Get the device class based on fuel type."""
    fuel_type = meter_data.meter.get("fuelType", "").upper()
    if fuel_type == "GAS":
        return SensorDeviceClass.GAS
    return SensorDeviceClass.ENERGY


_RATE_WINDOW = 3  # billing cycles to include in blended rate


def _get_cost_per_unit_unit(meter_data: MeterData) -> str:
    """Return the appropriate cost-per-unit label based on fuel type."""
    fuel_type = meter_data.meter.get("fuelType", "").upper()
    return "USD/CCF" if fuel_type == "GAS" else "USD/kWh"


def _get_cost_per_unit(
    coordinator: NationalGridDataUpdateCoordinator, meter_data: MeterData
) -> float:
    """Return blended rate (total cost ÷ total usage) over the last 3 matched months.

    Returns 0.0 when no matched month pairs are available so Energy Dashboard
    cost calculations remain functional.
    """
    fuel_type = meter_data.meter.get("fuelType")
    usages = coordinator.get_all_usages(meter_data.account_id, fuel_type)
    costs = coordinator.get_all_costs(meter_data.account_id, fuel_type)
    if not usages or not costs:
        return 0.0

    # Build YYYYMM → cost amount lookup from the date field (month is 1-12 only).
    cost_by_month: dict[int, float] = {}
    for cost in costs:
        date_str = cost.get("date", "")
        if len(date_str) >= 7:  # noqa: PLR2004
            yyyymm = int(date_str[:7].replace("-", ""))
            cost_by_month[yyyymm] = cost.get("amount", 0.0)

    # Collect months that have both a positive usage record and a cost record.
    matched = sorted(
        [
            (u.get("usageYearMonth", 0), u.get("usage", 0.0))
            for u in usages
            if u.get("usageYearMonth", 0) in cost_by_month and u.get("usage", 0.0) > 0
        ],
        key=lambda x: x[0],
        reverse=True,
    )
    if not matched:
        return 0.0

    window = matched[:_RATE_WINDOW]
    total_cost = sum(cost_by_month[yyyymm] for yyyymm, _ in window)
    total_usage = sum(usage for _, usage in window)
    if total_usage == 0:  # pragma: no cover
        return 0.0
    return round(total_cost / total_usage, 4)


def _get_current_bill_amount(
    coordinator: NationalGridDataUpdateCoordinator, account_id: str
) -> float | None:
    """Return current billing period charges."""
    bill = coordinator.get_current_bill(account_id)
    return bill.get("currentChargesAmount") if bill else None


def _get_current_bill_attributes(
    coordinator: NationalGridDataUpdateCoordinator, account_id: str
) -> dict[str, Any]:
    """Return due date and status as extra attributes."""
    bill = coordinator.get_current_bill(account_id)
    if not bill:
        return {}
    return {
        "due_date": bill.get("dueDate"),
        "statement_date": bill.get("statementDate"),
        "status": bill.get("status"),
        "total_due": bill.get("totalDueAmount"),
    }


def _get_next_reading_date(
    coordinator: NationalGridDataUpdateCoordinator, account_id: str
) -> date | None:
    """Return the next scheduled meter reading date."""
    raw = coordinator.get_next_reading_date(account_id)
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).date()
    except ValueError:
        return None


ACCOUNT_SENSOR_DESCRIPTIONS: tuple[NationalGridAccountSensorEntityDescription, ...] = (
    NationalGridAccountSensorEntityDescription(
        key="current_bill_amount",
        translation_key="current_bill_amount",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        state_class=None,
        value_fn=_get_current_bill_amount,
        attributes_fn=_get_current_bill_attributes,
    ),
    NationalGridAccountSensorEntityDescription(
        key="next_reading_date",
        translation_key="next_reading_date",
        device_class=SensorDeviceClass.DATE,
        state_class=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_get_next_reading_date,
    ),
)

SENSOR_DESCRIPTIONS: tuple[NationalGridSensorEntityDescription, ...] = (
    NationalGridSensorEntityDescription(
        key="energy_cost",
        translation_key="energy_cost",
        native_unit_of_measurement="USD",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_get_energy_cost,
    ),
    NationalGridSensorEntityDescription(
        key="energy_usage",
        translation_key="energy_usage",
        value_fn=_get_energy_usage,
        unit_fn=_get_energy_unit,
        device_class_fn=_get_energy_device_class,
    ),
    NationalGridSensorEntityDescription(
        key="cost_per_unit",
        translation_key="cost_per_unit",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=4,
        value_fn=_get_cost_per_unit,
        unit_fn=_get_cost_per_unit_unit,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: NationalGridConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = entry.runtime_data

    entities: list[SensorEntity] = []

    if coordinator.data:
        # Create sensors for each meter.
        for service_point_number, meter_data in coordinator.data.meters.items():
            entities.extend(
                NationalGridSensor(
                    coordinator=coordinator,
                    service_point_number=service_point_number,
                    entity_description=description,
                    meter_data=meter_data,
                )
                for description in SENSOR_DESCRIPTIONS
                if description.available_fn(meter_data)
            )

        # Create account-level sensors for each unique account.
        for account_id in coordinator.data.accounts:
            entities.extend(
                NationalGridAccountSensor(
                    coordinator=coordinator,
                    account_id=account_id,
                    entity_description=description,
                )
                for description in ACCOUNT_SENSOR_DESCRIPTIONS
            )

    async_add_entities(entities)


class NationalGridSensor(NationalGridEntity, SensorEntity):
    """National Grid sensor entity."""

    entity_description: NationalGridSensorEntityDescription

    def __init__(
        self,
        coordinator: NationalGridDataUpdateCoordinator,
        service_point_number: str,
        entity_description: NationalGridSensorEntityDescription,
        meter_data: MeterData,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, service_point_number)
        self.entity_description = entity_description
        self._attr_unique_id = (
            f"{DOMAIN}_{service_point_number}_{entity_description.key}"
        )
        # Set dynamic unit based on meter type.
        if entity_description.unit_fn:
            self._attr_native_unit_of_measurement = entity_description.unit_fn(
                meter_data
            )
        # Set dynamic device class based on meter type.
        if entity_description.device_class_fn:
            self._attr_device_class = entity_description.device_class_fn(meter_data)

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        meter_data = self.coordinator.get_meter_data(self._service_point_number)
        if meter_data is None:
            return None
        return self.entity_description.value_fn(self.coordinator, meter_data)


class NationalGridAccountSensor(NationalGridAccountEntity, SensorEntity):
    """National Grid account-level sensor entity."""

    entity_description: NationalGridAccountSensorEntityDescription

    def __init__(
        self,
        coordinator: NationalGridDataUpdateCoordinator,
        account_id: str,
        entity_description: NationalGridAccountSensorEntityDescription,
    ) -> None:
        """Initialize the account sensor."""
        super().__init__(coordinator, account_id)
        self.entity_description = entity_description
        self._attr_unique_id = f"{DOMAIN}_{account_id}_{entity_description.key}"

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator, self._account_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes if an attributes_fn is defined."""
        if self.entity_description.attributes_fn is None:
            return None
        return self.entity_description.attributes_fn(self.coordinator, self._account_id)
