# copyright by berlonak
# telegram: @Kilax123
"""One configured, fixed UTC offset for displaying times and quiet hours."""
from datetime import timedelta, timezone


def local_timezone(offset_hours):
    return timezone(timedelta(hours=offset_hours))


def in_quiet_hours(instant, quiet_window, offset_hours=3):
    if quiet_window is None:
        return False
    local = instant.astimezone(local_timezone(offset_hours))
    minute = local.hour * 60 + local.minute
    start, end = quiet_window.start_minute, quiet_window.end_minute
    if start < end:  # daytime window, e.g., 13:00–14:00
        return start <= minute < end
    return minute >= start or minute < end  # wraps midnight


def format_local(instant, offset_hours=3):
    hours_text = f"UTC{offset_hours:+d}"
    return instant.astimezone(local_timezone(offset_hours)).strftime("%d.%m.%Y %H:%M:%S") + f" ({hours_text})"


def as_utc(instant):
    if instant.tzinfo is None:
        return instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(timezone.utc)


def humanize_seconds(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if minutes or not parts:
        parts.append(f"{minutes} мин")
    return " ".join(parts)
