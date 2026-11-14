"""Bounded web-search data; no arbitrary URL fetching or external actions."""

import ipaddress
import json
import re
import socket
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def wants_web(text, previous=False):
    # Creative requests and personal feelings do not become searches just
    # because they contain words such as "today" or "now".
    if re.search(r'查|搜|联网|核实|验证', text):
        return True
    if re.search(r'故事|写诗|写一|编一|笑话|不开心|难过|无聊|陪我|谢谢|你好|介绍一下你自己', text):
        return False
    if re.search(
        r'天气|气温|温度|几度|下雨|新闻|热搜|汇率|股价|股票|行情|金价|油价|价格|多少钱|现任|总统|首相|政策|法规|航班|车次|路况|限行|比分|赛程|营业|开门|推荐|版本|发布|上市|利率|最新|最近|目前|现在|今日|今天|明天|今年|这周|本周|\b(?:weather|news|latest|price|current|today)\b',
        text,
        re.I,
    ):
        return True
    return previous and len(text.strip()) <= 32


def public_url(value):
    # 规范公网来源地址的字符串边界检查，不构成 DNS 级网络隔离。
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or re.search(r'[\s\x00-\x1f\x7f-\x9f\ud800-\udfff]', value)
    ):
        return None
    try:
        p = urlsplit(value)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme not in ('https', 'http') or not host or p.username or p.password:
            return None
        if p.port not in (None, 80, 443):
            return None
        if (
            host == 'localhost'
            or host.endswith(('.localhost', '.local', '.internal'))
            or '.' not in host
        ):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            # Browsers also accept short/octal/hex IPv4 spellings such as
            # 127.1 and 0177.0.0.1. Do not treat those as public DNS names.
            try:
                address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                pass
            else:
                if host != str(address) or not address.is_global:
                    return None
        return urlunsplit((p.scheme, p.netloc, p.path, p.query, ''))
    except ValueError:
        return None


def source_key(url):
    # Compare public URLs already accepted by public_url. Limit equivalence to
    # DNS host case, an explicit default port, and the empty root path.
    parts = urlsplit(url)
    host = parts.hostname
    if not host or not re.fullmatch(r'[a-z0-9.-]+', host):
        return url
    authority = parts.netloc.lower()
    port = parts.port
    if authority not in (host, f'{host}:{port}'):
        return url
    default_port = 443 if parts.scheme == 'https' else 80
    return (
        parts.scheme,
        host,
        default_port if port is None else port,
        parts.path or '/',
        parts.query,
    )


def _source_text(value, limit):
    if not isinstance(value, str) or re.search(r'[\ud800-\udfff]', value):
        return ''
    return re.sub(r'[\s\x00-\x1f\x7f-\x9f]+', ' ', value).strip()[:limit]


def normalize(data):
    items = []
    seen = set()
    rows = data.get('organic', [])
    if not isinstance(rows, list):
        return items
    for row in rows[:30]:
        if not isinstance(row, dict):
            continue
        url = public_url(row.get('link'))
        if not url or url in seen:
            continue
        title = _source_text(row.get('title'), 160)
        snippet = _source_text(row.get('snippet'), 1400)
        if not title or not snippet:
            continue
        seen.add(url)
        items.append(
            {
                'title': title,
                'url': url,
                'snippet': snippet,
                'published_at': _source_text(row.get('date'), 64),
            }
        )
        if len(items) >= 6:
            break
    return items


def parse_plan(content):
    content = content.strip()
    if content.startswith('```'):
        content = re.sub(r'^```(?:json)?\s*|\s*```$', '', content)
    result = json.loads(content)
    query = result.get('query')
    clarification = result.get('clarification')
    if not isinstance(query, str) or not isinstance(clarification, str):
        raise ValueError('Invalid query plan')
    query = query.strip()
    clarification = clarification.strip()
    if bool(query) == bool(clarification) or len(query) > 400 or len(clarification) > 120:
        raise ValueError('Invalid query plan')
    return query, clarification


class SpeechClauses:
    # 将模型增量缓存到可朗读的句段边界，避免每个 token 都触发一次 TTS。
    """Keep URLs whole across network chunks, then omit them from speech."""

    def __init__(self):
        self.buffer = ''

    @staticmethod
    def clean(value):
        value = re.sub(r'\[([^\]]+)\]\(https?://[^\s)]+\)', r'\1', value)
        value = re.sub(r'<https?://[^\s<>]+>', '', value)
        value = re.sub(r'https?://[^\s，。！？；）)>]+', '', value)
        value = re.sub(r'www\.[^\s，。！？；）)>]+', '', value)
        value = re.sub(r'\[\d+(?:[,，、-]\d+)*\]|【\d+】', '', value)
        value = re.sub(r'(?m)^\s*(?:[-•]|\d+[.、])\s*', '', value)
        return re.sub(r'[*#`]', '', value).strip()

    def feed(self, piece, final=False):
        self.buffer += piece
        out = []
        while self.buffer:
            # A URL may include ASCII ?/! or be split at an arbitrary byte
            # boundary. Never emit or split a sentence inside one.
            protected = [
                m.span()
                for m in re.finditer(r'(?:https?://|www\.)[^\s，。！？；）)>]*', self.buffer)
            ]
            boundary = None
            for m in re.finditer(r'[。！？!?；;\n]', self.buffer):
                if not any(a <= m.start() < b for a, b in protected):
                    boundary = m.end()
                    break
            if boundary is None and len(self.buffer) > 72:
                possible = [
                    m.end()
                    for m in re.finditer(r'[，,]', self.buffer[:72])
                    if m.end() >= 20 and not any(a <= m.start() < b for a, b in protected)
                ]
                if possible:
                    boundary = possible[-1]
                elif not protected and not re.search(
                    r'(?:h(?:t(?:t(?:p(?:s(?::(?:/)?)?)?)?)?)?|www?\.?)$', self.buffer
                ):
                    boundary = 64
            if boundary is None:
                if final:
                    boundary = len(self.buffer)
                else:
                    break
            value = self.clean(self.buffer[:boundary])
            self.buffer = self.buffer[boundary:]
            if value:
                out.append(value)
        return out
