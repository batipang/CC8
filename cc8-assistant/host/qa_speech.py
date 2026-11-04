"""Public fixed spoken QA stimuli and exact, formatting-only comparison."""

import hashlib
import unicodedata

WAKE = '大黄蜂'
QUESTIONS = tuple(f'{left}加{right}等于几？' for left in '一二三四五六' for right in '一二三四五')
CORPUS_SHA256 = hashlib.sha256('\n'.join(QUESTIONS).encode()).hexdigest()
DIGITS = str.maketrans('一二三四五六', '123456')


def normalize_question(text):
    text = ''.join(unicodedata.normalize('NFKC', text).split()).rstrip('。?!.,，')
    if text.startswith(WAKE):
        text = text[len(WAKE) :].lstrip(',，:：')
    return text.translate(DIGITS).replace('+', '加').replace('=', '等于')
