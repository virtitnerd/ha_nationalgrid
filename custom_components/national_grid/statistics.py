"""Import AMI energy data into Home Assistant long-term statistics.

Creates external statistic series for energy usage:

For electric meters:
- 15-min AMI stats (consumption and optional return): fetched via
  get_ami_energy_usages_15min(), covering the accessible ~45-day hot-storage window.

For gas meters:
- 15-min AMI stats: same endpoint, same window.

Import strategy:
- First refresh: Import all 15-min records available (typically ~45 days)
- Midnight refresh: Import all records in the 5-day window, continuing cumulative
  sum from before the window (catches backfilled/newly available data)
- Incremental: Import latest 15-min records (last 7-day fetch window)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy

from .const import _LOGGER, DOMAIN

# StatisticMeanType was added in HA 2025.11
try:
    from homeassistant.components.recorder.models import (
        StatisticMeanType,
    )

    HAS_MEAN_TYPE = True
except ImportError:
    HAS_MEAN_TYPE = False

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .coordinator import NationalGridDataUpdateCoordinator


def _build_statistic_metadata(
    statistic_id: str,
    name: str,
    unit: str,
    unit_class: str,
) -> StatisticMetaData:
    """Build StatisticMetaData with compatibility shims."""
    kwargs: dict[str, object] = {
        "has_mean": False,
        "has_sum": True,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "unit_class": unit_class,
    }
    if HAS_MEAN_TYPE:
        kwargs["mean_type"] = StatisticMeanType.NONE
    return StatisticMetaData(**kwargs)


def _resolve_hourly_stat_info(
    service_point: str,
    *,
    is_gas: bool,
    return_only: bool,
) -> tuple[str, str, str, str, str]:
    """Return (statistic_id, fuel, unit, unit_class, stat_name)."""
    if is_gas:
        fuel = "gas"
        stat_id = f"{DOMAIN}:{service_point}_{fuel}_hourly_usage"
        return stat_id, fuel, "CCF", "volume", f"{service_point} Gas Hourly Usage"
    fuel = "electric"
    if return_only:
        stat_id = f"{DOMAIN}:{service_point}_{fuel}_return_hourly_usage"
        name = f"{service_point} Electric Return Hourly Usage"
    else:
        stat_id = f"{DOMAIN}:{service_point}_{fuel}_hourly_usage"
        name = f"{service_point} Electric Hourly Usage"
    return stat_id, fuel, UnitOfEnergy.KILO_WATT_HOUR, "energy", name


def _parse_ami_datetime(date_str: str) -> datetime | None:
    """Parse AMI API date string into a UTC datetime, preserving 15-min precision.

    The API returns timestamps like "2026-01-31T23:15:00.000Z".
    Sub-second precision is stripped; the minute is preserved so 15-min
    interval boundaries are maintained in statistics.
    """
    try:
        # Strip fractional seconds and handle Z suffix
        clean = re.sub(r"\.\d+", "", date_str)
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.replace(second=0, microsecond=0)  # keep minute; drop sub-minute
    except ValueError:
        _LOGGER.debug("Could not parse AMI date: %s", date_str)
        return None


async def async_import_all_statistics(
    hass: HomeAssistant,
    coordinator: NationalGridDataUpdateCoordinator,
) -> None:
    """Import energy usage statistics based on available data."""
    data = coordinator.data
    if data is None:
        _LOGGER.info("No data available to import statistics")
        return

    # Only force-import all hourly data on first refresh (or
    # force_full_refresh which resets to first refresh).
    force_hourly_import = data.is_first_refresh
    is_midnight_refresh = data.is_midnight_refresh

    mode = (
        "first_refresh"
        if data.is_first_refresh
        else "midnight_refresh"
        if data.is_midnight_refresh
        else "incremental"
    )

    _LOGGER.info(
        "Importing statistics: %s AMI meters, %s interval meters, mode=%s",
        len(data.ami_usages),
        len(data.interval_reads),
        mode,
    )

    # Import AMI data for all meters
    for sp, ami_readings in data.ami_usages.items():
        meter_data = data.meters.get(sp)
        if meter_data is None:
            continue
        fuel_type = str(meter_data.meter.get("fuelType", ""))
        is_gas = fuel_type == "Gas"

        if is_gas:
            await _import_hourly_stats(
                hass,
                sp,
                ami_readings,
                is_gas=True,
                force_import_all=force_hourly_import,
                is_midnight_refresh=is_midnight_refresh,
            )
        else:
            await _import_hourly_stats_electric(
                hass,
                sp,
                ami_readings,
                force_import_all=force_hourly_import,
                is_midnight_refresh=is_midnight_refresh,
            )

    _LOGGER.info("Statistics import complete")


async def _import_hourly_stats_electric(
    hass: HomeAssistant,
    service_point: str,
    readings: list,
    *,
    force_import_all: bool = False,
    is_midnight_refresh: bool = False,
) -> None:
    """Import hourly AMI stats for electric, split by direction.

    Creates separate consumption (positive) and return (negative)
    statistics to match OPower / Energy Dashboard conventions.
    """
    await _import_hourly_stats(
        hass,
        service_point,
        readings,
        is_gas=False,
        consumption_only=True,
        force_import_all=force_import_all,
        is_midnight_refresh=is_midnight_refresh,
    )

    has_negative = any(float(r.get("quantity", 0)) < 0 for r in readings)
    if has_negative:
        await _import_hourly_stats(
            hass,
            service_point,
            readings,
            is_gas=False,
            return_only=True,
            force_import_all=force_import_all,
            is_midnight_refresh=is_midnight_refresh,
        )


async def _import_hourly_stats(  # noqa: PLR0913
    hass: HomeAssistant,
    service_point: str,
    readings: list,
    *,
    is_gas: bool,
    consumption_only: bool = False,
    return_only: bool = False,
    force_import_all: bool = False,
    is_midnight_refresh: bool = False,
) -> None:
    """Import hourly AMI usage statistics."""
    stat_id, fuel, unit, unit_class, stat_name = _resolve_hourly_stat_info(
        service_point,
        is_gas=is_gas,
        return_only=return_only,
    )

    last_sum, last_ts = await _get_last_sum_and_ts(
        hass,
        stat_id,
        force_import_all=force_import_all,
        reading_count=len(readings),
        readings=readings,
        is_midnight_refresh=is_midnight_refresh,
    )

    stats, running_sum = _build_hourly_stat_list(
        readings,
        last_sum,
        last_ts,
        consumption_only=consumption_only,
        return_only=return_only,
    )

    if not stats:
        _LOGGER.info(
            "Hourly %s: no new stats to import for %s",
            fuel,
            service_point,
        )
        return

    metadata = _build_statistic_metadata(
        stat_id,
        stat_name,
        unit,
        unit_class,
    )
    async_add_external_statistics(hass, metadata, stats)

    _LOGGER.info(
        "Imported %s hourly AMI stats for %s (sum=%.3f)",
        len(stats),
        stat_id,
        running_sum,
    )


async def _get_last_sum_and_ts(  # noqa: PLR0913
    hass: HomeAssistant,
    statistic_id: str,
    *,
    force_import_all: bool,
    reading_count: int,
    readings: list,
    is_midnight_refresh: bool,
) -> tuple[float, float]:
    """Return (last_sum, last_ts) from recorder, or (0, 0) if forcing.

    For midnight refresh, imports all data in the 5-day window by:
    1. Finding the earliest reading timestamp
    2. Querying for the last statistic before that timestamp
    3. Returning that sum with last_ts set to 0 (to import all readings)
    """
    if force_import_all:
        _LOGGER.info(
            "Force import mode for %s - will import all %d readings (fills gaps)",
            statistic_id,
            reading_count,
        )
        return 0.0, 0.0

    if is_midnight_refresh and readings:
        # Find earliest timestamp in the readings
        earliest_ts: float | None = None
        for reading in readings:
            date_str = str(reading.get("date", ""))
            if not date_str:
                continue
            dt = _parse_ami_datetime(date_str)
            if dt is not None:
                ts = dt.timestamp()
                if earliest_ts is None or ts < earliest_ts:
                    earliest_ts = ts

        if earliest_ts is not None:
            # Query for statistics before the 5-day window
            earliest_dt = datetime.fromtimestamp(earliest_ts, tz=UTC)
            stats = await get_instance(hass).async_add_executor_job(
                partial(
                    statistics_during_period,
                    hass,
                    datetime.fromtimestamp(0, tz=UTC),  # From epoch
                    earliest_dt,  # Until start of window
                    {statistic_id},
                    "hour",
                    None,
                    {"sum"},
                )
            )

            if stats.get(statistic_id):
                # Get the last (most recent) statistic before the window
                last_stat = stats[statistic_id][-1]
                last_sum = last_stat.get("sum") or 0.0
                _LOGGER.info(
                    "Midnight refresh for %s: importing all %d readings "
                    "in 5-day window (continuing from sum=%.3f before %s)",
                    statistic_id,
                    reading_count,
                    last_sum,
                    earliest_dt.strftime("%Y-%m-%d %H:%M UTC"),
                )
                # Return last_ts=0 to import all readings in window
                return last_sum, 0.0

            _LOGGER.info(
                "Midnight refresh for %s: importing all %d readings "
                "in 5-day window (no pre-existing stats, starting from 0)",
                statistic_id,
                reading_count,
            )
            return 0.0, 0.0

    # Normal incremental mode
    last = await get_instance(hass).async_add_executor_job(
        partial(
            get_last_statistics,
            hass,
            1,
            statistic_id,
            convert_units=True,
            types={"sum"},
        )
    )
    if last.get(statistic_id):
        row = last[statistic_id][0]
        return row.get("sum") or 0.0, row.get("start") or 0.0
    return 0.0, 0.0


def _build_hourly_stat_list(
    readings: list,
    last_sum: float,
    last_ts: float,
    *,
    consumption_only: bool = False,
    return_only: bool = False,
) -> tuple[list[StatisticData], float]:
    """Build sorted StatisticData list from AMI readings.

    Returns (stats_list, running_sum).
    """
    sorted_readings = sorted(
        readings,
        key=lambda r: str(r.get("date", "")),
    )
    stats: list[StatisticData] = []
    running_sum = last_sum
    skipped_already = 0
    skipped_filtered = 0

    for reading in sorted_readings:
        date_str = str(reading.get("date", ""))
        quantity = float(reading.get("quantity", 0))
        if not date_str:
            continue

        if consumption_only and quantity < 0:
            skipped_filtered += 1
            continue
        if return_only and quantity >= 0:
            skipped_filtered += 1
            continue
        if return_only:
            quantity = abs(quantity)

        dt = _parse_ami_datetime(date_str)
        if dt is None:
            continue

        if dt.timestamp() <= last_ts:
            skipped_already += 1
            continue

        running_sum += quantity
        stats.append(StatisticData(start=dt, state=quantity, sum=running_sum))

    if skipped_already > 0:
        _LOGGER.debug(
            "Skipped %s already-imported AMI readings",
            skipped_already,
        )
    if skipped_filtered > 0:
        label = "consumption" if consumption_only else "return"
        _LOGGER.debug(
            "Filtered %s readings (keeping %s only)",
            skipped_filtered,
            label,
        )

    return stats, running_sum
