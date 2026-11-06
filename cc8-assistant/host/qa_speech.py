"""Public fixed spoken latency stimuli and exact, formatting-only comparison."""

import hashlib
import re
import unicodedata

WAKE = '大黄蜂'
QUESTIONS = tuple(f'{left}加{right}等于几？' for left in '一二三四五六' for right in '一二三四五')
WEB_TOPICS = (
    '中国气象局',
    '国家图书馆',
    '中国国家博物馆',
    '中国科技馆',
    '故宫博物院',
    '上海博物馆',
    '南京博物院',
    '苏州博物馆',
    '浙江省博物馆',
    '湖南博物院',
    '广东省博物馆',
    '四川博物院',
    '陕西历史博物馆',
    '河南博物院',
    '山东博物馆',
    '湖北省博物馆',
    '云南省博物馆',
    '贵州省博物馆',
    '山西博物院',
    '甘肃省博物馆',
    '北京天文馆',
    '上海天文馆',
    '广东科学中心',
    '浙江省科技馆',
    '重庆科技馆',
    '上海图书馆',
    '南京图书馆',
    '浙江图书馆',
    '成都图书馆',
    '深圳图书馆',
)
TOOL_PREFIX = '请用本机只读终端命令计算'
TOOL_ANSWER_LABEL = re.compile(r'^本机(?:只读终端(?:命令)?|终端)计算结果:\s*')
MATH_SCENARIOS = ('qa', 'tool')
TOOL_COMPARE_ENGINES = ('whisper', 'sensevoice')
SCENARIO_QUESTIONS = {
    'qa': QUESTIONS,
    'web': tuple(f'联网查找{topic}的官方网站。' for topic in WEB_TOPICS),
    'tool': tuple(TOOL_PREFIX + question for question in QUESTIONS),
}


def tool_sample_request(value):
    if not isinstance(value, dict):
        return False
    operation = value.get('operation')
    if operation in ('tool_sample_arm', 'tool_sample_compare_arm'):
        return (
            (
                set(value) == {'operation', 'case'}
                or (
                    operation == 'tool_sample_compare_arm'
                    and set(value) == {'operation', 'case', 'engine'}
                    and value['engine'] in TOOL_COMPARE_ENGINES
                )
            )
            and type(value['case']) is int
            and 1 <= value['case'] <= len(SCENARIO_QUESTIONS['tool'])
        )
    return (
        operation in ('tool_sample_begin', 'tool_sample_release')
        and set(value) == {'operation', 'token'}
        and isinstance(value['token'], str)
        and re.fullmatch('[a-f0-9]{32}', value['token']) is not None
    )


ALL_QUESTIONS = tuple(question for group in SCENARIO_QUESTIONS.values() for question in group)
SCENARIO_DIGESTS = {
    scenario: hashlib.sha256('\n'.join(questions).encode()).hexdigest()
    for scenario, questions in SCENARIO_QUESTIONS.items()
}
CORPUS_SHA256 = SCENARIO_DIGESTS['qa']
DIGITS = str.maketrans('一二三四五六', '123456')
ANSWER_WORDS = {
    **{word: number for number, word in enumerate('零一二三四五六七八九十')},
    '〇': 0,
    '两': 2,
    '十一': 11,
}
ANSWER_NUMBER = r'(?:[0-9]{1,2}|十一|[零〇一二两三四五六七八九十])'
ANSWER_TOKEN = rf'(\*\*{ANSWER_NUMBER}\*\*|`{ANSWER_NUMBER}`|{ANSWER_NUMBER})'
ANSWER_VALUE = re.compile(rf'(?:(?:答案|结果)(?:是|:)|是|等于)?\s*{ANSWER_TOKEN}')
ANSWER_EQUATION = re.compile(
    rf'{ANSWER_TOKEN}\s*(?:加|\+)\s*{ANSWER_TOKEN}\s*(?:等于|=)\s*{ANSWER_TOKEN}'
)
TOOL_QUESTION = re.compile(rf'({ANSWER_NUMBER})加({ANSWER_NUMBER})等于(?:几|多少|什么)')
FIXED_ADDITION = re.compile(r'([1-6])加([1-5])等于几')
TOOL_FORMATTING = str.maketrans('', '', ':"“”「」『』')


def normalize_question(text):
    text = ''.join(unicodedata.normalize('NFKC', text).split()).rstrip('。?!.,，')
    if text.startswith(WAKE):
        text = text[len(WAKE) :].lstrip(',，:：')
    return text.replace(',', '').translate(DIGITS).replace('+', '加').replace('=', '等于')


def tool_transcript_rejection(text, expected):
    """Classify a fixed-tool mismatch without broadening admission or exposing text."""
    actual = normalize_question(text)
    if actual == expected:
        return None
    if actual.translate(TOOL_FORMATTING) == expected:
        return 'formatting_mismatch'
    if '只读' not in actual:
        return 'read_only_missing'
    if not actual.startswith(TOOL_PREFIX):
        return 'tool_instruction_mismatch'
    parsed = TOOL_QUESTION.fullmatch(actual.removeprefix(TOOL_PREFIX))
    target = FIXED_ADDITION.fullmatch(expected.removeprefix(TOOL_PREFIX))
    if parsed is None or target is None:
        return 'transcript_mismatch'
    operands = tuple(
        int(value) if value.isascii() else ANSWER_WORDS[value] for value in parsed.groups()
    )
    return (
        'arithmetic_form_mismatch'
        if operands == tuple(map(int, target.groups()))
        else 'arithmetic_mismatch'
    )


def tool_asr_result(text, expected):
    if re.search(r'\w', text) is None:
        return 'empty'
    return tool_transcript_rejection(text, expected) or 'matched'


def math_answer_status(question, result):
    """Classify only unambiguous short answers to the fixed public additions."""
    operands = FIXED_ADDITION.fullmatch(normalize_question(question.removeprefix(TOOL_PREFIX)))
    if operands is None or not isinstance(result, str) or not 0 < len(result) <= 1600:
        return 'unconfirmed'

    def number(token):
        token = token.strip('*`')
        return int(token) if token.isascii() else ANSWER_WORDS[token]

    left, right = map(int, operands.groups())
    answer = unicodedata.normalize('NFKC', result).strip().rstrip('。.!').rstrip()
    if question.startswith(TOOL_PREFIX):
        answer = TOOL_ANSWER_LABEL.sub('', answer, count=1)
    value = ANSWER_VALUE.fullmatch(answer)
    if value is not None:
        return 'matched' if number(value[1]) == left + right else 'mismatch'
    equation = ANSWER_EQUATION.fullmatch(answer)
    if equation is None:
        return 'unconfirmed'
    first, second, total = map(number, equation.groups())
    if (first, second) in ((left, right), (right, left)) and total == left + right:
        return 'matched'
    return 'mismatch'
