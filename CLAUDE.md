# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a Home Assistant custom integration for National Grid, based on the `ludeeus/integration_blueprint` template. It uses the [`py-nationalgrid`](https://github.com/virtitnerd/py-nationalgrid) Python library to access data from National Grid. It uses HACS for distribution and requires Home Assistant 2025.2.4+.

## Development Commands

```bash
# Install dependencies (run first time or after requirements.txt changes)
scripts/setup

# Start Home Assistant with the integration loaded (creates config/ dir if needed)
scripts/develop

# Format and lint code (uses ruff)
scripts/lint
```

The devcontainer is configured for VS Code with Python 3.13 and exposes Home Assistant on port 8123.

## Architecture

The integration follows the standard Home Assistant custom component pattern:

- **`__init__.py`**: Entry setup with `async_setup_entry`/`async_unload_entry`. Configures the coordinator with 1-hour update interval and forwards to platforms (sensor, binary_sensor).

- **`coordinator.py`**: `NationalGridDataUpdateCoordinator` extends Home Assistant's `DataUpdateCoordinator`. Uses `py_nationalgrid.NationalGridClient` directly (no intermediate wrapper). Contains `MeterData` and `NationalGridCoordinatorData` dataclasses. Fetches billing, usage, cost, and 15-min AMI data per meter via `get_ami_energy_usages_15min()`. Catches `py_nationalgrid` exceptions and translates to HA-specific ones (`ConfigEntryAuthFailed`, `UpdateFailed`).

- **`config_flow.py`**: `NationalGridFlowHandler` implements UI configuration. Collects username/password, then presents account selection step. Supports reauthentication flow.

- **`entity.py`**: `NationalGridEntity` base class extends `CoordinatorEntity`. Sets up device info and unique_id from config entry.

- **`data.py`**: `NationalGridConfigEntry` type alias for typed config entries. `entry.runtime_data` is the coordinator directly.

- **`const.py`**: Domain, logger, attribution, `CONF_SELECTED_ACCOUNTS`, and unit constants (`UNIT_KWH`, `UNIT_CCF`).

- **`statistics.py`**: Imports long-term statistics into Home Assistant's recorder. `async_import_all_statistics` processes 15-min AMI data for each meter. Creates `national_grid:{sp}_electric_hourly_usage` and `national_grid:{sp}_gas_hourly_usage` stat series. Supports first-refresh (full import), midnight-refresh (5-day window with backfill continuity), and incremental modes.

- **Platform files** (`sensor.py`, `binary_sensor.py`): Each defines entity descriptions and entity classes inheriting from `NationalGridEntity`.

## Key Patterns

- All entities inherit from `NationalGridEntity` which handles coordinator binding and device registration
- Runtime data stored in `entry.runtime_data` as the coordinator directly (no wrapper dataclass)
- Coordinator uses `py_nationalgrid.NationalGridClient` directly with an HA-managed session
- Uses `CoordinatorEntity` pattern for automatic state updates
- AMI data comes from a single `get_ami_energy_usages_15min()` call per meter (auto-chunks, graceful 504 truncation at ~45-day window, works for both Electric and Gas)
- `fuelType` on `Meter` objects is title-case from the API (`"Electric"` / `"Gas"`); cost records use uppercase (`"ELECTRIC"` / `"GAS"`)

## Documentation Reference

- When looking up Home Assistant developer documentation, use Context7 with the library ID `/home-assistant/developers.home-assistant`.

## Code Style

- Uses ruff for formatting and linting
- Uses black-compatible formatting (via ruff)
- Type hints throughout with `TYPE_CHECKING` imports for circular dependency prevention
