"""Diagnostics support for National Grid."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import NationalGridConfigEntry

_REDACT = {"username", "password", "accountNumber", "billingAccountId", "premiseNumber"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001
    entry: NationalGridConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    if data is None:
        return {"error": "No data loaded yet"}

    meters: dict[str, Any] = {}
    for sp, meter_data in data.meters.items():
        meter = meter_data.meter
        meters[sp] = {
            "fuel_type": meter.get("fuelType"),
            "has_ami_smart_meter": meter.get("hasAmiSmartMeter"),
            "is_smart_meter": meter.get("isSmartMeter"),
            "ami_usage_count": len(data.ami_usages.get(sp, [])),
            "interval_read_count": len(data.interval_reads.get(sp, [])),
        }

    account_summaries: list[dict[str, Any]] = []
    for account_id, billing_account in data.accounts.items():
        bill = coordinator.get_current_bill(account_id)
        account_summaries.append(
            {
                "region": billing_account.get("region"),
                "meter_count": len(billing_account.get("meter", {}).get("nodes", [])),
                "next_reading_date": data.reading_dates.get(account_id),
                "current_bill_status": bill.get("status") if bill else None,
                "usage_record_count": len(data.usages.get(account_id, [])),
                "cost_record_count": len(data.costs.get(account_id, [])),
                "bill_count": len(data.bills.get(account_id, [])),
            }
        )

    return async_redact_data(
        {
            "entry": async_redact_data(entry.data, _REDACT),
            "coordinator": {
                "is_first_refresh": data.is_first_refresh,
                "pending_full_refresh": coordinator.pending_full_refresh,
                "last_update_success": coordinator.last_update_success,
                "account_count": len(data.accounts),
            },
            "accounts": account_summaries,
            "meters": meters,
        },
        _REDACT,
    )
