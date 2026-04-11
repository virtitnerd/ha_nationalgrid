"""Button platform for National Grid."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .entity import NationalGridEntity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import NationalGridDataUpdateCoordinator
    from .data import NationalGridConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: NationalGridConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up National Grid button entities."""
    coordinator: NationalGridDataUpdateCoordinator = entry.runtime_data
    async_add_entities(
        NationalGridForceRefreshButton(coordinator, sp)
        for sp in coordinator.data.meters
    )


class NationalGridForceRefreshButton(NationalGridEntity, ButtonEntity):
    """Button to force a full historical AMI re-import for one meter."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:refresh"
    _attr_translation_key = "force_refresh"

    @property
    def name(self) -> str:
        """Return entity name."""
        return "Force Refresh"

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._service_point_number}_force_refresh"

    async def async_press(self) -> None:
        """Fetch full AMI history for this meter and re-import its statistics."""
        await self.coordinator.async_force_refresh_meter(
            self._service_point_number
        )
