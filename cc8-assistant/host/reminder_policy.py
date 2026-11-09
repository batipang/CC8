"""Explicit Beijing-time reminder policy; does not touch capture or providers."""

from datetime import datetime
import re
from typing import Literal, TypedDict, cast
from zoneinfo import ZoneInfo


class ReminderPolicy(TypedDict):
    revision: int
    quiet_enabled: bool
    quiet_start: str
    quiet_end: str
    late_policy: Literal['deliver', 'skip']
    max_late_minutes: int


def defaults() -> ReminderPolicy:
    return {
        'revision': 0,
        'quiet_enabled': False,
        'quiet_start': '22:00',
        'quiet_end': '07:00',
        'late_policy': 'deliver',
        'max_late_minutes': 60,
    }


def minute(value: object) -> int:
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', value):
        raise ValueError('安静时段需要 HH:MM 格式的北京时间')
    hour, minutes = value.split(':')
    return int(hour) * 60 + int(minutes)


def validate(value: object) -> ReminderPolicy:
    if not isinstance(value, dict) or set(value) != set(defaults()):
        raise ValueError('提醒送达设置字段不正确')
    if (
        type(value['revision']) is not int
        or not 0 <= value['revision'] < 2**31 - 1
        or type(value['quiet_enabled']) is not bool
        or value['late_policy'] not in ('deliver', 'skip')
        or type(value['max_late_minutes']) is not int
        or not 1 <= value['max_late_minutes'] <= 10080
        or minute(value['quiet_start']) == minute(value['quiet_end'])
    ):
        raise ValueError('提醒送达设置不正确；安静时段起止不能相同')
    return cast(ReminderPolicy, dict(value))


def quiet(policy: ReminderPolicy, now: datetime) -> bool:
    if not policy['quiet_enabled']:
        return False
    if now.tzinfo is None:
        raise ValueError('Reminder clock requires a timezone')
    now = now.astimezone(ZoneInfo('Asia/Shanghai'))
    start, end = minute(policy['quiet_start']), minute(policy['quiet_end'])
    current = now.hour * 60 + now.minute
    return start <= current < end if start < end else current >= start or current < end
