"""Pure helpers for evaluating notification preferences."""

from datetime import datetime
from zoneinfo import ZoneInfo


def should_notify(
    now: datetime,
    timezone_name: str,
    *,
    notifications_enabled: bool,
    quiet_start_minute: int | None,
    quiet_end_minute: int | None,
) -> bool:
    """Return whether a notification is allowed at ``now`` in the user's zone.

    Quiet intervals include their start and exclude their end. An interval
    whose start and end are equal is treated as no quiet time (rather than
    quiet all day).
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    try:
        timezone = ZoneInfo(timezone_name)
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(f"invalid IANA timezone: {timezone_name!r}") from exc

    if (quiet_start_minute is None) != (quiet_end_minute is None):
        raise ValueError("quiet start and end must both be set or both be None")

    if quiet_start_minute is not None and quiet_end_minute is not None:
        for name, minute in (
            ("quiet_start_minute", quiet_start_minute),
            ("quiet_end_minute", quiet_end_minute),
        ):
            if isinstance(minute, bool) or not isinstance(minute, int):
                raise ValueError(f"{name} must be an integer from 0 to 1439")
            if not 0 <= minute <= 1439:
                raise ValueError(f"{name} must be an integer from 0 to 1439")

    if not notifications_enabled:
        return False

    if quiet_start_minute is None or quiet_end_minute is None:
        return True

    # Equal endpoints deliberately mean no quiet interval.
    if quiet_start_minute == quiet_end_minute:
        return True

    local_now = now.astimezone(timezone)
    local_minute = local_now.hour * 60 + local_now.minute
    if quiet_start_minute < quiet_end_minute:
        is_quiet = quiet_start_minute <= local_minute < quiet_end_minute
    else:
        # The quiet interval crosses midnight.
        is_quiet = local_minute >= quiet_start_minute or local_minute < quiet_end_minute

    return not is_quiet
