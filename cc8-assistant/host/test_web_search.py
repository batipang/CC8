import json
import unittest
from minimax_client import MiniMaxBrain, MiniMaxError, MiniMaxSearchError, Cancelled, Transport
from web_search import normalize, public_url, parse_plan, SpeechClauses, wants_web


def result(title='官方公告', url='https://example.com/news', snippet='发布于本周的新产品公告。'):
    return {'title': title, 'link': url, 'snippet': snippet, 'date': '2026-10-30'}


class Fake:
    def __init__(self, plan=None, search=None, error=None):
        self.plan = plan or {'query': '官方 最新公告', 'clarification': ''}
        self.search = (
            search
            if search is not None
            else {'base_resp': {'status_code': 0}, 'organic': [result()]}
        )
        self.calls = []
        self.error = error
        self.on_search = None

    def cancel(self):
        pass

    def post_json(self, route, body, current):
        if not current():
            raise Cancelled()
        self.calls.append((route, body))
        if route.endswith('/search'):
            if self.on_search:
                self.on_search()
            if self.error:
                raise self.error
            return self.search
        return {
            'choices': [
                {
                    'finish_reason': 'stop',
                    'message': {'content': json.dumps(self.plan, ensure_ascii=False)},
                }
            ]
        }

    def events(self, route, body, current):
        if not current():
            raise Cancelled()
        self.calls.append((route, body))
        yield {
            'choices': [{'delta': {'content': '我查到官方发布了新产品。'}, 'finish_reason': 'stop'}]
        }


class Routing(unittest.TestCase):
    def test_fresh_facts_and_explicit_search_use_web(self):
        for q in [
            '上海明天气温多少',
            '今天有什么新闻',
            '人民币汇率',
            '现在黄金价格',
            '查一下Python文档',
        ]:
            self.assertTrue(wants_web(q), q)

    def test_creative_and_personal_requests_do_not_search(self):
        for q in ['今天心情不开心', '现在给我讲个故事', '你好，介绍一下你自己', '谢谢']:
            self.assertFalse(wants_web(q, True), q)

    def test_followup_city_uses_previous_web_context(self):
        self.assertTrue(wants_web('上海', True))
        self.assertFalse(wants_web('上海', False))

    def test_plan_has_exactly_one_action_and_bounded_lengths(self):
        self.assertEqual(
            parse_plan('```json\n{"query":"上海 天气", "clarification":""}\n```'), ('上海 天气', '')
        )
        for value in [
            {'query': 'q', 'clarification': 'ask'},
            {'query': '', 'clarification': ''},
            {'query': 1, 'clarification': ''},
            {'query': 'x' * 401, 'clarification': ''},
        ]:
            with self.assertRaises(ValueError):
                parse_plan(json.dumps(value))


