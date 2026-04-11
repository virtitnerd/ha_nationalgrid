"""DataUpdateCoordinator for national_grid."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from py_nationalgrid import NationalGridClient, NationalGridConfig, create_cookie_jar
from py_nationalgrid.exceptions import (
    CannotConnectError,
    InvalidAuthError,
    NationalGridError,
    RetryExhaustedError,
)

from .const import _LOGGER, CONF_SELECTED_ACCOUNTS, DOMAIN

if TYPE_CHECKING:
    import logging

    from homeassistant.core import HomeAssistant
    from py_nationalgrid.models import (
        AmiEnergyUsage,
        BillingAccount,
        EnergyUsage,
        EnergyUsageCost,
        Meter,
    )

    from .data import NationalGridConfigEntry

# Truncate datetime strings to "YYYY-MM-DDTHH:MM" for log readability
_DATETIME_LOG_LEN = 16


@dataclass
class MeterData:
    """Data for a single meter."""

    meter: Meter
    account_id: str
    billing_account: BillingAccount


@dataclass
class NationalGridCoordinatorData:
    """Data returned by the coordinator."""

    accounts: dict[str, BillingAccount]
    ami_usages: dict[str, list[AmiEnergyUsage]] = field(default_factory=dict)
    costs: dict[str, list[EnergyUsageCost]] = field(default_factory=dict)
    meters: dict[str, MeterData] = field(default_factory=dict)
    usages: dict[str, list[EnergyUsage]] = field(default_factory=dict)
    is_first_refresh: bool = False
    is_midnight_refresh: bool = False  # Midnight refresh: force full hourly import


class NationalGridDataUpdateCoordinator(
    DataUpdateCoordinator[NationalGridCoordinatorData]
):
    """Class to manage fetching data from the API."""

    config_entry: NationalGridConfigEntry

    def __init__(  # noqa: PLR0913
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        update_interval: timedelta | None,
        username: str,
        password: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, logger, name=name, update_interval=update_interval)
        session = async_create_clientsession(hass, cookie_jar=create_cookie_jar())
        self.api = NationalGridClient(
            config=NationalGridConfig(username=username, password=password),
            session=session,
        )
        self._previous_update_success = True
        self._is_first_refresh = True
        self._interval_only_mode = False  # When True, only fetch interval reads
        self._is_midnight_refresh = False  # When True, force full hourly import
        self._pending_full_refresh = False  # Retry flag for failed full refreshes
        self._store: Store | None = None  # Initialised in async_initialize()

    async def async_initialize(self) -> None:
        """Load persisted state and configure initial refresh mode.

        Must be called after config_entry is set, before the first refresh.
        Reads from HA storage so that a completed initial import is not
        re-run on every Home Assistant restart.
        """
        self._store = Store(
            self.hass, 1, f"{DOMAIN}.{self.config_entry.entry_id}"
        )
        stored: dict = await self._store.async_load() or {}
        if stored.get("initial_import_done", False):
            self._is_first_refresh = False
            _LOGGER.debug(
                "Skipping full first-refresh — initial import already done"
            )

    async def async_refresh_interval_only(self) -> None:
        """Refresh only interval data (skip AMI data)."""
        self._interval_only_mode = True
        try:
            await self.async_refresh()
        finally:
            self._interval_only_mode = False

    @property
    def pending_full_refresh(self) -> bool:
        """Whether a full refresh needs to be retried."""
        return self._pending_full_refresh

    async def async_refresh_full_with_clear(self) -> None:
        """Full refresh for midnight: fetch all data + import statistics.

        If the refresh fails, sets a pending flag so the next scheduled
        interval retries a full refresh instead of interval-only.
        """
        self._is_midnight_refresh = True
        try:
            await self.async_refresh()
        finally:
            self._is_midnight_refresh = False

        if self.last_update_success:
            self._pending_full_refresh = False
        else:
            self._pending_full_refresh = True
            _LOGGER.warning(
                "Full refresh failed — will retry at next scheduled interval"
            )

    async def _async_update_data(self) -> NationalGridCoordinatorData:
        """Update data via library."""
        mode = "interval-only" if self._interval_only_mode else "full"
        try:
            data = await self._fetch_all_data()
        except InvalidAuthError as exception:
            _LOGGER.error(
                "Authentication failed during %s refresh: %s", mode, exception
            )
            raise ConfigEntryAuthFailed(exception) from exception
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
        ) as exception:
            _LOGGER.warning("National Grid %s refresh failed: %s", mode, exception)
            if self._previous_update_success:
                _LOGGER.warning("National Grid service unavailable: %s", exception)
            self._previous_update_success = False
            raise UpdateFailed(exception) from exception

        if not self._previous_update_success:
            _LOGGER.info("National Grid service recovered")
        self._previous_update_success = True
        return data

    async def _fetch_all_data(self) -> NationalGridCoordinatorData:  # noqa: PLR0912
        """Fetch all data from the API."""
        selected_accounts: list[str] = self.config_entry.data.get(
            CONF_SELECTED_ACCOUNTS, []
        )

        if self._interval_only_mode:
            _LOGGER.info("Interval-only refresh started")
        else:
            _LOGGER.info(
                "Full refresh started for %s account(s)", len(selected_accounts)
            )

        # Seed from previous data to preserve stale data on per-account errors.
        data = self._seed_from_previous()

        # Mark if this is the first refresh for historical import
        data.is_first_refresh = self._is_first_refresh

        # Mark if this is a midnight refresh
        data.is_midnight_refresh = self._is_midnight_refresh

        if self._is_first_refresh:
            _LOGGER.info(
                "First refresh - will import full historical data (from epoch)"
            )

        if self._is_midnight_refresh:
            _LOGGER.info("Midnight refresh - will force full hourly import")

        # Calculate from_month for usage query.
        # On first refresh: get up to 465 days of history
        # On subsequent refreshes: only get last 12 months
        today = datetime.now(tz=UTC).date()
        if self._is_first_refresh:
            # Go back ~465 days (15 months)
            from_date = today - timedelta(days=465)
            from_month = from_date.year * 100 + from_date.month
        else:
            # Normal operation: 12 months back
            from_month = (today.year - 1) * 100 + today.month

        if not self._interval_only_mode:
            _LOGGER.debug("Fetching usages from month: %s", from_month)

        for account_id in selected_accounts:
            try:
                await self._fetch_account_data(account_id, today, from_month, data)
            except InvalidAuthError:
                # Re-raise auth errors to trigger reauth flow.
                raise
            except (
                CannotConnectError,
                RetryExhaustedError,
                NationalGridError,
            ) as err:
                _LOGGER.warning(
                    "Error fetching data for account %s: %s", account_id, err
                )
                continue

        # Log completion at INFO level with summary
        if self._interval_only_mode:
            ami_count = sum(len(a) for a in data.ami_usages.values())
            _LOGGER.info(
                "Interval-only refresh complete: %s AMI 15-min records fetched",
                ami_count,
            )
        else:
            ami_count = sum(len(a) for a in data.ami_usages.values())
            _LOGGER.info(
                "Full refresh complete: %s AMI 15-min records fetched",
                ami_count,
            )

        # After first successful refresh, mark as complete and persist so
        # subsequent HA restarts skip the slow epoch→today AMI fetch.
        if self._is_first_refresh:
            self._is_first_refresh = False
            _LOGGER.info("First refresh complete - switching to incremental updates")
            if self._store is not None:
                await self._store.async_save({"initial_import_done": True})

        return data

    def _seed_from_previous(self) -> NationalGridCoordinatorData:
        """Create coordinator data seeded from previous fetch results."""
        prev = self.data
        if prev is None:
            return NationalGridCoordinatorData(accounts={})
        return NationalGridCoordinatorData(
            accounts=dict(prev.accounts),
            ami_usages=dict(prev.ami_usages),
            costs=dict(prev.costs),
            meters=dict(prev.meters),
            usages=dict(prev.usages),
        )

    async def _fetch_account_data(
        self,
        account_id: str,
        today: date,
        from_month: int,
        data: NationalGridCoordinatorData,
    ) -> None:
        """Fetch billing, usage, cost, and AMI data for a single account."""
        # Fetch billing account info (always needed for premise number).
        _LOGGER.debug("Fetching billing account: %s", account_id)
        billing_account = await self.api.get_billing_account(account_id)
        data.accounts[account_id] = billing_account
        _LOGGER.debug(
            "Billing account %s: region=%s, meters=%s",
            account_id,
            billing_account.get("region"),
            len(billing_account.get("meter", {}).get("nodes", [])),
        )

        # Extract meters from the billing account.
        meter_nodes = billing_account.get("meter", {}).get("nodes", [])
        for meter in meter_nodes:
            service_point = str(meter.get("servicePointNumber", ""))
            if service_point:
                data.meters[service_point] = MeterData(
                    meter=meter,
                    account_id=account_id,
                    billing_account=billing_account,
                )
                _LOGGER.debug(
                    "Found meter: service_point=%s, fuel_type=%s",
                    service_point,
                    meter.get("fuelType"),
                )

        # Skip usage/cost fetching in interval-only mode (doesn't change frequently)
        if not self._interval_only_mode:
            # Fetch energy usages.
            data.usages[account_id] = await self._fetch_usages(account_id, from_month)

            # Fetch energy costs (company_code is the region from billing account).
            data.costs[account_id] = await self._fetch_costs(
                account_id, today, billing_account
            )

        # Fetch AMI 15-min data for AMI-capable meters.
        await self._fetch_ami_data(
            billing_account,
            meter_nodes,
            today,
            data.ami_usages,
            is_first_refresh=data.is_first_refresh,
        )

    async def _fetch_usages(
        self, account_id: str, from_month: int
    ) -> list[EnergyUsage]:
        """Fetch energy usage records for an account.

        Args:
            account_id: Account ID to query.
            from_month: Start month in YYYYMM format.

        Returns:
            List of energy usage records.

        """
        try:
            account_usages = await self.api.get_energy_usages(
                account_number=account_id,
                from_month=from_month,
            )
            _LOGGER.debug(
                "Fetched %s usage records for account %s",
                len(account_usages),
                account_id,
            )
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
            ValueError,
        ) as err:
            _LOGGER.debug(
                "Could not fetch energy usages for account %s: %s",
                account_id,
                err,
            )
            return []
        else:
            return account_usages

    async def _fetch_costs(
        self,
        account_id: str,
        today: date,
        billing_account: BillingAccount,
    ) -> list[EnergyUsageCost]:
        """Fetch energy cost records for an account."""
        try:
            region = billing_account.get("region", "")
            if not region:
                _LOGGER.debug("No region for account %s, skipping costs", account_id)
                return []
            account_costs = await self.api.get_energy_usage_costs(
                account_number=account_id,
                query_date=today,
                company_code=region,
            )
            _LOGGER.debug(
                "Fetched %s cost records for account %s",
                len(account_costs),
                account_id,
            )
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
            ValueError,
        ) as err:
            _LOGGER.debug(
                "Could not fetch energy costs for account %s: %s",
                account_id,
                err,
            )
            return []
        else:
            return account_costs

    async def _fetch_ami_data(
        self,
        billing_account: BillingAccount,
        meter_nodes: list[Meter],
        today: date,
        ami_usages: dict[str, list[AmiEnergyUsage]],
        *,
        is_first_refresh: bool = False,
    ) -> None:
        """Fetch AMI 15-minute data for all AMI-capable meters in an account."""
        premise_number = str(billing_account.get("premiseNumber", ""))
        for meter in meter_nodes:
            if not meter.get("hasAmiSmartMeter"):
                continue
            sp = str(meter.get("servicePointNumber", ""))
            if not sp:
                continue

            await self._fetch_ami_15min_data(
                meter,
                premise_number,
                sp,
                today,
                ami_usages,
                is_first_refresh=is_first_refresh,
            )

    async def _fetch_ami_15min_data(  # noqa: PLR0913
        self,
        meter: Meter,
        premise_number: str,
        sp: str,
        today: date,
        ami_usages: dict[str, list[AmiEnergyUsage]],
        *,
        is_first_refresh: bool = False,
    ) -> None:
        """Fetch AMI 15-minute interval data for a single meter.

        Uses get_ami_energy_usages_15min(), which:
        - Auto-chunks large date ranges into ≤45-day windows
        - Falls back to the daily endpoint for meters that don't support 15-min
        - Gracefully truncates on 504 (cold-storage boundary ~45 days ago)
        - Works for both ELECTRIC and GAS meters

        On first refresh, requests from epoch so the library can collect as much
        history as the API allows (~45 days). Incremental refreshes cover 7 days
        to catch backfilled data.
        """
        try:
            if is_first_refresh:
                date_from = date(1970, 1, 1)
                _LOGGER.info(
                    "First refresh: fetching 15-min AMI from epoch to %s for meter %s "
                    "(accessible window ~45 days; older data truncated gracefully)",
                    today,
                    sp,
                )
            else:
                date_from = today - timedelta(days=7)
                _LOGGER.debug(
                    "Incremental: fetching 15-min AMI from %s to %s for meter %s",
                    date_from,
                    today,
                    sp,
                )

            ami_data = await self.api.get_ami_energy_usages_15min(
                meter_number=str(meter.get("meterNumber", "")),
                premise_number=premise_number,
                service_point_number=sp,
                meter_point_number=str(meter.get("meterPointNumber", "")),
                date_from=date_from,
                date_to=today,
                fuel_type=meter.get("fuelType"),  # library normalises case internally
            )
            ami_usages[sp] = ami_data
            self._log_ami_results(ami_data, sp)
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
        ) as err:
            _LOGGER.debug(
                "Could not fetch 15-min AMI for meter %s: %s",
                sp,
                err,
            )

    @staticmethod
    def _log_ami_results(ami_data: list[AmiEnergyUsage], sp: str) -> None:
        """Log AMI data results with date range info."""
        if ami_data:
            dates = [r.get("date") for r in ami_data if r.get("date")]
            if dates:
                _LOGGER.info(
                    "Fetched %s AMI 15-min records for meter %s (date range: %s to %s)",
                    len(ami_data),
                    sp,
                    min(dates),
                    max(dates),
                )
            else:
                _LOGGER.debug(
                    "Fetched %s AMI 15-min records for meter %s",
                    len(ami_data),
                    sp,
                )
        else:
            _LOGGER.debug(
                "No AMI 15-min records returned for meter %s",
                sp,
            )

    def get_meter_data(self, service_point_number: str) -> MeterData | None:
        """Get meter data by service point number."""
        if self.data is None:
            return None
        return self.data.meters.get(service_point_number)

    def get_latest_usage(
        self, account_id: str, fuel_type: str | None = None
    ) -> EnergyUsage | None:
        """Get the most recent energy usage for an account."""
        if self.data is None:
            return None
        account_usages = self.data.usages.get(account_id, [])
        if not account_usages:
            return None

        # Filter by fuel type if specified.
        # Map meter fuel type to usage type: Electric->KWH, Gas->THERMS.
        filtered = account_usages
        if fuel_type:
            usage_type_map = {
                "Electric": "TOTAL_KWH",
                "Gas": "THERMS",
            }
            usage_type = usage_type_map.get(fuel_type, fuel_type.upper())
            filtered = [u for u in account_usages if u.get("usageType") == usage_type]
            _LOGGER.debug(
                "Filtering usages: fuel_type=%s -> usage_type=%s, found %s matches",
                fuel_type,
                usage_type,
                len(filtered),
            )

        if not filtered:
            return None

        # Return most recent (highest usageYearMonth).
        return max(filtered, key=lambda u: u.get("usageYearMonth", 0))

    def get_latest_cost(
        self, account_id: str, fuel_type: str | None = None
    ) -> EnergyUsageCost | None:
        """Get the most recent energy cost for an account."""
        if self.data is None:
            return None
        account_costs = self.data.costs.get(account_id, [])
        if not account_costs:
            return None

        # Filter by fuel type if specified.
        # Cost records use fuelType field which may be "ELECTRIC" or "GAS" (uppercase).
        filtered = account_costs
        if fuel_type:
            # Try exact match first, then uppercase match.
            filtered = [
                c
                for c in account_costs
                if c.get("fuelType") == fuel_type
                or c.get("fuelType") == fuel_type.upper()
            ]

        if not filtered:
            return None

        # Return most recent (highest month).
        return max(filtered, key=lambda c: c.get("month", 0))

    def get_all_usages(
        self, account_id: str, fuel_type: str | None = None
    ) -> list[EnergyUsage]:
        """Get all energy usages for an account, filtered by fuel type."""
        if self.data is None:
            return []
        account_usages = self.data.usages.get(account_id, [])
        if not account_usages:
            return []

        # Filter by fuel type if specified.
        if fuel_type:
            usage_type_map = {
                "Electric": "TOTAL_KWH",
                "Gas": "THERMS",
            }
            usage_type = usage_type_map.get(fuel_type, fuel_type.upper())
            return [u for u in account_usages if u.get("usageType") == usage_type]

        return list(account_usages)

    def get_all_costs(
        self, account_id: str, fuel_type: str | None = None
    ) -> list[EnergyUsageCost]:
        """Get all energy costs for an account, filtered by fuel type."""
        if self.data is None:
            return []
        account_costs = self.data.costs.get(account_id, [])
        if not account_costs:
            return []

        # Filter by fuel type if specified.
        if fuel_type:
            return [
                c
                for c in account_costs
                if c.get("fuelType") == fuel_type
                or c.get("fuelType") == fuel_type.upper()
            ]

        return list(account_costs)

    def get_latest_ami_usage(self, service_point_number: str) -> AmiEnergyUsage | None:
        """Get the most recent AMI usage reading for a service point."""
        if self.data is None:
            return None
        readings = self.data.ami_usages.get(service_point_number, [])
        if not readings:
            return None
        return max(readings, key=lambda r: r.get("date", ""))

    def reset_to_first_refresh(self) -> None:
        """Reset the coordinator to perform a full historical data import.

        This sets the first refresh flag to True, which will cause the next
        refresh to fetch full historical data instead of just recent incremental
        data. Useful for recovering from data gaps or re-importing statistics.
        Also clears the persisted storage flag so the next HA restart also
        performs a full import.
        """
        _LOGGER.info(
            "Resetting coordinator to first refresh mode for full historical import"
        )
        self._is_first_refresh = True
        if self._store is not None:
            self.hass.async_create_task(
                self._store.async_save({"initial_import_done": False})
            )

    async def async_force_refresh_meter(self, service_point: str) -> None:
        """Fetch full AMI history for one meter and re-import its statistics.

        Used by the Force Refresh button entity. Fetches from epoch (as if it
        were a first refresh) for just the given service point, then re-imports
        that meter's statistics with force_import_all=True.
        """
        if self.data is None:
            _LOGGER.warning(
                "Force refresh: coordinator has no data yet for meter %s",
                service_point,
            )
            return
        meter_data = self.get_meter_data(service_point)
        if meter_data is None:
            _LOGGER.warning(
                "Force refresh: meter %s not found in coordinator data", service_point
            )
            return

        today = datetime.now(tz=UTC).date()
        premise_number = str(meter_data.billing_account.get("premiseNumber", ""))

        _LOGGER.info("Force refresh triggered for meter %s", service_point)
        await self._fetch_ami_15min_data(
            meter=meter_data.meter,
            premise_number=premise_number,
            sp=service_point,
            today=today,
            ami_usages=self.data.ami_usages,
            is_first_refresh=True,  # always fetch from epoch
        )

        # Import stats for just this meter (deferred import avoids circular import
        # at module level; statistics.py imports coordinator only under TYPE_CHECKING)
        from .statistics import async_import_meter_statistics  # noqa: PLC0415

        await async_import_meter_statistics(
            self.hass, self, service_point, force_import_all=True
        )
