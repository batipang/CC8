"""Public fixed spoken latency stimuli and exact, formatting-only comparison."""

import hashlib
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
SCENARIO_QUESTIONS = {
    'qa': QUESTIONS,
    'web': tuple(f'联网查找{topic}的官方网站。' for topic in WEB_TOPICS),
    'tool': tuple(f'请用本机只读终端命令计算{question}' for question in QUESTIONS),
}
ALL_QUESTIONS = tuple(question for group in SCENARIO_QUESTIONS.values() for question in group)
SCENARIO_DIGESTS = {
    scenario: hashlib.sha256('\n'.join(questions).encode()).hexdigest()
    for scenario, questions in SCENARIO_QUESTIONS.items()
}
CORPUS_SHA256 = SCENARIO_DIGESTS['qa']
DIGITS = str.maketrans('一二三四五六', '123456')


def normalize_question(text):
    text = ''.join(unicodedata.normalize('NFKC', text).split()).rstrip('。?!.,，')
    if text.startswith(WAKE):
        text = text[len(WAKE) :].lstrip(',，:：')
    return text.replace(',', '').translate(DIGITS).replace('+', '加').replace('=', '等于')
