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
        Bill,
        BillingAccount,
        EnergyUsage,
        EnergyUsageCost,
        IntervalRead,
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
    bills: dict[str, list[Bill]] = field(default_factory=dict)
    costs: dict[str, list[EnergyUsageCost]] = field(default_factory=dict)
    interval_reads: dict[str, list[IntervalRead]] = field(default_factory=dict)
    meters: dict[str, MeterData] = field(default_factory=dict)
    reading_dates: dict[str, str | None] = field(default_factory=dict)
    usages: dict[str, list[EnergyUsage]] = field(default_factory=dict)
    is_first_refresh: bool = False
    # Midnight refresh: force full hourly import + clear/reimport interval stats
    is_midnight_refresh: bool = False


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
        self._store = Store(self.hass, 1, f"{DOMAIN}.{self.config_entry.entry_id}")
        stored: dict = await self._store.async_load() or {}
        if stored.get("initial_import_done", False):
            self._is_first_refresh = False
            _LOGGER.debug("Skipping full first-refresh — initial import already done")

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
            _LOGGER.info("First refresh - fetching AMI from epoch via primary endpoint")

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

        # Fetch next scheduled reading dates (one per account, not per meter)
        if not self._interval_only_mode:
            try:
                account_links = await self.api.get_linked_accounts()
                for link in account_links:
                    acct_id = link.get("billingAccountId", "")
                    if acct_id in selected_accounts:
                        billing = link.get("billingAccount") or {}
                        data.reading_dates[acct_id] = billing.get(
                            "nextSchedReadingDate"
                        )
            except (CannotConnectError, RetryExhaustedError, NationalGridError) as err:
                _LOGGER.debug("Could not fetch next reading dates: %s", err)

        # Log completion at INFO level with summary
        if self._interval_only_mode:
            interval_count = sum(len(r) for r in data.interval_reads.values())
            _LOGGER.info(
                "Interval-only refresh complete: %s interval reads fetched",
                interval_count,
            )
        else:
            ami_count = sum(len(a) for a in data.ami_usages.values())
            interval_count = sum(len(r) for r in data.interval_reads.values())
            _LOGGER.info(
                "Full refresh complete: %s AMI 15-min records,"
                " %s interval reads fetched",
                ami_count,
                interval_count,
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
            bills=dict(prev.bills),
            costs=dict(prev.costs),
            interval_reads=dict(prev.interval_reads),
            meters=dict(prev.meters),
            reading_dates=dict(prev.reading_dates),
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

        # Skip usage/cost/bill fetching in interval-only mode
        if not self._interval_only_mode:
            # Fetch energy usages.
            data.usages[account_id] = await self._fetch_usages(account_id, from_month)

            # Fetch energy costs (company_code is the region from billing account).
            data.costs[account_id] = await self._fetch_costs(
                account_id, today, billing_account
            )

            # Fetch bill history.
            data.bills[account_id] = await self._fetch_bills(account_id)

        # Fetch AMI and interval read data for AMI-capable meters.
        # In interval-only mode: skips slow GraphQL AMI fetch, does interval reads only.
        # In full mode: fetches both AMI 15-min data and interval reads.
        await self._fetch_ami_data(
            billing_account,
            meter_nodes,
            today,
            data.ami_usages,
            data.interval_reads,
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

    async def _fetch_bills(self, account_id: str) -> list[Bill]:
        """Fetch bill history for an account."""
        try:
            bills = await self.api.get_bills(account_id)
            _LOGGER.debug("Fetched %s bills for account %s", len(bills), account_id)
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
            ValueError,
        ) as err:
            _LOGGER.debug("Could not fetch bills for account %s: %s", account_id, err)
            return []
        else:
            return bills

    async def _fetch_ami_data(  # noqa: PLR0913
        self,
        billing_account: BillingAccount,
        meter_nodes: list[Meter],
        today: date,
        ami_usages: dict[str, list[AmiEnergyUsage]],
        interval_reads: dict[str, list[IntervalRead]],
        *,
        is_first_refresh: bool = False,
    ) -> None:
        """Fetch AMI and interval read data for all AMI-capable meters in an account.

        In full mode: fetches both AMI 15-min (GraphQL) and interval reads (REST).
        In interval-only mode: skips the slow AMI GraphQL fetch; interval reads only.
        Interval reads are always fetched for electric meters regardless of mode.
        """
        premise_number = str(billing_account.get("premiseNumber", ""))
        for meter in meter_nodes:
            if not meter.get("hasAmiSmartMeter"):
                continue
            sp = str(meter.get("servicePointNumber", ""))
            if not sp:
                continue

            # Skip AMI fetch in interval-only mode
            # (AMI data only updates once daily around midnight)
            if not self._interval_only_mode:
                await self._fetch_ami_graphql_data(
                    meter,
                    premise_number,
                    sp,
                    today,
                    ami_usages,
                    is_first_refresh=is_first_refresh,
                )

            # Always fetch interval reads for electric meters (fast REST endpoint)
            await self._fetch_interval_reads(meter, premise_number, sp, interval_reads)

    async def _fetch_interval_reads(
        self,
        meter: Meter,
        premise_number: str,
        sp: str,
        interval_reads: dict[str, list[IntervalRead]],
    ) -> None:
        """Fetch interval reads for a single electric meter.

        The AMIAdapter REST API provides near-real-time 15-minute data.
        Gas meters are skipped (the endpoint returns 404 for them).
        Fetches from yesterday midnight UTC so interval reads pick up
        seamlessly where hourly AMI data leaves off.
        """
        # Interval reads are for electric meters only.
        fuel_type = str(meter.get("fuelType", ""))
        if fuel_type == "Gas":
            return

        try:
            now = datetime.now(tz=UTC)
            # Start from yesterday midnight UTC — interval covers yesterday through now,
            # picking up where verified AMI data leaves off.
            yesterday_midnight = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            reads = await self.api.get_interval_reads(
                premise_number=premise_number,
                service_point_number=sp,
                start_datetime=yesterday_midnight,
            )
            interval_reads[sp] = reads

            if reads:
                times = [r.get("startTime") for r in reads if r.get("startTime")]
                if times:
                    min_t = min(times)
                    max_t = max(times)
                    _LOGGER.debug(
                        "Fetched %s interval reads for meter %s (range: %s to %s)",
                        len(reads),
                        sp,
                        min_t[:_DATETIME_LOG_LEN]
                        if len(min_t) > _DATETIME_LOG_LEN
                        else min_t,
                        max_t[:_DATETIME_LOG_LEN]
                        if len(max_t) > _DATETIME_LOG_LEN
                        else max_t,
                    )
                else:
                    _LOGGER.debug(
                        "Fetched %s interval reads for meter %s", len(reads), sp
                    )
            else:
                _LOGGER.debug("No interval reads returned for meter %s", sp)
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
        ) as err:
            _LOGGER.debug("Could not fetch interval reads for meter %s: %s", sp, err)

    async def _fetch_ami_graphql_data(  # noqa: PLR0913
        self,
        meter: Meter,
        premise_number: str,
        sp: str,
        today: date,
        ami_usages: dict[str, list[AmiEnergyUsage]],
        *,
        is_first_refresh: bool = False,
    ) -> None:
        """Fetch AMI data for a single meter using a two-pass strategy.

        Pass 1 — bulk history via get_ami_energy_usages() (daily/hourly endpoint):
          First refresh: epoch → today-3d
          Incremental:   today-7d → today-3d
          Falls back to 50-day get_ami_energy_usages_15min() if the primary fails
          entirely on first refresh.

        Pass 2 — recent 72 h via get_ami_energy_usages_15min():
          Always fetches today-3d → today at 15-min granularity so the most
          recent data is at full resolution. Errors are logged and skipped.

        Both result lists are concatenated so statistics.py sees the complete
        range; it buckets to top-of-hour regardless of source granularity.
        """
        meter_kwargs = {
            "meter_number": str(meter.get("meterNumber", "")),
            "premise_number": premise_number,
            "service_point_number": sp,
            "meter_point_number": str(meter.get("meterPointNumber", "")),
            "fuel_type": str(meter.get("fuelType", "")),
        }

        cutoff = today - timedelta(days=3)  # 72-hour boundary

        if is_first_refresh:
            date_from = date(1970, 1, 1)
            _LOGGER.info(
                "First refresh: fetching AMI epoch→%s (hourly) + %s→%s (15-min)"
                " for meter %s",
                cutoff,
                cutoff,
                today,
                sp,
            )
        else:
            date_from = today - timedelta(days=7)
            _LOGGER.debug(
                "Incremental: fetching AMI %s→%s (hourly) + %s→%s (15-min)"
                " for meter %s",
                date_from,
                cutoff,
                cutoff,
                today,
                sp,
            )

        # Pass 1: bulk history (hourly records)
        bulk_data: list[AmiEnergyUsage] = []
        try:
            bulk_data = await self.api.get_ami_energy_usages(
                date_from=date_from,
                date_to=cutoff,
                **meter_kwargs,  # type: ignore[arg-type]
            )
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
        ) as err:
            if is_first_refresh:
                # Primary method failed; retry with explicit 15-min, 50-day window.
                _LOGGER.warning(
                    "Primary AMI fetch failed for meter %s: %s"
                    " — retrying with 15-min, 50-day window",
                    sp,
                    err,
                )
                try:
                    bulk_data = await self.api.get_ami_energy_usages_15min(
                        date_from=today - timedelta(days=50),
                        date_to=cutoff,
                        **meter_kwargs,  # type: ignore[arg-type]
                    )
                except (
                    CannotConnectError,
                    RetryExhaustedError,
                    NationalGridError,
                ) as err2:
                    _LOGGER.debug(
                        "Could not fetch bulk AMI for meter %s"
                        " (both methods failed): %s",
                        sp,
                        err2,
                    )
            else:
                _LOGGER.debug("Could not fetch bulk AMI for meter %s: %s", sp, err)

        # Pass 2: recent 72 h at 15-min resolution
        recent_data: list[AmiEnergyUsage] = []
        try:
            recent_data = await self.api.get_ami_energy_usages_15min(
                date_from=cutoff,
                date_to=today,
                **meter_kwargs,  # type: ignore[arg-type]
            )
        except (
            CannotConnectError,
            RetryExhaustedError,
            NationalGridError,
        ) as err:
            _LOGGER.debug("Could not fetch recent 15-min AMI for meter %s: %s", sp, err)

        combined = bulk_data + recent_data
        if combined:
            ami_usages[sp] = combined
            self._log_ami_results(combined, sp)

    @staticmethod
    def _log_ami_results(ami_data: list[AmiEnergyUsage], sp: str) -> None:
        """Log AMI data results with date range info."""
        if ami_data:
            dates = [r.get("date") for r in ami_data if r.get("date")]
            if dates:
                _LOGGER.info(
                    "Fetched %s AMI records for meter %s (date range: %s to %s)",
                    len(ami_data),
                    sp,
                    min(dates),
                    max(dates),
                )
            else:
                _LOGGER.debug(
                    "Fetched %s AMI records for meter %s",
                    len(ami_data),
                    sp,
                )
        else:
            _LOGGER.debug(
                "No AMI records returned for meter %s",
                sp,
            )

    def get_meter_data(self, service_point_number: str) -> MeterData | None:
        """Get meter data by service point number."""
        if self.data is None:
            return None
        return self.data.meters.get(service_point_number)

    def get_current_bill(self, account_id: str) -> Bill | None:
        """Return the most recent bill for an account (bills are newest-first)."""
        if self.data is None:
            return None
        bills = self.data.bills.get(account_id, [])
        return bills[0] if bills else None

    def get_next_reading_date(self, account_id: str) -> str | None:
        """Get the next scheduled reading date for an account."""
        if self.data is None:
            return None
        return self.data.reading_dates.get(account_id)

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
        await self._fetch_ami_graphql_data(
            meter=meter_data.meter,
            premise_number=premise_number,
            sp=service_point,
            today=today,
            ami_usages=self.data.ami_usages,
            is_first_refresh=True,
        )

        # Import stats for just this meter (deferred import avoids circular import
        # at module level; statistics.py imports coordinator only under TYPE_CHECKING)
        from .statistics import async_import_meter_statistics  # noqa: PLC0415

        await async_import_meter_statistics(
            self.hass, self, service_point, force_import_all=True
        )
