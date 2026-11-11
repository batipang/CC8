"""Beijing schedule slots; no catch-up beyond a bounded grace, no work on activation."""

from datetime import datetime, timedelta
import math

from work_reports import BEIJING

GRACE_SECONDS = 900


def timestamp(now):
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 253402300799:
        raise ValueError('Invalid workflow clock')
    return int(now)


def due_slot(flow, now):
    now = timestamp(now)
    schedule = flow['schedule']
    if schedule is None or not schedule['enabled']:
        return None
    configured = schedule['configured_at']
    if now <= configured:
        return None
    if schedule['kind'] == 'interval':
        seconds = schedule['seconds']
        intervals = (now - configured) // seconds
        if intervals == 0:
            return None
        slot = configured + intervals * seconds
        grace = min(GRACE_SECONDS, seconds - 1)
    else:
        try:
            local = datetime.fromtimestamp(now, BEIJING)
            minute = schedule['minute_of_day']
            scheduled = local.replace(
                hour=minute // 60, minute=minute % 60, second=0, microsecond=0
            )
            if scheduled > local:
                scheduled -= timedelta(days=1)
            if scheduled.weekday() not in schedule['weekdays']:
                return None
            slot = int(scheduled.timestamp())
        except (OverflowError, OSError, ValueError):
            raise ValueError('Workflow local date unavailable') from None
        grace = GRACE_SECONDS
    return slot if configured < slot <= now and now - slot <= grace else None
