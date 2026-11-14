"""Bounded public-page reads: pinned DNS, no credentials, scripts or persistence.

Only an owned model tool call starts a read. curl is used without user config,
proxies or automatic redirects; each redirect is resolved and checked again.
The caller receives untrusted page text, never a claim that its facts are true.
"""

from datetime import datetime
from html.parser import HTMLParser
import ipaddress
import json
import queue
import re
import selectors
import socket
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit

from web_search import now, public_url, source_key

MAX_BYTES = 1024 * 1024
MAX_HEADERS = 32 * 1024
MAX_TEXT = 20000
MAX_METADATA_BLOCKS = 4
MAX_METADATA_CHARS = 16384
SCHEMA_CONTEXTS = ('https://schema.org', 'http://schema.org')
ARTICLE_TYPES = frozenset(('Article', 'NewsArticle', 'BlogPosting'))
PUBLICATION_DATE = re.compile(
    r'[0-9]{4}-[0-9]{2}-[0-9]{2}'
    r'(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?'
    r'(?:Z|[+-][0-9]{2}:[0-5][0-9])?)?'
)
DNS_SLOTS = threading.BoundedSemaphore(2)
FAILURE_CODES = frozenset(
    {
        'non_public_address',
        'dns_busy',
        'dns_failed',
        'cancelled',
        'timeout',
        'page_too_large',
        'network_failed',
        'read_failed',
        'invalid_headers',
        'ambiguous_headers',
        'unsupported_content_type',
        'unsupported_encoding',
        'invalid_url',
        'redirect_loop',
        'redirect_limit',
        'invalid_redirect',
        'insecure_redirect',
        'http_error',
        'unsupported_content_encoding',
        'no_readable_text',
    }
)


