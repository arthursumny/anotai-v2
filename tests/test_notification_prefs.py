from datetime import datetime, timezone

import pytest

from app.services.notification_prefs import should_notify


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_sao_paulo_day_and_night_quiet_hours() -> None:
    # 09:00 and 22:00 in Sao Paulo (UTC-3).
    assert should_notify(
        _utc(2024, 1, 15, 12),
        "America/Sao_Paulo",
        notifications_enabled=True,
        quiet_start_minute=22 * 60,
        quiet_end_minute=7 * 60,
    )
    assert not should_notify(
        _utc(2024, 1, 15, 1),
        "America/Sao_Paulo",
        notifications_enabled=True,
        quiet_start_minute=22 * 60,
        quiet_end_minute=7 * 60,
    )


def test_cross_midnight_quiet_window() -> None:
    preferences = {
        "notifications_enabled": True,
        "quiet_start_minute": 22 * 60,
        "quiet_end_minute": 7 * 60,
    }
    assert not should_notify(_utc(2024, 1, 15, 2), "America/Sao_Paulo", **preferences)
    assert should_notify(_utc(2024, 1, 15, 10), "America/Sao_Paulo", **preferences)


def test_dst_uses_local_wall_clock_time() -> None:
    # New York is UTC-4 after the spring transition: 03:30 local is quiet.
    assert not should_notify(
        _utc(2024, 3, 10, 7, 30),
        "America/New_York",
        notifications_enabled=True,
        quiet_start_minute=2 * 60,
        quiet_end_minute=4 * 60,
    )
    # Before the fall transition New York is UTC-4: 01:30 local is quiet.
    assert not should_notify(
        _utc(2024, 11, 3, 5, 30),
        "America/New_York",
        notifications_enabled=True,
        quiet_start_minute=60,
        quiet_end_minute=120,
    )


def test_quiet_start_is_inclusive_and_end_is_exclusive() -> None:
    options = {
        "timezone_name": "America/Sao_Paulo",
        "notifications_enabled": True,
        "quiet_start_minute": 22 * 60,
        "quiet_end_minute": 7 * 60,
    }
    assert not should_notify(_utc(2024, 1, 15, 1), **options)  # 22:00
    assert should_notify(_utc(2024, 1, 15, 10), **options)  # 07:00


def test_disabled_notifications_always_return_false() -> None:
    assert not should_notify(
        _utc(2024, 1, 15, 12),
        "UTC",
        notifications_enabled=False,
        quiet_start_minute=None,
        quiet_end_minute=None,
    )


@pytest.mark.parametrize("timezone_name", ["Not/A_Real_Zone", ""])
def test_invalid_timezone_is_rejected(timezone_name: str) -> None:
    with pytest.raises(ValueError, match="timezone"):
        should_notify(
            _utc(2024, 1, 15, 12),
            timezone_name,
            notifications_enabled=True,
            quiet_start_minute=None,
            quiet_end_minute=None,
        )


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        should_notify(
            datetime(2024, 1, 15, 12),
            "UTC",
            notifications_enabled=True,
            quiet_start_minute=None,
            quiet_end_minute=None,
        )


def test_incomplete_quiet_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="both"):
        should_notify(
            _utc(2024, 1, 15, 12),
            "UTC",
            notifications_enabled=True,
            quiet_start_minute=60,
            quiet_end_minute=None,
        )


@pytest.mark.parametrize("minute", [-1, 1440, True, 1.5])
def test_invalid_quiet_minute_is_rejected(minute: int) -> None:
    with pytest.raises(ValueError, match="integer"):
        should_notify(
            _utc(2024, 1, 15, 12),
            "UTC",
            notifications_enabled=True,
            quiet_start_minute=minute,
            quiet_end_minute=120,
        )


def test_equal_quiet_endpoints_mean_no_quiet_time() -> None:
    assert should_notify(
        _utc(2024, 1, 15, 12),
        "UTC",
        notifications_enabled=True,
        quiet_start_minute=600,
        quiet_end_minute=600,
    )
