"""Import AMI energy data into Home Assistant long-term statistics.

Creates external statistic series for energy usage:

For electric meters:
- 15-min AMI stats (consumption and optional return): fetched via
  get_ami_energy_usages_15min(), covering the accessible ~45-day hot-storage window.

For gas meters:
- 15-min AMI stats: same endpoint, same window.

For meters without an AMI smart meter:
- Billed usage and cost from bill history, spread evenly across each billing
  period's days (daily stats). Bill totals are real; daily values are estimates.

Import strategy:
- First refresh: Import all 15-min records available (typically ~45 days)
- Midnight refresh: Import all records in the 5-day window, continuing cumulative
  sum from before the window (catches backfilled/newly available data)
- Incremental: Import latest 15-min records (last 7-day fetch window)
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
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
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .const import _LOGGER, DOMAIN

# StatisticMeanType was added in HA 2025.11
try:
    from homeassistant.components.recorder.models import (
        StatisticMeanType,
    )

    HAS_MEAN_TYPE = True  # pragma: no cover
except ImportError:  # pragma: no cover
    HAS_MEAN_TYPE = False  # pragma: no cover

if TYPE_CHECKING:  # pragma: no cover
    from homeassistant.core import HomeAssistant

    from .coordinator import NationalGridDataUpdateCoordinator


def _build_statistic_metadata(
    statistic_id: str,
    name: str,
    unit: str | None,
    unit_class: str | None,
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
        kwargs["mean_type"] = StatisticMeanType.NONE  # pragma: no cover
    return StatisticMetaData(**kwargs)  # type: ignore[typeddict-item]


def _resolve_hourly_stat_info(
    service_point: str,
    account_id: str,
    *,
    is_gas: bool,
    return_only: bool,
) -> tuple[str, str, str, str, str]:
    """Return (statistic_id, fuel, unit, unit_class, stat_name)."""
    prefix = f"{account_id}_{service_point}"
    display = f"{account_id}-{service_point}"
    if is_gas:
        fuel = "gas"
        stat_id = f"{DOMAIN}:{prefix}_{fuel}_hourly_usage"
        return stat_id, fuel, "CCF", "volume", f"{display} Gas Hourly Usage"
    fuel = "electric"
    if return_only:
        stat_id = f"{DOMAIN}:{prefix}_{fuel}_return_hourly_usage"
        name = f"{display} Electric Return Hourly Usage"
    else:
        stat_id = f"{DOMAIN}:{prefix}_{fuel}_hourly_usage"
        name = f"{display} Electric Hourly Usage"
    return stat_id, fuel, UnitOfEnergy.KILO_WATT_HOUR, "energy", name


def _parse_ami_datetime(date_str: str) -> datetime | None:
    """Parse an API date string into a UTC datetime, preserving 15-min precision.

    Handles both AMI timestamps ("2026-01-31T23:15:00.000Z") and interval-read
    timestamps ("2026-01-22T13:00:00-05:00"). All results are normalised to UTC.
    Sub-second precision is stripped; the minute is preserved so 15-min
    interval boundaries are maintained in statistics.
    """
    try:
        # Strip fractional seconds and handle Z suffix
        clean = re.sub(r"\.\d+", "", date_str)
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        return dt.replace(second=0, microsecond=0)  # keep minute; drop sub-minute
    except ValueError:
        _LOGGER.debug("Could not parse AMI date: %s", date_str)
        return None


async def async_import_meter_statistics(
    hass: HomeAssistant,
    coordinator: NationalGridDataUpdateCoordinator,
    service_point: str,
    *,
    force_import_all: bool = False,
) -> None:
    """Import statistics for a single service point.

    Used by the Force Refresh button and coordinator.async_force_refresh_meter()
    to re-import one meter's history without touching any other meters.
    """
    data = coordinator.data
    if data is None:
        return
    ami_readings = data.ami_usages.get(service_point, [])
    if not ami_readings:
        _LOGGER.debug("No AMI readings for %s — nothing to import", service_point)
        return
    meter_data = data.meters.get(service_point)
    if meter_data is None:
        _LOGGER.debug("No meter data for %s — skipping import", service_point)
        return

    fuel_type = str(meter_data.meter.get("fuelType", ""))
    is_gas = fuel_type == "Gas"

    account_id = meter_data.account_id
    if is_gas:
        await _import_hourly_stats(
            hass,
            service_point,
            account_id,
            ami_readings,
            is_gas=True,
            force_import_all=force_import_all,
        )
    else:
        await _import_hourly_stats_electric(
            hass,
            service_point,
            account_id,
            ami_readings,
            force_import_all=force_import_all,
        )


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
        account_id = meter_data.account_id

        if is_gas:
            await _import_hourly_stats(
                hass,
                sp,
                account_id,
                ami_readings,
                is_gas=True,
                force_import_all=force_hourly_import,
                is_midnight_refresh=is_midnight_refresh,
            )
        else:
            await _import_hourly_stats_electric(
                hass,
                sp,
                account_id,
                ami_readings,
                force_import_all=force_hourly_import,
                is_midnight_refresh=is_midnight_refresh,
            )

    # Import interval read stats (electric only; always cleared and reimported)
    for sp, reads in data.interval_reads.items():
        meter_data = data.meters.get(sp)
        if meter_data is None:
            continue
        await _import_interval_stats_electric(hass, sp, meter_data.account_id, reads)

    # Import billed usage/cost for meters without AMI data
    await _import_billed_stats(hass, coordinator)

    _LOGGER.info("Statistics import complete")


async def _import_hourly_stats_electric(  # noqa: PLR0913
    hass: HomeAssistant,
    service_point: str,
    account_id: str,
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
        account_id,
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
            account_id,
            readings,
            is_gas=False,
            return_only=True,
            force_import_all=force_import_all,
            is_midnight_refresh=is_midnight_refresh,
        )


async def _import_hourly_stats(  # noqa: PLR0913
    hass: HomeAssistant,
    service_point: str,
    account_id: str,
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
        account_id,
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
    """Build hourly StatisticData from AMI readings, aggregating sub-hour intervals.

    HA statistics require top-of-hour timestamps (minutes and seconds must be 0).
    15-min readings within the same clock hour are summed into a single
    StatisticData entry.

    Returns (stats_list, running_sum).
    """
    sorted_readings = sorted(
        readings,
        key=lambda r: str(r.get("date", "")),
    )

    # Accumulate quantities per top-of-hour bucket
    hourly: dict[datetime, float] = {}
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

        # HA statistics require top-of-hour timestamps
        bucket = dt.replace(minute=0, second=0, microsecond=0)

        if bucket.timestamp() <= last_ts:
            skipped_already += 1
            continue

        hourly[bucket] = hourly.get(bucket, 0.0) + quantity

    stats: list[StatisticData] = []
    running_sum = last_sum

    for bucket_dt in sorted(hourly):
        hour_total = hourly[bucket_dt]
        running_sum += hour_total
        stats.append(StatisticData(start=bucket_dt, state=hour_total, sum=running_sum))

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


async def _import_interval_stats_electric(
    hass: HomeAssistant,
    service_point: str,
    account_id: str,
    reads: list,
) -> None:
    """Import interval stats for electric meters, split by direction.

    Interval stats bridge the gap between verified AMI data and real-time,
    starting at the hour after the last hour AMI has already published (or
    yesterday midnight UTC when AMI has no stats yet). Always clears and
    reimports so stale provisional data never accumulates.
    """
    await _import_interval_stats(
        hass,
        service_point,
        account_id,
        reads,
        consumption_only=True,
    )

    has_negative = any(float(r.get("value", 0)) < 0 for r in reads)
    if has_negative:
        await _import_interval_stats(
            hass,
            service_point,
            account_id,
            reads,
            return_only=True,
        )


async def _get_last_statistic_start(hass: HomeAssistant, statistic_id: str) -> float:
    """Return the start timestamp of the most recent stat row, or 0 if none."""
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
        return last[statistic_id][0].get("start") or 0.0
    return 0.0


async def get_ami_last_covered_hour(
    hass: HomeAssistant, service_point: str, account_id: str
) -> datetime | None:
    """Return the most recent hour the AMI hourly stat series already covers.

    Used by the coordinator to size the interval-read fetch window so it
    reaches back to where AMI coverage actually ends, instead of a fixed
    lookback that leaves a gap when AMI publishing falls behind (AMI only
    refreshes once daily, at midnight).
    """
    stat_id, *_ = _resolve_hourly_stat_info(
        service_point, account_id, is_gas=False, return_only=False
    )
    last_ts = await _get_last_statistic_start(hass, stat_id)
    if not last_ts:
        return None
    return datetime.fromtimestamp(last_ts, tz=UTC)


async def _import_interval_stats(  # noqa: PLR0913
    hass: HomeAssistant,
    service_point: str,
    account_id: str,
    reads: list,
    *,
    consumption_only: bool = False,
    return_only: bool = False,
) -> None:
    """Import 15-min interval stats for a single electric meter.

    Always clears and reimports from the hour after the last hour AMI has
    already published. This covers the near-real-time window that verified AMI
    data does not yet include, and joins the two series with no gap and no
    overlap, so nothing is double-counted in the Energy dashboard. Only when
    AMI has no stats yet does the window start at yesterday midnight UTC.
    """
    ami_stat_id, *_ = _resolve_hourly_stat_info(
        service_point, account_id, is_gas=False, return_only=return_only
    )
    ami_last_ts = await _get_last_statistic_start(hass, ami_stat_id)
    if not ami_last_ts and return_only:
        # No AMI return series yet. The interval fetch starts at the last AMI
        # consumption hour, so anchor there instead of cutting the window.
        consumption_stat_id, *_ = _resolve_hourly_stat_info(
            service_point, account_id, is_gas=False, return_only=False
        )
        ami_last_ts = await _get_last_statistic_start(hass, consumption_stat_id)

    if ami_last_ts:
        # Pick up at the hour after the last verified AMI hour, which is also
        # where the interval fetch window starts.
        cutoff_ts = ami_last_ts
    else:
        # No AMI stats yet (e.g. a fresh install): start at yesterday midnight
        # UTC. Readings at or before the cutoff are dropped, so use the hour
        # before to keep the midnight hour itself.
        yesterday_midnight = (datetime.now(tz=UTC) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        cutoff_ts = (yesterday_midnight - timedelta(hours=1)).timestamp()

    prefix = f"{account_id}_{service_point}"
    display = f"{account_id}-{service_point}"
    if return_only:
        stat_id = f"{DOMAIN}:{prefix}_electric_interval_return_usage"
        stat_name = f"{display} Electric Interval Return Usage"
        stat_type = "return"
    else:
        stat_id = f"{DOMAIN}:{prefix}_electric_interval_usage"
        stat_name = f"{display} Electric Interval Usage"
        stat_type = "consumption"

    _LOGGER.info(
        "Interval %s: cutoff=%s (AMI last hour=%s), clearing and reimporting",
        stat_type,
        datetime.fromtimestamp(cutoff_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC"),
        datetime.fromtimestamp(ami_last_ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        if ami_last_ts
        else "none",
    )

    # Clear existing stats then yield to event loop before writing new ones
    recorder = get_instance(hass)
    recorder.async_clear_statistics([stat_id])
    await asyncio.sleep(0)

    hourly_buckets = _bucket_interval_reads(
        reads,
        cutoff_ts,
        consumption_only=consumption_only,
        return_only=return_only,
        stat_type=stat_type,
    )

    stats: list[StatisticData] = []
    running_sum = 0.0
    for hour_start in sorted(hourly_buckets):
        hour_total = hourly_buckets[hour_start]
        running_sum += hour_total
        stats.append(StatisticData(start=hour_start, state=hour_total, sum=running_sum))

    if not stats:
        _LOGGER.info(
            "Interval %s: no data within window for %s", stat_type, service_point
        )
        return

    metadata = _build_statistic_metadata(
        stat_id,
        stat_name,
        UnitOfEnergy.KILO_WATT_HOUR,
        "energy",
    )
    async_add_external_statistics(hass, metadata, stats)

    _LOGGER.info(
        "Imported %s interval %s stats for %s (sum=%.3f)",
        len(stats),
        stat_type,
        service_point,
        running_sum,
    )


def _bucket_interval_reads(
    reads: list,
    cutoff_ts: float,
    *,
    consumption_only: bool,
    return_only: bool,
    stat_type: str,
) -> dict[datetime, float]:
    """Bucket interval reads into top-of-hour totals.

    Filters by direction (consumption vs return) and drops readings whose
    hour is at or before cutoff_ts (the last AMI-covered hour, or the hour
    before yesterday midnight UTC when AMI has no stats yet).
    """
    hourly_buckets: dict[datetime, float] = {}
    skipped_filtered = 0
    skipped_old = 0

    for read in reads:
        start_str = str(read.get("startTime", ""))
        value = float(read.get("value", 0))
        if not start_str:
            continue

        if consumption_only and value < 0:
            skipped_filtered += 1
            continue
        if return_only and value >= 0:
            skipped_filtered += 1
            continue
        if return_only:
            value = abs(value)

        try:
            dt = datetime.fromisoformat(start_str)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        except ValueError:
            _LOGGER.debug("Could not parse interval startTime: %s", start_str)
            continue

        hour_start = dt.replace(minute=0, second=0, microsecond=0)
        if hour_start.timestamp() <= cutoff_ts:
            skipped_old += 1
            continue

        hourly_buckets[hour_start] = hourly_buckets.get(hour_start, 0.0) + value

    if skipped_filtered > 0:
        label = "consumption" if consumption_only else "return"
        _LOGGER.debug(
            "Filtered %s interval readings (keeping %s only)",
            skipped_filtered,
            label,
        )
    if skipped_old > 0:
        _LOGGER.info(
            "Interval %s: skipped %s readings older than cutoff",
            stat_type,
            skipped_old,
        )

    return hourly_buckets


# Older HA releases have no therm energy unit; gas billed usage is skipped
# there rather than stored in another unit that would later need converting.
_UNIT_THERM = getattr(UnitOfEnergy, "THERM", "thm")
_THERM_SUPPORTED = _UNIT_THERM in EnergyConverter.VALID_UNITS

# Bill history record field holding the billed quantity, per meter fuelType
_BILLED_QUANTITY_FIELDS = {"Electric": "totalKwh", "Gas": "totalTherms"}


def _resolve_billed_stat_info(
    service_point: str,
    account_id: str,
    *,
    is_gas: bool,
    is_cost: bool,
) -> tuple[str, str, str | None, str | None]:
    """Return (statistic_id, stat_name, unit, unit_class) for a billed series."""
    # Recorder rejects statistic IDs with uppercase characters
    prefix = f"{account_id}_{service_point}".lower()
    display = f"{account_id}-{service_point}"
    fuel = "gas" if is_gas else "electric"
    label = "Gas" if is_gas else "Electric"
    if is_cost:
        # Monetary series carry no unit, matching HA's Opower integration
        return (
            f"{DOMAIN}:{prefix}_{fuel}_billed_cost",
            f"{display} {label} Billed Cost",
            None,
            None,
        )
    unit = _UNIT_THERM if is_gas else UnitOfEnergy.KILO_WATT_HOUR
    return (
        f"{DOMAIN}:{prefix}_{fuel}_billed_usage",
        f"{display} {label} Billed Usage",
        unit,
        "energy",
    )


def _parse_bill_date(date_str: str) -> date | None:
    """Parse the date part of a bill history date ("YYYY-MM-DD[T...]")."""
    try:
        return date.fromisoformat(date_str[:10])
    except ValueError:
        return None


async def _import_billed_stats(
    hass: HomeAssistant,
    coordinator: NationalGridDataUpdateCoordinator,
) -> None:
    """Import billed usage and cost for meters without AMI data.

    Bill history is reported per account and fuel type, so a fuel type is
    skipped when an account has more than one non-AMI meter of it (the totals
    could not be attributed to a single meter).
    """
    data = coordinator.data
    if data is None:
        return

    candidates: dict[tuple[str, str], list[str]] = {}
    for sp, meter_data in data.meters.items():
        if meter_data.meter.get("hasAmiSmartMeter") or sp in data.ami_usages:
            continue
        fuel_type = str(meter_data.meter.get("fuelType", ""))
        if fuel_type not in _BILLED_QUANTITY_FIELDS:
            continue
        candidates.setdefault((meter_data.account_id, fuel_type), []).append(sp)

    for (account_id, fuel_type), service_points in candidates.items():
        if len(service_points) > 1:
            _LOGGER.warning(
                "Account %s has %s %s meters without AMI; billed usage cannot "
                "be split between them, skipping billed statistics",
                account_id,
                len(service_points),
                fuel_type,
            )
            continue
        sp = service_points[0]
        is_gas = fuel_type == "Gas"
        history = (data.gas_bill_history if is_gas else data.electric_bill_history).get(
            account_id, []
        )

        series = [(True, "totalCharges")]
        if is_gas and not _THERM_SUPPORTED:
            _LOGGER.warning(
                "This Home Assistant version has no therm energy unit; "
                "skipping billed gas usage statistics for %s",
                sp,
            )
        else:
            series.append((False, _BILLED_QUANTITY_FIELDS[fuel_type]))

        for is_cost, field_name in series:
            stat_id, stat_name, unit, unit_class = _resolve_billed_stat_info(
                sp, account_id, is_gas=is_gas, is_cost=is_cost
            )
            daily = _spread_bills_by_day(history, field_name)
            await _import_daily_series(
                hass, stat_id, stat_name, unit, unit_class, daily
            )


def _spread_bills_by_day(records: list, field_name: str) -> dict[date, float]:
    """Spread each bill's total evenly over the days of its billing period.

    A period covers readFromDate up to (not including) readDate.
    """
    daily: dict[date, float] = {}
    for record in records:
        start = _parse_bill_date(str(record.get("readFromDate", "")))
        end = _parse_bill_date(str(record.get("readDate", "")))
        total = record.get(field_name)
        if start is None or end is None or end <= start or total is None:
            continue
        days = (end - start).days
        per_day = float(total) / days
        for offset in range(days):
            day = start + timedelta(days=offset)
            daily[day] = daily.get(day, 0.0) + per_day
    return daily


async def _import_daily_series(  # noqa: PLR0913
    hass: HomeAssistant,
    statistic_id: str,
    stat_name: str,
    unit: str | None,
    unit_class: str | None,
    daily: dict[date, float],
) -> None:
    """Import per-day values as stats starting at local midnight.

    The whole available window is rewritten on every refresh (so revised bills
    are picked up), continuing the cumulative sum from the last stat before
    the window. Days that have not started yet are skipped.
    """
    tz = dt_util.get_default_time_zone()
    today = dt_util.now(tz).date()
    days = sorted(day for day in daily if day <= today)
    if not days:
        return

    first = days[0]
    window_start = datetime(first.year, first.month, first.day, tzinfo=tz)
    running_sum = await _get_sum_before(hass, statistic_id, window_start)

    stats: list[StatisticData] = []
    for day in days:
        running_sum += daily[day]
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        stats.append(StatisticData(start=start, state=daily[day], sum=running_sum))

    metadata = _build_statistic_metadata(statistic_id, stat_name, unit, unit_class)
    async_add_external_statistics(hass, metadata, stats)
    _LOGGER.info(
        "Imported %s daily billed stats for %s (%s to %s, sum=%.3f)",
        len(stats),
        statistic_id,
        days[0],
        days[-1],
        running_sum,
    )


async def _get_sum_before(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> float:
    """Return the cumulative sum of the last stat starting before `before`."""
    stats = await get_instance(hass).async_add_executor_job(
        partial(
            statistics_during_period,
            hass,
            datetime.fromtimestamp(0, tz=UTC),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
    )
    if stats.get(statistic_id):
        return stats[statistic_id][-1].get("sum") or 0.0
    return 0.0
