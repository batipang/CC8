"""One explicit, expiring correction draft in RAM; no fuzzy transcript rewriting."""

from dataclasses import dataclass
import re
import time
import uuid
import unicodedata
from request_text import validate_request_text

LIMIT = 512
EXPIRES = 120
EXPLICIT_PAIR = re.compile(r'[，,]\s*(?:而)?不是|而不是')
FULL_QUESTION = re.compile(r'^完整问题是[，,：:\s]*')

# Keep punctuation used inside amounts, along with Chinese numerals. Unicode
# decimal digits cover other scripts without normalizing the transcript itself.
NUMERALS = '零〇一二三四五六七八九十百千万亿两点负正+-−.．%％'


def number_character(char):
    return bool(char) and (char.isdecimal() or char in NUMERALS)


def name_character(char):
    return bool(char) and (
        char == '_'
        or char.isdecimal()
        or unicodedata.category(char).startswith('M')
        or unicodedata.name(char, '').startswith('LATIN ')
    )


def command(text):
    if not isinstance(text, str):
        return None
    value = re.sub(r'^(?:大黄蜂|bumblebee)[，,：:\s]*', '', text.strip(), flags=re.I)
    short = ''.join(value.split()).rstrip('。.!！?？')
    if short in ('确认更正', '撤回更正', '取消更正', '查看更正'):
        return (
            'confirm' if short == '确认更正' else 'show' if short == '查看更正' else 'cancel',
            '',
        )
    if value.startswith('我说的是'):
        return 'draft', value[4:].lstrip('，,：: ').rstrip('。.!！')
    return None


def same_owner(left, right):
    return left is not None and right is not None and left[0] is right[0] and left[1:] == right[1:]


@dataclass(frozen=True)
class Draft:
    ident: str
    original: str
    proposed: str
    owner: tuple
    expires: float


class CorrectionUnavailable(ValueError):
    pass


class SpeechCorrection:
    """Caller holds the session guard for every read, mutation and publication."""

    def __init__(self, now=time.monotonic):
        self.now = now
        self.source = None
        self.source_ident = None
        self.draft = None
        self.revision = 0

    def clear(self, preserve_source=False):
        changed = self.draft is not None
        if not preserve_source:
            self.source = None
            self.source_ident = None
        self.draft = None
        if changed:
            self.revision += 1
        return changed

    def note(self, text, owner):
        changed = self.clear()
        if owner is not None:
            try:
                text = validate_request_text(text, LIMIT)
            except ValueError:
                return changed
            self.source = (text, owner, self.now() + EXPIRES)
            self.source_ident = uuid.uuid4().hex
        return changed

    def source_token(self, owner):
        if (
            self.source is None
            or not same_owner(self.source[1], owner)
            or self.now() >= self.source[2]
        ):
            return None
        return self.source_ident

    def prepare(self, body, owner, *, allow_full_question=True):
        # Replacement is literal and must identify exactly one old occurrence.
        # The full original request remains unchanged until explicit submission.
        self.draft = None
        self.revision += 1
        if (
            self.source is None
            or not same_owner(self.source[1], owner)
            or self.now() >= self.source[2]
        ):
            self.clear()
            raise CorrectionUnavailable('本次语音会话没有可更正的问题，请直接说完整的新问题。')
        original = self.source[0]
        try:
            body = validate_request_text(body, LIMIT)
        except ValueError:
            raise CorrectionUnavailable(
                '请说完整的新问题，或说“我说的是新词，不是旧词”。'
            ) from None
        full = FULL_QUESTION.match(body) if allow_full_question else None
        if full:
            body = body[full.end() :]
        else:
            pair = replacement_pair(body, original)
            if pair is not None:
                body = replace_literal(original, *pair)
        try:
            body = validate_request_text(body, LIMIT)
        except ValueError:
            raise CorrectionUnavailable('请重说一条完整的新问题，最多 512 字。') from None
        if body == original:
            raise CorrectionUnavailable('更正内容与原问题相同，没有重复提交。')
        self.draft = Draft(uuid.uuid4().hex, original, body, owner, self.now() + EXPIRES)
        return self.draft

    def current(self, owner, ident=None):
        draft = self.draft
        if (
            draft is None
            or not same_owner(draft.owner, owner)
            or self.now() >= draft.expires
            or ident is not None
            and ident != draft.ident
        ):
            raise CorrectionUnavailable('更正草稿不存在、已过期或会话已变化，请重新提出更正。')
        return draft

    def consume(self, owner, ident):
        draft = self.current(owner, ident)
        self.clear()
        return draft.proposed

    def state(self, owner):
        try:
            draft = self.current(owner)
        except CorrectionUnavailable:
            return dict(revision=self.revision, pending=False)
        return dict(
            revision=self.revision,
            pending=True,
            draft_id=draft.ident,
            original=draft.original,
            proposed=draft.proposed,
        )


def replacement_pair(body, original):
    explicit = list(EXPLICIT_PAIR.finditer(body))
    if len(explicit) > 1:
        raise CorrectionUnavailable('一次只能更正一个词，请重说完整的新问题。')
    if explicit:
        boundary = explicit[0]
        new = body[: boundary.start()].strip()
        old = body[boundary.end() :].lstrip('：: \t').strip()
        if not 1 <= len(new) <= 64 or not 1 <= len(old) <= 64:
            raise CorrectionUnavailable('替换关系不明确，请重说完整的新问题。')
        return new, old
    markers = list(re.finditer('不是', body))
    for marker in markers:
        new = body[: marker.start()].strip()
        old = body[marker.end() :].lstrip('：: \t').strip()
        if 1 <= len(new) <= 64 and 1 <= len(old) <= 64 and old in original:
            if len(markers) > 1:
                raise CorrectionUnavailable('听到了多处“不是”，请一次更正一个词，或重说完整问题。')
            return new, old
    return None


def replace_literal(original, new, old):
    index = original.find(old) if old else -1
    if not new or index < 0 or original.find(old, index + 1) >= 0:
        raise CorrectionUnavailable('旧词不能唯一定位，请重说完整的新问题；没有猜测替换。')
    before = original[index - 1 : index] if index else ''
    after = original[index + len(old) : index + len(old) + 1]
    partial_number = (
        number_character(old[0])
        and number_character(before)
        or number_character(old[-1])
        and number_character(after)
    )
    partial_word = (
        name_character(old[0])
        and name_character(before)
        or name_character(old[-1])
        and name_character(after)
        or unicodedata.category(old[0]).startswith('M')
        or bool(after)
        and unicodedata.category(after).startswith('M')
    )
    if partial_number or partial_word:
        raise CorrectionUnavailable('旧词只匹配到数字或名称的一部分，请重说完整的新问题。')
    return original.replace(old, new, 1)