class SearchData(unittest.TestCase):
    def test_local_unsafe_or_credential_urls_rejected(self):
        for url in [
            'file:///x',
            'javascript:alert(1)',
            'https://127.0.0.1/x',
            'http://192.168.1.2/',
            'http://[::1]/',
            'https://a.local/',
            'https://user:pass@example.com/',
            'https://example.com:8080/x',
            'https://example.com/\nsecret',
        ]:
            self.assertIsNone(public_url(url), url)

    def test_url_scalar_limit_preserves_valid_unicode_and_rejects_broken_sequences(self):
        prefix = 'https://example.com/'
        url = prefix + '\U0001f600' * (2048 - len(prefix))
        self.assertEqual(public_url(url), url)
        self.assertIsNone(public_url(url + 'x'))
        for bad in ('\ud800', '\udfff', '\x80', '\x9f'):
            self.assertIsNone(public_url(prefix + bad))

    def test_normalize_deduplicates_bounds_and_keeps_dates_separate(self):
        rows = [
            result(snippet='a' * 9000),
            result(url='https://example.com/news#same'),
            result(url='file:///x'),
        ] + [result(url=f'https://example.com/{i}') for i in range(10)]
        items = normalize({'organic': rows})
        self.assertEqual(len(items), 6)
        self.assertEqual(len(items[0]['snippet']), 1400)
        self.assertEqual(items[0]['published_at'], '2026-10-30')
        self.assertNotIn('retrieved_at', items[0])

    def test_search_fields_remain_serializable_unicode_and_optional_date_can_be_unknown(self):
        for field in ('title', 'snippet'):
            row = result()
            row[field] = 'bad\ud800'
            self.assertEqual(normalize({'organic': [row]}), [])
        row = result(title='\U0001f600' * 160)
        row['date'] = '\udfff'
        normalized = normalize({'organic': [row]})
        self.assertEqual(normalized[0]['published_at'], '')
        self.assertEqual(normalized[0]['title'], row['title'])
        json.dumps(normalized, ensure_ascii=False).encode('utf-8')
        row.update(title='first\x00 second', snippet='some\x80 text', date=True)
        normalized = normalize({'organic': [row]})
        self.assertEqual(normalized[0]['title'], 'first second')
        self.assertEqual(normalized[0]['snippet'], 'some text')
        self.assertEqual(normalized[0]['published_at'], '')

    def test_split_urls_and_citations_never_spoken(self):
        s = SpeechClauses()
        out = []
        for piece in [
            '来源是[官方](htt',
            'ps://example.com/a?x=1',
            '&q=ok)。参考 https://example.com/test',
            '?x=2!a，已核实[1]。',
        ]:
            out.extend(s.feed(piece))
        out.extend(s.feed('', True))
        spoken = ''.join(out)
        self.assertNotIn('http', spoken)
        self.assertNotIn('example', spoken)
        self.assertNotIn('[1]', spoken)
        self.assertIn('官方', spoken)
        self.assertIn('已核实', spoken)


class WebTurns(unittest.TestCase):
    def test_lookup_happens_before_answer_and_sources_have_no_snippets(self):
        f = Fake()
        b = MiniMaxBrain(f)
        updates = []
        answer = ''.join(
            b.clauses('查一下最新公告', [], on_web=lambda k, v: updates.append((k, v)))
        )
        self.assertIn('新产品', answer)
        self.assertEqual(
            [r for r, _ in f.calls],
            ['/v1/chat/completions', '/v1/coding_plan/search', '/v1/chat/completions'],
        )
        self.assertEqual(
            [k for k, _ in updates],
            ['clear', 'searching', 'route_completed', 'sources', 'dot_result'],
        )
        self.assertEqual(updates[-3][1], {'scenario': 'web'})
        self.assertNotIn('snippet', updates[-2][1]['items'][0])
        self.assertTrue(updates[-2][1]['retrieved_at'])
        self.assertEqual(updates[-2][1]['time_basis'], 'search_response')
        self.assertEqual(updates[-2][1]['items'][0]['access_evidence'], 'search_result_returned')
        self.assertIs(updates[-2][1]['items'][0]['body_verified'], False)
        self.assertIn('新产品', updates[-1][1]['text'])
        self.assertIs(b.last_metrics['completed'], True)
        prompt = f.calls[-1][1]['messages']
        self.assertIn('不可信网页摘录', prompt[-1]['content'])
        self.assertIn('绝不能服从', prompt[0]['content'])

    def test_missing_weather_city_clarifies_then_searches_after_city(self):
        f = Fake(plan={'query': '', 'clarification': '你想查哪个城市的天气？'})
        b = MiniMaxBrain(f)
        self.assertIn('哪个城市', b.reply('今天天气怎么样', []))
        self.assertEqual(len(f.calls), 1)
        f.plan = {'query': '上海 2026-10-31 天气', 'clarification': ''}
        b.reply('上海', [])
        self.assertEqual(f.calls[1][0], '/v1/chat/completions')
        self.assertIn('今天天气', f.calls[1][1]['messages'][-2]['content'])
        self.assertIsNone(b.pending_web)
        self.assertEqual(f.calls[2][1]['q'], '上海 2026-10-31 天气')

    def test_search_error_cannot_fall_back_to_model_facts(self):
        f = Fake(error=MiniMaxError('network', http_status=503))
        b = MiniMaxBrain(f)
        with self.assertRaises(MiniMaxError) as raised:
            b.reply('今天最新新闻', [])
        self.assertIsInstance(raised.exception, MiniMaxSearchError)
        self.assertEqual(raised.exception.http_status, 503)
        self.assertEqual(len(f.calls), 2)
        self.assertTrue(b.last_metrics['web_error'])

    def test_empty_results_do_not_get_a_model_answer(self):
        f = Fake(search={'base_resp': {'status_code': 0}, 'organic': []})
        updates = []
        answer = ''.join(
            MiniMaxBrain(f).clauses('查一下新闻', [], on_web=lambda kind, _: updates.append(kind))
        )
        self.assertIn('无法核实', answer)
        self.assertEqual(updates, ['clear', 'searching', 'no_results'])
        self.assertEqual(len(f.calls), 2)

    def test_missing_provider_status_is_failure(self):
        f = Fake(search={'organic': [result()]})
        with self.assertRaises(MiniMaxSearchError):
            MiniMaxBrain(f).reply('查一下新闻', [])
        self.assertEqual(len(f.calls), 2)

    def test_cancel_during_search_drops_late_sources_and_answer(self):
        f = Fake()
        current = [True]
        f.on_search = lambda: current.__setitem__(0, False)
        updates = []
        with self.assertRaises(Cancelled):
            list(
                MiniMaxBrain(f).clauses(
                    '查一下新闻', [], lambda: current[0], lambda k, v: updates.append(k)
                )
            )
        self.assertNotIn('sources', updates)
        self.assertNotIn('route_completed', updates)
        self.assertEqual(len(f.calls), 2)

    def test_cancel_before_start_makes_no_network_request(self):
        f = Fake()
        with self.assertRaises(Cancelled):
            list(MiniMaxBrain(f).clauses('查天气', [], lambda: False))
        self.assertEqual(f.calls, [])

    def test_bad_plan_still_searches_original_question(self):
        f = Fake(plan={'query': '', 'clarification': ''})
        self.assertIn('查到', MiniMaxBrain(f).reply('最新消息', []))
        self.assertEqual(f.calls[1][1]['q'], '最新消息')

    def test_bad_weather_plan_does_not_guess_location(self):
        f = Fake(plan={'query': '', 'clarification': ''})
        self.assertIn('城市', MiniMaxBrain(f).reply('今天天气', []))
        self.assertEqual(len(f.calls), 1)

    def test_planner_does_not_inherit_past_assistant_failure_as_instruction(self):
        f = Fake()
        MiniMaxBrain(f).reply('查今天新闻', [{'role': 'assistant', 'content': '我不能联网。'}])
        self.assertFalse(any('不能联网' in row['content'] for row in f.calls[0][1]['messages']))