def visible_text(value):
    # Native metadata rejects control characters. Normalize them before the
    # excerpt is bounded, so one C0/C1 character cannot erase an entire view.
    return ' '.join(re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', value).split())


class PageError(Exception):
    """A fixed, non-sensitive failure code suitable for tool responses."""


def public_address(raw):
    value = ipaddress.ip_address(raw)
    if (
        not value.is_global
        or value.is_multicast
        or isinstance(value, ipaddress.IPv6Address)
        and (value.ipv4_mapped or value.sixtofour or value.teredo or value.scope_id)
    ):
        raise PageError('non_public_address')
    return str(value)


def addresses(host, deadline, current):
    # OS DNS has no per-call cancellation. At most two bounded daemon workers
    # may outlive a cancelled read; no executor shutdown waits on a stuck DNS.
    if not DNS_SLOTS.acquire(blocking=False):
        raise PageError('dns_busy')
    results = queue.Queue(maxsize=1)

    def resolve():
        try:
            results.put(socket.getaddrinfo(host, None, type=socket.SOCK_STREAM))
        except OSError:
            results.put(None)
        finally:
            DNS_SLOTS.release()

    try:
        threading.Thread(target=resolve, daemon=True, name='cc8-page-dns').start()
    except RuntimeError:
        DNS_SLOTS.release()
        raise PageError('dns_busy') from None
    while current() and time.monotonic() < deadline:
        try:
            rows = results.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if not rows or len(rows) > 64:
            raise PageError('dns_failed')
        values = []
        for row in rows:
            values.append(public_address(row[4][0]))
        # Prefer a public IPv4 endpoint on hosts without a usable IPv6 route;
        # still validate every DNS answer before connecting to any one of them.
        return min(values, key=lambda value: ':' in value)
    raise PageError('cancelled' if not current() else 'timeout')


def fetch(url, address, deadline, current, *, popen=subprocess.Popen):
    address = public_address(address)
    port = urlsplit(url).port or (443 if url.startswith('https:') else 80)
    pin = '[' + address + ']' if ':' in address else address
    remaining = deadline - time.monotonic()
    if not current() or remaining <= 0:
        raise PageError('cancelled' if not current() else 'timeout')
    command = [
        '/usr/bin/curl',
        '-q',
        '--silent',
        '--show-error',
        '--globoff',
        '--noproxy',
        '*',
        '--proto',
        '=http,https',
        # Empty origin host/port match every URL spelling. This cannot fall
        # back to an unchecked DNS lookup after hostname normalization; TLS
        # SNI/certificate checks and HTTP Host still use the original URL.
        '--connect-to',
        f'::{pin}:{port}',
        '--max-time',
        str(remaining),
        '--connect-timeout',
        str(min(3, remaining)),
        '--max-filesize',
        str(MAX_BYTES),
        '--header',
        'Accept-Encoding: identity',
        '--header',
        'Accept: text/html, application/xhtml+xml, text/plain',
        '--user-agent',
        'CC8-SourceReader/1.0',
        '--dump-header',
        '/dev/stderr',
        '--url',
        url,
    ]
    process = popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    chunks = {'body': bytearray(), 'headers': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'body')
            selector.register(process.stderr, selectors.EVENT_READ, 'headers')
            while selector.get_map():
                if not current():
                    raise PageError('cancelled')
                if time.monotonic() >= deadline:
                    raise PageError('timeout')
                for key, _ in selector.select(0.05):
                    # BufferedReader.read may wait for a full chunk; read1 never
                    # waits for more data than the readiness event promised.
                    block = key.fileobj.read1(16384)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    chunks[key.data].extend(block)
                    limit = MAX_BYTES if key.data == 'body' else MAX_HEADERS
                    if len(chunks[key.data]) > limit:
                        raise PageError('page_too_large')
        if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
            raise PageError('network_failed')
        return bytes(chunks['body']), bytes(chunks['headers'])
    finally:
        try:
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            process.wait(timeout=2)
        finally:
            process.stdout.close()
            process.stderr.close()


def headers(raw):
    if len(raw) > MAX_HEADERS or b'\x00' in raw:
        raise PageError('invalid_headers')
    # Ignore interim 1xx blocks. curl is not following redirects or using a
    # proxy, so a second final response is not a valid single-hop receipt.
    blocks = raw.decode('iso-8859-1').split('\r\n\r\n')
    final = None
    for block in blocks:
        if not block:
            continue
        lines = block.split('\r\n')
        status = lines[0].split()
        if (
            len(status) < 2
            or not status[0].startswith('HTTP/')
            or re.fullmatch('[1-5][0-9]{2}', status[1]) is None
        ):
            raise PageError('invalid_headers')
        code = int(status[1])
        if 100 <= code < 200:
            continue
        if final is not None:
            raise PageError('invalid_headers')
        fields = {}
        for line in lines[1:]:
            if ':' not in line or line[:1].isspace():
                raise PageError('invalid_headers')
            name, value = line.split(':', 1)
            name = name.lower()
            if name in ('location', 'content-type', 'content-encoding') and name in fields:
                raise PageError('ambiguous_headers')
            fields[name] = value.strip()
        final = code, fields
    if final is None:
        raise PageError('invalid_headers')
    return final


def iso_publication_label(value):
    if not isinstance(value, str) or not PUBLICATION_DATE.fullmatch(value):
        return None
    try:
        datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    return value


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title = []
        self.parts = []
        self.dates = []
        self.metadata = None
        self.metadata_blocks = 0
        self.articles = []

    def add_date(self, value):
        value = visible_text(value)
        if value and len(value) <= 64 and value not in self.dates and len(self.dates) < 8:
            self.dates.append(value)

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and not self.hidden and self.metadata_blocks < MAX_METADATA_BLOCKS:
            types = [(value or '').strip().lower() for name, value in attrs if name == 'type']
            if types == ['application/ld+json'] and all(name != 'src' for name, _ in attrs):
                self.metadata_blocks += 1
                self.metadata = ''
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden += 1
        if tag == 'title':
            self.in_title = True
        if tag == 'meta' and not self.hidden:
            values = dict(attrs)
            if len(values) != len(attrs):
                return  # Conflicting duplicate attributes do not pick a date.
            key = (values.get('property') or values.get('name') or '').lower()
            value = values.get('content') or ''
            if key in ('article:published_time', 'datepublished', 'dc.date.issued'):
                # Preserve the publisher's string. Neither this nor an HTTP
                # Last-Modified header proves an event or publication date.
                self.add_date(value)
            elif key == 'parsely-pub-date' and (date := iso_publication_label(value.strip())):
                self.add_date(date)

    def handle_endtag(self, tag):
        if tag == 'script' and self.metadata is not None:
            self.articles.extend(article_nodes(self.metadata))
            self.metadata = None
        if tag in ('script', 'style', 'noscript', 'template'):
            self.hidden = max(0, self.hidden - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.metadata is not None:
            self.metadata = (
                self.metadata + data
                if len(self.metadata) + len(data) <= MAX_METADATA_CHARS
                else None
            )
        if self.hidden:
            return
        if self.in_title:
            self.title.append(data)
        self.parts.append(data)


def unique_json_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError('Ambiguous JSON-LD fields')
    return result


def article_nodes(raw):
    try:
        root = json.loads(raw, object_pairs_hook=unique_json_fields)
        if not isinstance(root, dict) or root.get('@context') not in SCHEMA_CONTEXTS:
            return []
        nodes = root.get('@graph', [root])
        if not isinstance(nodes, list) or len(nodes) > 32:
            return []
        articles = []
        for node in nodes:
            if not isinstance(node, dict) or '@context' in node and node is not root:
                continue
            types = node.get('@type')
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and any(
                isinstance(kind, str) and kind in ARTICLE_TYPES for kind in types
            ):
                articles.append(node)
        return articles
    except (ValueError, RecursionError):
        return []


def article_publication_date(articles, page_url):
    matching = []
    target = source_key(page_url)
    for article in articles:
        # Prefer the explicit relationship to this page. An article's URL
        # also identifies it when mainEntityOfPage was omitted.
        ref = article.get('mainEntityOfPage', article.get('url'))
        if isinstance(ref, dict):
            ref = ref.get('@id')
        declared = public_url(ref)
        if declared and source_key(declared) == target:
            matching.append(article)
    if len(matching) == 1:
        return iso_publication_label(matching[0].get('datePublished'))
    if len(articles) == 1 and 'url' not in articles[0] and 'mainEntityOfPage' not in articles[0]:
        return iso_publication_label(articles[0].get('datePublished'))
    return None


def extract(body, content_type, page_url):
    media = content_type.split(';', 1)[0].strip().lower()
    if media not in ('text/html', 'application/xhtml+xml', 'text/plain'):
        raise PageError('unsupported_content_type')
    # Do not decompress a server response that ignored Accept-Encoding: identity.
    # UTF-8 decoding errors are explicit; no guessed encoding can invent text.
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise PageError('unsupported_encoding') from None
    if media == 'text/plain':
        return '', visible_text(text), []
    parser = PageText()
    parser.feed(text)
    parser.close()
    if date := article_publication_date(parser.articles, page_url):
        parser.add_date(date)
    return (
        visible_text(' '.join(parser.title))[:160],
        visible_text(' '.join(parser.parts)),
        parser.dates,
    )


def read_page(url, current=lambda: True, *, resolve=addresses, request=fetch, clock=now):
    """Fetch one bounded public source; return only sanitized failures and RAM text."""
    original = public_url(url)
    result = {'ok': False, 'url': original or '', 'error': 'invalid_url'}
    deadline = time.monotonic() + 8
    try:
        target = original
        seen = set()
        for hop in range(4):
            if not current():
                raise PageError('cancelled')
            if not target or not urlsplit(target).hostname.isascii():
                raise PageError('invalid_url')
            if target in seen:
                raise PageError('redirect_loop')
            seen.add(target)
            # Only record a public destination once this hop is attempted.
            # A rejected redirect target cannot become evidence for another URL.
            result['final_url'] = target
            host = urlsplit(target).hostname
            address = resolve(host, deadline, current)
            body, raw = request(target, address, deadline, current)
            if not current():
                raise PageError('cancelled')
            if time.monotonic() >= deadline:
                raise PageError('timeout')
            if len(body) > MAX_BYTES:
                raise PageError('page_too_large')
            code, fields = headers(raw)
            if code in (301, 302, 303, 307, 308):
                if hop == 3:
                    raise PageError('redirect_limit')
                next_target = public_url(urljoin(target, fields.get('location', '')))
                if not next_target:
                    raise PageError('invalid_redirect')
                if urlsplit(target).scheme == 'https' and urlsplit(next_target).scheme != 'https':
                    raise PageError('insecure_redirect')
                target = next_target
                continue
            if code != 200:
                result['http_status'] = code
                raise PageError('http_error')
            # A 200 response can still fail decoding/extraction. Keep only a
            # terminal response status, never a prior redirect's status.
            result['http_status'] = code
            if fields.get('content-encoding', 'identity').lower() != 'identity':
                raise PageError('unsupported_content_encoding')
            title, text, dates = extract(body, fields.get('content-type', ''), target)
            if not current() or time.monotonic() >= deadline:
                raise PageError('cancelled' if not current() else 'timeout')
            if not text:
                raise PageError('no_readable_text')
            return dict(
                ok=True,
                url=original,
                final_url=target,
                observed_at=clock(),
                http_status=code,
                title=title,
                text=text[:MAX_TEXT],
                text_truncated=len(text) > MAX_TEXT,
                received_bytes=len(body),
                publication_labels=dates,
                facts_verified=False,
                untrusted_page=True,
            )
        raise PageError('redirect_limit')
    except PageError as error:
        code = str(error)
        result['error'] = code if code in FAILURE_CODES else 'read_failed'
    except (OSError, ValueError, subprocess.SubprocessError):
        result['error'] = 'network_failed'
    result['observed_at'] = clock()
    return result
