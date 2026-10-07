"""NationalGridEntity class."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import NationalGridDataUpdateCoordinator

if TYPE_CHECKING:  # pragma: no cover
    from py_nationalgrid.models import BillingAccount, Meter


def _core_supports_via_device_id() -> bool:
    """Return True when this core takes ``via_device_id`` in ``DeviceInfo``.

    Home Assistant 2026.8 added ``via_device_id`` (the registry id of the parent
    device) and deprecated the ``via_device`` identifier tuple, which core removes
    in 2027.8. Older cores reject unknown ``DeviceInfo`` keys, so the key is only
    used where the running core declares it.
    """
    return "via_device_id" in DeviceInfo.__optional_keys__


class NationalGridEntity(CoordinatorEntity[NationalGridDataUpdateCoordinator]):
    """Base entity class for National Grid integration."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NationalGridDataUpdateCoordinator,
        service_point_number: str,
    ) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._service_point_number = service_point_number
        self._attr_device_info = self._build_device_info()

    def _build_device_info(self) -> DeviceInfo:
        """Build device info for this meter."""
        meter_data = self.coordinator.get_meter_data(self._service_point_number)

        if meter_data is None:
            return DeviceInfo(
                identifiers={(DOMAIN, self._service_point_number)},
                serial_number=self._service_point_number,
                name=f"Meter {self._service_point_number}",
                manufacturer="National Grid",
            )

        meter: Meter = meter_data.meter
        billing_account: BillingAccount = meter_data.billing_account

        meter_number = str(meter.get("meterNumber", "")) or self._service_point_number
        fuel_type = str(meter.get("fuelType", ""))
        has_ami = bool(meter.get("hasAmiSmartMeter", False))
        is_smart = bool(meter.get("isSmartMeter", False))

        # Build device name — account_id + service_point ensures uniqueness across
        # multiple accounts (where SP numbers like 100/200 repeat).
        account_id = meter_data.account_id
        name = (
            f"{fuel_type.title()} Meter {account_id}-{self._service_point_number}"
            if fuel_type
            else f"Meter {account_id}-{self._service_point_number}"
        )

        # Determine model based on meter capabilities
        if has_ami:
            model = "AMI Smart Meter"
        elif is_smart:
            model = "Smart Meter"
        else:
            model = "Standard Meter"

        # Add fuel type to model if available
        if fuel_type:
            model = f"{fuel_type.title()} {model}"

        # Extract address and account info from billing account
        service_address = ""
        if billing_account:
            addr_info = billing_account.get("serviceAddress", {})
            service_address = str(addr_info.get("serviceAddressCompressed", ""))

        # Build configuration URL with account info if available
        config_url = "https://myaccount.nationalgrid.com"

        # Extract suggested area from service address (first part before comma)
        suggested_area: str | None = None
        if service_address:
            parts = service_address.split(",")
            if len(parts) >= 2:  # noqa: PLR2004
                suggested_area = parts[0].strip().title()

        device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{account_id}_{self._service_point_number}")},
            serial_number=meter_number,
            name=name,
            manufacturer="National Grid",
            model=model,
            configuration_url=config_url,
            suggested_area=suggested_area,
        )
        self._link_account_device(device_info, account_id)
        return device_info

    def _link_account_device(self, device_info: DeviceInfo, account_id: str) -> None:
        """Link the meter device to its account device.

        The account device is pre-registered in ``async_setup_entry`` before the
        platforms load, so its registry id is normally available here. The
        identifier tuple remains the fallback for cores older than 2026.8 and for
        the case where the account device is not registered yet.
        """
        if _core_supports_via_device_id():
            hass = getattr(self.coordinator, "hass", None)
            if isinstance(hass, HomeAssistant):
                account_device = dr.async_get(hass).async_get_device(
                    identifiers={(DOMAIN, account_id)}
                )
                if account_device is not None:
                    device_info["via_device_id"] = account_device.id
                    return
        device_info["via_device"] = (DOMAIN, account_id)  # type: ignore[typeddict-unknown-key]

    @property
    def account_id(self) -> str | None:
        """Return the account ID for this meter."""
        meter_data = self.coordinator.get_meter_data(self._service_point_number)
        return meter_data.account_id if meter_data else None

    @property
    def meter(self) -> Meter | None:
        """Return the meter data."""
        meter_data = self.coordinator.get_meter_data(self._service_point_number)
        return meter_data.meter if meter_data else None

    @property
    def billing_account(self) -> BillingAccount | None:
        """Return the billing account data."""
        meter_data = self.coordinator.get_meter_data(self._service_point_number)
        return meter_data.billing_account if meter_data else None


class NationalGridAccountEntity(CoordinatorEntity[NationalGridDataUpdateCoordinator]):
    """Base entity class for account-level National Grid entities."""

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NationalGridDataUpdateCoordinator,
        account_id: str,
    ) -> None:
        """Initialize the account entity."""
        super().__init__(coordinator)
        self._account_id = account_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, account_id)},
            name=f"National Grid {account_id}",
            manufacturer="National Grid",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://myaccount.nationalgrid.com",
        )