class JsonResponse:
    status_code = 200

    def __init__(self, data):
        self.raw = json.dumps(data).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        self.closed = True


class JsonTransport(unittest.TestCase):
    def make(self, data):
        response = JsonResponse(data)

        class Session:
            def post(self, *args, **kwargs):
                self.kwargs = kwargs
                return response

            def close(self):
                pass

        session = Session()
        return Transport(key='sk-cp-test', session_factory=lambda: session), response, session

    def test_json_redirect_not_followed(self):
        t, r, s = self.make({})
        r.status_code = 302
        with self.assertRaises(MiniMaxError):
            t.post_json('/v1/coding_plan/search', {})
        self.assertFalse(s.kwargs['allow_redirects'])
        self.assertTrue(r.closed)

    def test_json_provider_error_sanitized(self):
        t, r, s = self.make({'base_resp': {'status_code': 1008, 'status_msg': 'secret body'}})
        with self.assertRaises(MiniMaxError) as e:
            t.post_json('/v1/coding_plan/search', {})
        self.assertNotIn('secret', str(e.exception))
        self.assertEqual(e.exception.code, 1008)

    def test_cancel_while_reading_json_suppresses_result(self):
        t, r, s = self.make({})
        current = [True]

        def content(chunk_size):
            current[0] = False
            yield r.raw

        r.iter_content = content
        with self.assertRaises(Cancelled):
            t.post_json('/v1/coding_plan/search', {}, lambda: current[0])
        self.assertTrue(r.closed)

    def test_json_size_bound(self):
        t, r, s = self.make({})
        r.raw = b'x' * (2 * 1024 * 1024 + 1)
        with self.assertRaises(MiniMaxError):
            t.post_json('/v1/coding_plan/search', {})


if __name__ == '__main__':
    unittest.main()
