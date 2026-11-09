"""Explicit Beijing calendar ranges; ambiguous fields never reach the reader."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import re
from work_reports import BEIJING, ReportWindow

PREFIX = r'(?:请)?(?:帮我)?(?:汇报|播报|总结|查看)(?:一下)?(?:从)?'
BODY = (
    PREFIX
    + r'(?P<left>.{1,96}?)(?:到|至)(?P<right>.{1,96}?)(?:期间)?的?(?:工作成果|工作结果|完成的工作|工作|成果)(?:汇报|情况)?'
)
RANGE = re.compile(BODY)
COMPOUND = re.compile(BODY + r'[，,；;]*(?:然后|并且|并|再|接着|之后).*')
NUMBER = r'(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})'
POINT = re.compile(r'(' + NUMBER + r')(?:点|时)(半|一刻|三刻|(' + NUMBER + r')分?)?')
COLON = re.compile(r'([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?')
DAY_CHINESE = re.compile(r'^([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日')
DAY_ISO = re.compile(r'^([0-9]{4})([-/])([0-9]{1,2})\2([0-9]{1,2})')


class ReportTimeError(ValueError):
    pass


def number(text):
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if text in digits:
        return digits[text]
    if re.fullmatch(r'[一二两三四五]?十[一二三四五六七八九]?', text):
        tens, ones = text.split('十')
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    raise ReportTimeError('时刻未识别清楚，请用24小时制时刻，或明确上午、下午。')


def hour_for_period(hour, period):
    if not period:
        return hour
    if period in ('上午', '早上', '凌晨'):
        if 0 <= hour <= 11 and (period != '凌晨' or hour <= 5):
            return hour
    elif period == '中午':
        if hour == 12:
            return 12
    elif period in ('下午', '晚上', '傍晚'):
        if hour == 12 and period == '下午':
            return 12
        base = hour - 12 if 13 <= hour <= 23 else hour
        if (
            1 <= base <= 11
            and (period != '晚上' or base >= 6)
            and (period != '傍晚' or 5 <= base <= 7)
        ):
            return base + 12
    raise ReportTimeError('时段和钟点有歧义，请明确使用北京时间24小时制，例如08:00或20:00。')


def endpoint(text, today, default_day=None):
    text = text.removeprefix('北京时间')
    chosen = None
    for word, delta in (('今天', 0), ('昨天', 1), ('前天', 2)):
        if text.startswith(word):
            chosen, text = today - timedelta(days=delta), text[len(word) :]
            break
    if chosen is None:
        chinese, iso = DAY_CHINESE.match(text), DAY_ISO.match(text)
        if chinese:
            chosen = date(*map(int, chinese.groups()))
            text = text[chinese.end() :]
        elif iso:
            chosen = date(int(iso[1]), int(iso[3]), int(iso[4]))
            text = text[iso.end() :]
        else:
            chosen = default_day
    if chosen is None:
        raise ReportTimeError('请补全起始日期，例如“今天上午八点到上午十点”，或完整的年月日。')
    text = text.removeprefix('从').removeprefix('T')
    period = ''
    for word in ('上午', '下午', '中午', '晚上', '凌晨', '早上', '傍晚'):
        if text.startswith(word):
            period, text = word, text[len(word) :]
            break
    colon, point = COLON.fullmatch(text), POINT.fullmatch(text)
    if colon:
        hour, minute, second = int(colon[1]), int(colon[2]), int(colon[3] or 0)
    elif point:
        hour, second = number(point[1]), 0
        minute = {'半': 30, '一刻': 15, '三刻': 45}.get(point[2], 0)
        if point[3]:
            minute = number(point[3])
        if not period and 1 <= hour <= 12:
            raise ReportTimeError(
                '钟点没有说明上午或下午，请补全时段，或用08:00这样的24小时制时刻。'
            )
    else:
        raise ReportTimeError('起止时刻未识别清楚，请补全年月日及24小时制时刻，或明确上午、下午。')
    if not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ReportTimeError('分钟或秒数无效，请重新说明起止时刻。')
    if hour == 24 and minute == second == 0 and period in ('', '晚上'):
        return datetime.combine(chosen + timedelta(days=1), datetime.min.time(), BEIJING)
    hour = hour_for_period(hour, period)
    return datetime(chosen.year, chosen.month, chosen.day, hour, minute, second, tzinfo=BEIJING)


@dataclass(frozen=True)
class ReportRange:
    left: str
    right: str

    def resolve(self, now):
        if type(now) not in (int, float) or not 0 < now < 253402300799:
            raise ReportTimeError('当前时间不可确认，请先检查Mac系统时间。')
        try:
            today = datetime.fromtimestamp(now, BEIJING).date()
            start = endpoint(self.left, today)
            end = endpoint(self.right, today, start.date())
            if end <= start:
                raise ReportTimeError('截止时间须晚于开始时间；跨日时请明确说明截止日期。')
            if end.timestamp() > int(now):
                raise ReportTimeError('截止时间在未来，请给出已经过去的时间范围。')
            if (end - start).total_seconds() > 31 * 86400:
                raise ReportTimeError('一次最多查询31天，请缩小时间范围。')
            return ReportWindow(int(start.timestamp()), int(end.timestamp()))
        except ReportTimeError:
            raise
        except (ValueError, OverflowError, OSError):
            raise ReportTimeError('日期或时刻无效，请重新说明完整的起止日期和时刻。') from None


def parse_range(text):
    match = RANGE.fullmatch(text)
    return ReportRange(match['left'], match['right']) if match else None


def range_compound(text):
    return COMPOUND.fullmatch(text) is not None


def window_label(window):
    return (
        '北京时间'
        + datetime.fromtimestamp(window.start, BEIJING).strftime('%Y-%m-%d %H:%M:%S')
        + '至'
        + datetime.fromtimestamp(window.end, BEIJING).strftime('%Y-%m-%d %H:%M:%S')
    )
