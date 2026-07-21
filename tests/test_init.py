"""Tests for the National Grid integration init."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from py_nationalgrid.exceptions import InvalidAuthError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.national_grid_us.const import CONF_SELECTED_ACCOUNTS, DOMAIN

from .conftest import (
    MOCK_ACCOUNT_ID,
    MOCK_PASSWORD,
    MOCK_USERNAME,
    _mock_account_links,
    _mock_ami_usages,
    _mock_billing_account,
    _mock_bills,
    _mock_costs,
    _mock_interval_reads,
    _mock_usages,
)

PATCH_STATISTICS = "custom_components.national_grid_us.async_import_all_statistics"

PATCH_CLIENT = "custom_components.national_grid_us.coordinator.NationalGridClient"
PATCH_SESSION = (
    "custom_components.national_grid_us.coordinator.async_create_clientsession"
)
PATCH_TRACK_TIME = "custom_components.national_grid_us.async_track_time_change"


@pytest.fixture
def config_entry(hass: HomeAssistant):
    """Create and add a config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_USERNAME,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_SELECTED_ACCOUNTS: [MOCK_ACCOUNT_ID],
        },
    )
    entry.add_to_hass(hass)
    return entry


def _make_api_mock() -> AsyncMock:
    """Create a mock aionatgrid client."""
    api = AsyncMock()
    api.get_billing_account = AsyncMock(return_value=_mock_billing_account())
    api.get_energy_usages = AsyncMock(return_value=_mock_usages())
    api.get_energy_usage_costs = AsyncMock(return_value=_mock_costs())
    api.get_ami_energy_usages = AsyncMock(return_value=_mock_ami_usages())
    api.get_ami_energy_usages_15min = AsyncMock(return_value=_mock_ami_usages())
    api.get_interval_reads = AsyncMock(return_value=_mock_interval_reads())
    api.get_linked_accounts = AsyncMock(return_value=_mock_account_links())
    api.get_bills = AsyncMock(return_value=_mock_bills())
    return api


