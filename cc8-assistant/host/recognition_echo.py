"""Compare recognized speech with playback text before admitting a turn."""

import difflib
import re

from recognition_commands import STOP_COMMANDS


def resembles_playback(text, spoken):
    """Reject decoded playback fragments before creating a conversational turn.

    Explicit stop commands take priority. Short acknowledgements while the
    assistant is speaking are deliberately conservative in this prototype.
    """
    clean = lambda s: re.sub(r'[^\w\u4e00-\u9fff]', '', s).lower()
    candidate = clean(text)
    if candidate in STOP_COMMANDS:
        return False
    if len(candidate) <= 2:
        return True
    # Spoken percentages and numbers often use Chinese characters even when
    # the displayed sentence contains Arabic digits. Only use the numeric
    # variant when enough non-numeric words remain to identify the phrase.
    without_numbers = lambda value: re.sub(
        r'[0-9零〇一二三四五六七八九十百千万亿两点]', '', value.replace('百分之', '')
    )
    for line in [*spoken, ''.join(spoken)]:
        reference = clean(line)
        if not reference:
            continue
        question_markers = (
            '为什么',
            '为何',
            '怎么',
            '什么',
            '多少',
            '哪个',
            '哪些',
            '是否',
            '是不是',
            '吗',
            '呢',
        )
        novel_question = any(
            marker in candidate and marker not in reference for marker in question_markers
        )
        if novel_question:
            continue
        if candidate in reference:
            return True
        if len(candidate) >= 4:
            longest = difflib.SequenceMatcher(None, candidate, reference).find_longest_match()
            if longest.size / len(candidate) >= 0.8:
                return True
            if difflib.SequenceMatcher(None, candidate, reference).ratio() >= 0.78:
                return True
            blocks = difflib.SequenceMatcher(None, candidate, reference).get_matching_blocks()
            if (
                longest.size >= max(4, len(candidate) * 0.35)
                and sum(b.size for b in blocks) / len(candidate) >= 0.9
            ):
                return True
            # Actual CC8 echo substituted several separated Chinese words,
            # leaving no long contiguous match. Preserve new question intent
            # before using the more tolerant, ordered-coverage comparison.
            if (
                not novel_question
                and len(candidate) >= 10
                and longest.size >= 6
                and sum(b.size for b in blocks) / len(candidate) >= 0.78
            ):
                return True
            a = without_numbers(candidate)
            b = without_numbers(reference)
            if len(a) >= 6 and a in b:
                return True
    return False
