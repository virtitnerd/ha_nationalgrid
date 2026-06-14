"""Custom integration to integrate National Grid US with Home Assistant.

For more details about this integration, please refer to
https://github.com/virtitnerd/ha_nationalgrid
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components.recorder import get_instance as recorder_get_instance
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_change
from sqlalchemy import text as sa_text

from .const import _LOGGER, DOMAIN
from .coordinator import NationalGridDataUpdateCoordinator
from .statistics import async_import_all_statistics

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from homeassistant.core import HomeAssistant, ServiceCall

    from .data import NationalGridConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
]

# Service names
SERVICE_FORCE_REFRESH = "force_full_refresh"

# Service schemas
SERVICE_FORCE_REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
    }
)


async def async_migrate_entry(
    hass: HomeAssistant,
    config_entry: NationalGridConfigEntry,
) -> bool:
    """Migrate config entry to a newer version."""
    _LOGGER.debug("Migrating config entry from version %s", config_entry.version)

    if config_entry.version == 1:
        await _async_migrate_statistics_v1_to_v2(hass)
        hass.config_entries.async_update_entry(config_entry, version=2)
        _LOGGER.info("Migrated National Grid US config entry to version 2")
        return True

    _LOGGER.error("Unknown config entry version: %s", config_entry.version)
    return False


async def _async_migrate_statistics_v1_to_v2(hass: HomeAssistant) -> None:
    """Rename statistics from old national_grid domain to national_grid_us."""
    try:
        instance = recorder_get_instance(hass)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Recorder not available — skipping statistics migration")
        return

    _UPDATE_SQL = (  # noqa: N806
        "UPDATE statistics_meta "
        "SET statistic_id = REPLACE("
        "statistic_id, 'national_grid:', 'national_grid_us:'), "
        "    source = 'national_grid_us' "
        "WHERE source = 'national_grid'"
    )

    def _rename() -> int:
        with instance.get_session() as session:
            result = session.execute(sa_text(_UPDATE_SQL))
            return result.rowcount  # type: ignore[return-value]

    try:
        count = await hass.async_add_executor_job(_rename)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Statistics migration encountered an error: %s", err)
        return

    if count:
        _LOGGER.info(
            "Migrated %d long-term statistics from national_grid to national_grid_us",
            count,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NationalGridConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    coordinator = NationalGridDataUpdateCoordinator(
        hass=hass,
        logger=_LOGGER,
        name=DOMAIN,
        update_interval=None,  # We use time-based scheduling instead
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )
    coordinator.config_entry = entry
    await coordinator.async_initialize()

    entry.runtime_data = coordinator

    await coordinator.async_config_entry_first_refresh()
    # Run the initial statistics import in the background so setup returns
    # immediately after the coordinator data is fetched.  Writing potentially
    # years of 15-min AMI data to the recorder can take tens of seconds and
    # would otherwise block the config-flow UI until it finishes.
    hass.async_create_task(async_import_all_statistics(hass, coordinator))

    # Pre-register Account devices so via_device links resolve correctly when
    # Meter entities from other platforms (binary_sensor, button) are registered.
    registry = dr.async_get(hass)
    for account_id in coordinator.data.accounts:
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, account_id)},
            name=f"National Grid {account_id}",
            manufacturer="National Grid",
            entry_type=dr.DeviceEntryType.SERVICE,
            configuration_url="https://myaccount.nationalgrid.com",
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Re-import statistics on each coordinator update.
    def _on_update() -> None:
        hass.async_create_task(async_import_all_statistics(hass, coordinator))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    # Schedule updates at the 18th minute of every hour
    # - At 00:18 (midnight): Full refresh to sync with new Hourly data
    # - All other hours: Interval-only refresh (just interval reads)
    # - If a full refresh failed, retry it at the next interval
    def _scheduled_refresh(now: datetime) -> None:
        """Refresh data at scheduled time."""
        if now.hour == 0:
            _LOGGER.info(
                "Midnight refresh triggered at %s",
                now,
            )
            hass.add_job(coordinator.async_refresh_full_with_clear)
        elif coordinator.pending_full_refresh:
            _LOGGER.info("Retrying failed full refresh at %s", now)
            hass.add_job(coordinator.async_refresh_full_with_clear)
        else:
            _LOGGER.info(
                "Hourly refresh triggered at %s - fetching Interval data only",
                now,
            )
            hass.add_job(coordinator.async_refresh_interval_only)

    cancel_scheduled = async_track_time_change(
        hass,
        _scheduled_refresh,
        minute=18,
        second=0,
    )
    entry.async_on_unload(cancel_scheduled)

    # Register services (only once, when first entry is set up)
    await _async_setup_services(hass)

    return True


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up National Grid services."""

    async def handle_force_refresh(call: ServiceCall) -> None:
        """Handle the force_full_refresh service call."""
        entry_id = call.data.get("entry_id")

        # Get all National Grid config entries
        entries = hass.config_entries.async_entries(DOMAIN)

        if not entries:
            _LOGGER.warning("No National Grid integrations configured")
            return

        # Filter to specific entry if provided
        if entry_id:
            entries = [e for e in entries if e.entry_id == entry_id]
            if not entries:
                _LOGGER.warning(
                    "No National Grid integration found with entry_id: %s", entry_id
                )
                return

        for entry in entries:
            coordinator: NationalGridDataUpdateCoordinator = entry.runtime_data
            _LOGGER.info(
                "Force full refresh triggered for account: %s",
                entry.title,
            )

            # Reset to first refresh mode to get full historical data
            coordinator.reset_to_first_refresh()

            # Trigger an immediate refresh
            await coordinator.async_refresh()

            # Import statistics after refresh
            await async_import_all_statistics(hass, coordinator)

            _LOGGER.info(
                "Force full refresh completed for account: %s",
                entry.title,
            )

    # Only register if not already registered
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_REFRESH):
        hass.services.async_register(
            DOMAIN,
            SERVICE_FORCE_REFRESH,
            handle_force_refresh,
            schema=SERVICE_FORCE_REFRESH_SCHEMA,
        )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: NationalGridConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok and not any(
        e.entry_id != entry.entry_id for e in hass.config_entries.async_entries(DOMAIN)
    ):
        hass.services.async_remove(DOMAIN, SERVICE_FORCE_REFRESH)
    return unload_ok


async def async_reload_entry(
    hass: HomeAssistant,
    entry: NationalGridConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
