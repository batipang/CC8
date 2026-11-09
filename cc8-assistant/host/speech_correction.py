"""One explicit, expiring correction draft in RAM; no fuzzy transcript rewriting."""

from dataclasses import dataclass
import re
import time
import uuid
from request_text import validate_request_text

LIMIT = 512
EXPIRES = 120


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

    def prepare(self, body, owner):
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
        explicit_pair = re.search(r'[，,]\s*不是|而不是', body)
        pair = re.fullmatch(r'(.{1,64}?)(?:[，,]\s*|而)?不是[：: ]*(.{1,64})', body)
        if pair and (explicit_pair or pair.group(2).strip() in original):
            new, old = (part.strip() for part in pair.groups())
            if not old or original.count(old) != 1 or not new:
                raise CorrectionUnavailable('旧词不能唯一定位，请重说完整的新问题；没有猜测替换。')
            index = original.index(old)
            before = original[index - 1 : index] if index else ''
            after = original[index + len(old) : index + len(old) + 1]
            numerals = (
                '0123456789０１２３４５６７８９零〇一二三四五六七八九十百千万亿两点负正+-−.．%％'
            )
            partial_number = (
                old[0] in numerals
                and before
                and before in numerals
                or old[-1] in numerals
                and after
                and after in numerals
            )
            partial_word = (
                re.match(r'[A-Za-z0-9_]', old)
                and re.fullmatch(r'[A-Za-z0-9_]', before)
                or re.search(r'[A-Za-z0-9_]$', old)
                and re.fullmatch(r'[A-Za-z0-9_]', after)
            )
            if partial_number or partial_word:
                raise CorrectionUnavailable('旧词只匹配到数字或名称的一部分，请重说完整的新问题。')
            body = original.replace(old, new, 1)
        elif explicit_pair:
            raise CorrectionUnavailable('替换关系不明确，请重说完整的新问题；没有猜测替换。')
        try:
            body = validate_request_text(body, LIMIT)
        except ValueError:
            raise CorrectionUnavailable('更正内容过长，请直接说一个较短的完整新问题。') from None
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