async def test_setup_entry(hass: HomeAssistant, config_entry) -> None:
    """Test successful setup of a config entry."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(
            "custom_components.national_grid_us.async_import_all_statistics",
            new_callable=AsyncMock,
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data is not None


async def test_unload_entry(hass: HomeAssistant, config_entry) -> None:
    """Test unloading a config entry."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(
            "custom_components.national_grid_us.async_import_all_statistics",
            new_callable=AsyncMock,
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED

        await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_auth_error(hass: HomeAssistant, config_entry) -> None:
    """Test setup with auth error triggers reauth."""
    api = _make_api_mock()
    api.get_billing_account = AsyncMock(
        side_effect=InvalidAuthError("Bad creds"),
    )

    with (
        patch(PATCH_CLIENT, return_value=api),
        patch(PATCH_SESSION),
        patch(
            "custom_components.national_grid_us.async_import_all_statistics",
            new_callable=AsyncMock,
        ),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_service_registered_after_setup(
    hass: HomeAssistant, config_entry
) -> None:
    """Test force_full_refresh service is registered after entry setup."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    from custom_components.national_grid_us.const import DOMAIN

    assert hass.services.has_service(DOMAIN, "force_full_refresh")


async def test_force_refresh_service_triggers_coordinator(
    hass: HomeAssistant, config_entry
) -> None:
    """Test calling force_full_refresh service resets and refreshes the coordinator."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock) as mock_stats,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_stats.reset_mock()

        from custom_components.national_grid_us.const import DOMAIN

        coordinator = config_entry.runtime_data
        assert coordinator is not None

        await hass.services.async_call(
            DOMAIN,
            "force_full_refresh",
            {},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Statistics import should have been called again after the service call
    assert mock_stats.called


async def test_force_refresh_service_with_specific_entry_id(
    hass: HomeAssistant, config_entry
) -> None:
    """Test force_full_refresh service with a specific entry_id."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock) as mock_stats,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_stats.reset_mock()

        from custom_components.national_grid_us.const import DOMAIN

        await hass.services.async_call(
            DOMAIN,
            "force_full_refresh",
            {"entry_id": config_entry.entry_id},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert mock_stats.called


async def test_scheduled_refresh_midnight(hass: HomeAssistant, config_entry) -> None:
    """Test _scheduled_refresh triggers full refresh at hour=0 (midnight)."""
    captured_cb = None

    def _capture(_hass, callback, **_kw):
        nonlocal captured_cb
        captured_cb = callback
        return lambda: None

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(PATCH_TRACK_TIME, side_effect=_capture),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert captured_cb is not None
    coordinator = config_entry.runtime_data
    coordinator.async_refresh_full_with_clear = AsyncMock()
    coordinator.async_refresh_interval_only = AsyncMock()

    captured_cb(datetime(2026, 1, 1, 0, 18, 0, tzinfo=UTC))
    await hass.async_block_till_done()

    coordinator.async_refresh_full_with_clear.assert_called_once()


async def test_scheduled_refresh_pending_retry(
    hass: HomeAssistant, config_entry
) -> None:
    """Test _scheduled_refresh retries full refresh when pending_full_refresh is set."""
    captured_cb = None

    def _capture(_hass, callback, **_kw):
        nonlocal captured_cb
        captured_cb = callback
        return lambda: None

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(PATCH_TRACK_TIME, side_effect=_capture),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert captured_cb is not None
    coordinator = config_entry.runtime_data
    coordinator.async_refresh_full_with_clear = AsyncMock()
    coordinator.async_refresh_interval_only = AsyncMock()

    # Simulate a pending full refresh flag
    coordinator._pending_full_refresh = True

    captured_cb(datetime(2026, 1, 1, 12, 18, 0, tzinfo=UTC))
    await hass.async_block_till_done()

    coordinator.async_refresh_full_with_clear.assert_called_once()


async def test_scheduled_refresh_interval_only(
    hass: HomeAssistant, config_entry
) -> None:
    """Test _scheduled_refresh does interval-only refresh at non-midnight."""
    captured_cb = None

    def _capture(_hass, callback, **_kw):
        nonlocal captured_cb
        captured_cb = callback
        return lambda: None

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(PATCH_TRACK_TIME, side_effect=_capture),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert captured_cb is not None
    coordinator = config_entry.runtime_data
    coordinator.async_refresh_full_with_clear = AsyncMock()
    coordinator.async_refresh_interval_only = AsyncMock()

    coordinator._pending_full_refresh = False

    captured_cb(datetime(2026, 1, 1, 14, 18, 0, tzinfo=UTC))
    await hass.async_block_till_done()

    coordinator.async_refresh_interval_only.assert_called_once()


async def test_async_reload_entry(hass: HomeAssistant, config_entry) -> None:
    """Test async_reload_entry calls hass.config_entries.async_reload."""
    from custom_components.national_grid_us import async_reload_entry

    with patch.object(
        hass.config_entries,
        "async_reload",
        new_callable=AsyncMock,
    ) as mock_reload:
        await async_reload_entry(hass, config_entry)

    mock_reload.assert_called_once_with(config_entry.entry_id)


async def test_force_refresh_service_unknown_entry_id(
    hass: HomeAssistant, config_entry
) -> None:
    """Test force_full_refresh service with an unknown entry_id is a no-op."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock) as mock_stats,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_stats.reset_mock()

        from custom_components.national_grid_us.const import DOMAIN

        await hass.services.async_call(
            DOMAIN,
            "force_full_refresh",
            {"entry_id": "nonexistent_entry_id"},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Stats should not be called again since entry_id was invalid
    assert not mock_stats.called


async def test_force_refresh_service_no_entries(
    hass: HomeAssistant, config_entry
) -> None:
    """Test force_full_refresh service is a no-op when no entries are found."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock) as mock_stats,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        mock_stats.reset_mock()

        from custom_components.national_grid_us.const import DOMAIN

        with patch.object(hass.config_entries, "async_entries", return_value=[]):
            await hass.services.async_call(
                DOMAIN,
                "force_full_refresh",
                {},
                blocking=True,
            )
            await hass.async_block_till_done()

    assert not mock_stats.called


async def test_unload_removes_service_when_last_entry(
    hass: HomeAssistant, config_entry
) -> None:
    """Test service is removed when the last config entry is unloaded."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.services.has_service(DOMAIN, "force_full_refresh")

    await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, "force_full_refresh")


async def test_async_migrate_entry_v1_to_v2(hass: HomeAssistant) -> None:
    """Test migration from version 1 to version 2 bumps the entry version."""
    from custom_components.national_grid_us import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={},
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert entry.version == 2


async def test_async_migrate_entry_unknown_version(hass: HomeAssistant) -> None:
    """Test migration returns False for unknown config entry version."""
    from custom_components.national_grid_us import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=99,
        data={},
    )
    entry.add_to_hass(hass)

    result = await async_migrate_entry(hass, entry)

    assert result is False


async def test_setup_entry_db_error(hass: HomeAssistant) -> None:
    """Test setup completes gracefully when the statistics DB rename fails."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_SELECTED_ACCOUNTS: [MOCK_ACCOUNT_ID],
        },
    )
    entry.add_to_hass(hass)

    mock_instance = MagicMock()
    mock_instance.async_add_executor_job = AsyncMock(side_effect=Exception("DB error"))

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(
            "custom_components.national_grid_us.recorder_get_instance",
            return_value=mock_instance,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED


async def test_setup_entry_runs_statistics_rename(hass: HomeAssistant) -> None:
    """Test async_setup_entry runs the statistics rename for old national_grid rows."""
    # Use version=2 so async_migrate_entry is NOT triggered; only async_setup_entry
    # calls _rename, giving exactly 4 execute calls (3 DELETEs + 1 UPDATE).
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=MOCK_USERNAME,
        version=2,
        data={
            CONF_USERNAME: MOCK_USERNAME,
            CONF_PASSWORD: MOCK_PASSWORD,
            CONF_SELECTED_ACCOUNTS: [MOCK_ACCOUNT_ID],
        },
    )
    entry.add_to_hass(hass)

    mock_instance = MagicMock()
    execute_result = MagicMock()
    execute_result.rowcount = 2
    session = mock_instance.get_session.return_value.__enter__.return_value
    session.execute.return_value = execute_result
    # async_add_executor_job must actually call the function; a plain MagicMock
    # would be caught by the bare `except Exception` and silently skip _rename.
    mock_instance.async_add_executor_job = AsyncMock(
        side_effect=lambda fn, *args: fn(*args)
    )

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(
            "custom_components.national_grid_us.recorder_get_instance",
            return_value=mock_instance,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    # 3 DELETEs + 1 UPDATE = 4 execute calls
    assert session.execute.call_count == 4
    session.commit.assert_called_once()


async def test_warn_if_old_component_present(
    hass: HomeAssistant, config_entry, tmp_path
) -> None:
    """Test a persistent notification fires when the old national_grid folder exists."""
    old_dir = tmp_path / "national_grid"
    old_dir.mkdir()

    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(
            "custom_components.national_grid_us.recorder_get_instance",
            side_effect=Exception("skip"),
        ),
        patch.object(hass.config, "path", return_value=str(old_dir)),
        patch("custom_components.national_grid_us.pn_create") as mock_pn,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    mock_pn.assert_called_once()
    assert (
        mock_pn.call_args.kwargs.get("notification_id")
        == "national_grid_us_stale_folder"
    )


async def test_no_warn_without_old_component(
    hass: HomeAssistant, config_entry, tmp_path
) -> None:
    """Test that no notification is created when the old folder is absent."""
    with (
        patch(PATCH_CLIENT, return_value=_make_api_mock()),
        patch(PATCH_SESSION),
        patch(PATCH_STATISTICS, new_callable=AsyncMock),
        patch(
            "custom_components.national_grid_us.recorder_get_instance",
            side_effect=Exception("skip"),
        ),
        patch.object(hass.config, "path", return_value=str(tmp_path / "nonexistent")),
        patch("custom_components.national_grid_us.pn_create") as mock_pn,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    mock_pn.assert_not_called()
