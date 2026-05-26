# National Grid US Integration

[![GitHub Release](https://img.shields.io/github/v/release/virtitnerd/ha_nationalgrid?style=flat-square)](https://github.com/virtitnerd/ha_nationalgrid/releases)
[![License](https://img.shields.io/github/license/virtitnerd/ha_nationalgrid?style=flat-square)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/virtitnerd/ha_nationalgrid?style=flat-square)](https://github.com/virtitnerd/ha_nationalgrid/commits/main)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square)](https://hacs.xyz/)

A custom [Home Assistant](https://www.home-assistant.io/) integration that provides energy usage, cost, billing, and 15-minute AMI meter data from [National Grid](https://www.nationalgridus.com/) utility accounts. It uses the [py-nationalgrid](https://github.com/virtitnerd/py-nationalgrid) library to communicate with National Grid's API.

This is a fork of [ryanmorash/ha_nationalgrid](https://github.com/ryanmorash/ha_nationalgrid), updated to use the actively maintained `py-nationalgrid` library and rebuilt around the 15-minute AMI data endpoint.

## Features

- **Account & Meter Device Hierarchy**: Account devices appear as parent devices in HA; each meter device is linked via "Connected via" to its account
- **Energy Usage & Cost Sensors**: Monthly billing usage and costs for electric and gas meters
- **Current Bill Sensor**: Current billing period charges with due date, statement date, and status as attributes
- **Next Reading Date**: Diagnostic sensor showing the next scheduled meter read date per account
- **Smart Meter Detection**: Identifies meters with AMI (Advanced Metering Infrastructure) capabilities
- **15-Minute AMI Statistics**: Imports granular energy data into Home Assistant's Energy Dashboard
- **Solar / Return Support**: Separate statistics for grid consumption and energy returned to the grid
- **Historical Data Import**: On first setup, imports all available AMI data (as far back as National Grid retains for your meter)
- **Fast Restarts**: After the initial import, HA restarts skip the historical fetch and load in seconds
- **Per-Meter Force Refresh**: Button entity on each meter device to re-import its full history on demand
- **Force Refresh Service**: Manually trigger a full historical data refresh for all meters
- **Reconfigure Support**: Change your monitored account selection at any time without re-entering credentials
- **Diagnostics**: Full support for HA's "Download Diagnostics" with all sensitive data redacted

## Installation

> **Migrating from the original integration?** Uninstall `ryanmorash/ha_nationalgrid` from HACS and remove the integration entry from **Settings > Devices & Services** before installing this one. Both use the same domain (`national_grid`), so they cannot coexist.

> **Upgrading from an earlier version of this fork?** Statistics IDs now include the account ID prefix (e.g., `national_grid_us:1234567890_SP001_electric_hourly_usage`). If you had the old format (`national_grid_us:SP001_electric_hourly_usage`) configured in the Energy Dashboard, you will need to update those references after upgrading. See [Long-Term Statistics](#long-term-statistics) for the new format.

### HACS (Recommended)

1. Open HACS in your Home Assistant instance.
2. Go to **Integrations** and select the three-dot menu in the top right corner.
3. Select **Custom repositories**.
4. Add the URL `https://github.com/virtitnerd/ha_nationalgrid` with category **Integration**.
5. Find **National Grid** in the HACS integration list and click **Download**.
6. Restart Home Assistant.

### Manual Installation

1. Download the `custom_components/national_grid_us` folder from this repository.
2. Copy the `national_grid_us` folder into your Home Assistant `config/custom_components/` directory.
3. Restart Home Assistant.

## Configuration

Configuration is done entirely through the Home Assistant UI.

1. Go to **Settings > Devices & Services > Add Integration**.
2. Search for **National Grid**.
3. Enter your National Grid account **username** and **password**.
4. Select which billing accounts to monitor from the list of accounts linked to your login.

### Reconfiguring Account Selection

To change which accounts are monitored after initial setup:

1. Go to **Settings > Devices & Services**.
2. Find the **National Grid** integration entry.
3. Click the three-dot menu and select **Reconfigure**.
4. Select or deselect accounts as needed.

### Configuration Parameters

| Parameter         | Description                                              |
| ----------------- | -------------------------------------------------------- |
| Username          | Your National Grid online account email or username      |
| Password          | Your National Grid online account password               |
| Selected Accounts | Which linked billing accounts to monitor                 |

## Removal

1. Go to **Settings > Devices & Services**.
2. Find the **National Grid** integration entry.
3. Click the three-dot menu and select **Delete**.
4. Optionally, remove the `custom_components/national_grid_us` folder and restart Home Assistant.

## Devices & Entities

The integration uses a two-level device hierarchy:

```
National Grid {account_id}          ← Account device (one per billing account)
  ├── sensor: Current Bill
  ├── sensor: Next Reading Date
  └── Electric Meter {account_id}-{sp}   ← Meter device (one per service point)
        ├── sensor: Last Billing Usage
        ├── sensor: Last Billing Cost
        ├── sensor: Avg Cost per Unit
        ├── binary_sensor: Smart Meter
        └── button: Force Refresh
```

### Account Device Sensors

These sensors live on the account device and reflect account-level data.

| Entity            | Description                                            | Unit | Device Class | Category   |
| ----------------- | ------------------------------------------------------ | ---- | ------------ | ---------- |
| Current Bill      | Current billing period charges                         | USD  | Monetary     | —          |
| Next Reading Date | Next scheduled meter read date for this account        | —    | Date         | Diagnostic |

**Current Bill attributes:**

| Attribute        | Description                                    |
| ---------------- | ---------------------------------------------- |
| `due_date`       | Payment due date                               |
| `statement_date` | Date the bill was issued                       |
| `status`         | Bill status (e.g., `PAID`, `UNPAID`)           |
| `total_due`      | Total amount due including any prior balance   |

### Meter Device Sensors

| Entity             | Description                                                          | Unit                       | Device Class |
| ------------------ | -------------------------------------------------------------------- | -------------------------- | ------------ |
| Last Billing Usage | Most recent monthly billing usage                                    | kWh (electric) / CCF (gas) | Energy / Gas |
| Last Billing Cost  | Most recent monthly billing cost                                     | USD                        | Monetary     |
| Avg Cost per Unit  | Blended rate over the last 3 billing cycles (total cost ÷ total usage) | USD/kWh (electric) / USD/CCF (gas) | Monetary |

### Binary Sensors

| Entity      | Description                             | Category   |
| ----------- | --------------------------------------- | ---------- |
| Smart Meter | Whether the meter is an AMI smart meter | Diagnostic |

### Buttons

| Entity        | Description                                              | Category   |
| ------------- | -------------------------------------------------------- | ---------- |
| Force Refresh | Re-import full AMI history for this meter on demand      | Diagnostic |

### Device Information

Each **meter device** includes:

| Field         | Description                                                      |
| ------------- | ---------------------------------------------------------------- |
| Name          | Fuel type + account and service point (e.g., `Electric Meter 1234567890-SP001`) |
| Model         | Meter type (AMI Smart Meter, Smart Meter, or Standard Meter)     |
| Serial Number | Meter number                                                     |
| Connected via | The parent account device                                        |

## Data Updates

The integration refreshes data at the **18th minute of every hour**.

### First Setup

On first setup, the integration imports full historical data:

- All available 15-minute AMI data (as far back as National Grid retains for your meter)
- 15 months of billing usage data
- Current billing period cost data
- Bill history

This initial fetch takes 1–2 minutes per meter. Subsequent HA restarts skip this step and load in seconds.

### Midnight Refresh (00:18)

A full data fetch including:

- Billing account information and meter details
- Energy usage records for the last 15 months
- Energy cost records for the current billing period
- Bill history
- AMI 15-minute data for the last 7 days (catches newly available and backfilled readings)
- Next scheduled reading date per account

### Hourly Refresh (01:18 – 23:18)

A fast incremental fetch of near-real-time interval reads for electric meters:

- Interval reads from yesterday midnight UTC through now (REST endpoint, typically completes in under a second)
- Interval statistics are **cleared and reimported** on every hourly refresh so provisional data never accumulates

## Long-Term Statistics

All readings are aggregated into hourly buckets before being stored, as required by Home Assistant's recorder.

The integration maintains **two separate stat series** per electric meter:

- **Hourly AMI stats** — verified/settled data from the AMI GraphQL endpoint. Grows permanently; only new readings are appended.
- **Interval stats** — near-real-time data from the REST interval endpoint, covering yesterday midnight through now. Cleared and reimported on every refresh; bridges the gap until AMI data catches up.

Statistics IDs include the account ID and service point to ensure uniqueness across accounts.

### Electric Meters

| Statistic ID                                                          | Description                              | Window   |
| --------------------------------------------------------------------- | ---------------------------------------- | -------- |
| `national_grid_us:{account_id}_{sp}_electric_hourly_usage`               | Consumption — verified AMI data (kWh)    | All available history |
| `national_grid_us:{account_id}_{sp}_electric_return_hourly_usage`        | Return to grid / solar — verified (kWh)  | All available history |
| `national_grid_us:{account_id}_{sp}_electric_interval_usage`             | Consumption — near real-time (kWh)       | ~2 days  |
| `national_grid_us:{account_id}_{sp}_electric_interval_return_usage`      | Return to grid / solar — real-time (kWh) | ~2 days  |

### Gas Meters

| Statistic ID                                       | Description           | Window   |
| -------------------------------------------------- | --------------------- | -------- |
| `national_grid_us:{account_id}_{sp}_gas_hourly_usage` | Gas consumption (CCF) | All available history |

> **Note**: `{account_id}` is your billing account number and `{sp}` is your meter's service point number. Both can be found in the device info for your meter in Home Assistant (e.g., `national_grid_us:1234567890_SP001_electric_hourly_usage`).

### Energy Dashboard Setup

> **Finding your IDs**: Your `{account_id}` and `{sp}` (service point number) can be found in **Settings > Devices & Services > National Grid**, then click your meter device and look under **Device info**.

1. Go to **Settings > Dashboards > Energy**
2. Under **Electricity grid**, click **Add consumption** and search for your stat ID — add each one separately:
   - `national_grid_us:{account_id}_{sp}_electric_hourly_usage` — verified AMI data (history beyond 2 days)
   - `national_grid_us:{account_id}_{sp}_electric_interval_usage` — near real-time data (last ~2 days)
   - If you have solar/return-to-grid, also add `national_grid_us:{account_id}_{sp}_electric_return_hourly_usage` under **Return to grid**
3. Under **Gas consumption**, click **Add gas source** and add:
   - `national_grid_us:{account_id}_{sp}_gas_hourly_usage`

> **Why two electricity sources?** National Grid takes 1–2 days to finalize and publish verified AMI readings. The interval stat bridges that gap with near-real-time data. The integration ensures there is no overlap between them — together they give you a complete, continuous picture with no double-counting.

## Services

### `national_grid_us.force_full_refresh`

Triggers a full historical AMI re-import for all meters (or a specific integration entry). Equivalent to pressing the **Force Refresh** button on every meter device simultaneously.

**Service Data:**

| Field      | Required | Description                                                                                                          |
| ---------- | -------- | -------------------------------------------------------------------------------------------------------------------- |
| `entry_id` | No       | Config entry ID of a specific integration to refresh. If not provided, all National Grid integrations are refreshed. |

**Example automation:**

```yaml
service: national_grid_us.force_full_refresh
data: {}
```

## Troubleshooting

### Missing Historical Data

If you notice gaps in your statistics:

1. Press the **Force Refresh** button on the affected meter device (found under Diagnostic entities), or call the `national_grid_us.force_full_refresh` service.
2. Wait for completion (check logs for "Statistics import complete").
3. Note: The AMI API's data availability window depends on National Grid's retention policy.

### Slow First Startup

The first setup imports all available AMI history for each meter — how far back this goes depends on what National Grid retains for your specific meter. This is normal and typically takes 1–2 minutes per meter, but may take longer if the integration falls back to the 15-minute endpoint. All subsequent HA restarts complete in seconds because the initial import state is persisted.

### Statistics Not Showing in Energy Dashboard

If you upgraded from an earlier version, the statistics ID format changed to include the account ID prefix. Update your Energy Dashboard entries to the new format:

- Old: `national_grid_us:{sp}_electric_hourly_usage`
- New: `national_grid_us:{account_id}_{sp}_electric_hourly_usage`

### Logs

Enable debug logging for detailed information:

```yaml
logger:
  default: info
  logs:
    custom_components.national_grid_us: debug
    py_nationalgrid: debug
```
