import contextlib
import copy
import io
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests
import news_v8 as news


ITEM = dict(category='technology', headline='Новый чип', body='Компания представила чип.',
            aside='', source_urls=['https://example.org/news'], source_names=['Source'],
            importance=7, confidence=.8, preference_tags=['chips'],
            is_random_top=False, is_internet_element=False)
EDITION = dict(title='Новости', items=[ITEM])
CFG = dict(timezone='UTC', edition=dict(target_items=5, min_items=0, max_items=15,
           max_politics_share=0), models=dict(editor='openrouter/free'))


def response(content=None, status=200, finish='stop'):
    r = Mock(status_code=status)
    r.json.return_value = {'choices': [{'message': {'content': content}, 'finish_reason': finish}]}
    return r


class EditorTransportTests(unittest.TestCase):
    def run_editor(self, responses, cfg=None):
        session = Mock()
        session.post.side_effect = responses
        with patch.dict(news.os.environ, {'OPENROUTER_API_KEY': 'TEST-ONLY-NOT-A-KEY'}, clear=True), \
                patch.object(news, 'fallback_edition', return_value={'title': 'fallback', 'items': []}) as fallback:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = news.openrouter_edition(session, [{"title": "test candidate"}], [], cfg or CFG, '', '',
                                                datetime.now(timezone.utc), [], False, False)
        return result, session, fallback, output.getvalue()

    def test_text_and_fenced_json(self):
        for text in (json.dumps(EDITION), '```json\n' + json.dumps(EDITION) + '\n```'):
            result, session, fallback, _ = self.run_editor([response(text)])
            self.assertEqual(result, EDITION)
            self.assertEqual(session.post.call_args.kwargs['timeout'], (10, 120))
            fallback.assert_not_called()

    def test_text_parts(self):
        text = json.dumps(EDITION)
        parts = [{'type': 'text', 'text': text[:20]}, {'type': 'text', 'text': text[20:]}]
        self.assertEqual(self.run_editor([response(parts)])[0], EDITION)

    def test_invalid_outputs_fail_closed(self):
        for content in ('', None, '[]', '{"items": []}', '{"title":"t","items":[{}]}',
                        'prefix ' + json.dumps(EDITION)):
            result, session, fallback, _ = self.run_editor([response(content), response(content)])
            self.assertEqual(result['title'], 'fallback')
            self.assertEqual(session.post.call_count, 2)
            fallback.assert_called_once()

    def test_truncated_valid_json_is_rejected(self):
        result = self.run_editor([response(json.dumps(EDITION), finish='length')] * 2)[0]
        self.assertEqual(result['title'], 'fallback')

    def test_full_local_schema_validation(self):
        for change in ({'confidence': 2}, {'importance': True}, {'source_urls': 'bad'},
                       {'unknown': 1}, {'headline': ''}):
            edition = copy.deepcopy(EDITION)
            edition['items'][0].update(change)
            with self.assertRaises(news.EditorResponseError):
                news.parse_editor_response(response(json.dumps(edition)).json())

    def test_schema_compatibility_retry(self):
        captured = []
        session = Mock()
        def post(*args, **kwargs):
            captured.append(copy.deepcopy(kwargs['json']))
            return response(status=400) if len(captured) == 1 else response(json.dumps(EDITION))
        session.post.side_effect = post
        with patch.dict(news.os.environ, {'OPENROUTER_API_KEY': 'TEST-ONLY-NOT-A-KEY'}, clear=True):
            result = news.openrouter_edition(session, [{"title": "test candidate"}], [], CFG, '', '', datetime.now(timezone.utc), [], False, False)
        self.assertEqual(result, EDITION)
        self.assertIn('response_format', captured[0])
        self.assertNotIn('response_format', captured[1])
        self.assertIn('source_urls', captured[1]['messages'][0]['content'])

    def test_nonretryable_status_and_sensitive_text_not_logged(self):
        for status in (401, 402, 403, 429, 503):
            r = response(status=status)
            r.text = 'SECRET PROVIDER RESPONSE'
            _, session, _, output = self.run_editor([r])
            self.assertEqual(session.post.call_count, 1)
            self.assertNotIn('SECRET', output)
            self.assertNotIn('TEST-ONLY', output)

    def test_transport_error_is_sanitized(self):
        _, _, _, output = self.run_editor([requests.Timeout('SECRET URL')] * 2)
        self.assertNotIn('SECRET', output)
        self.assertIn('transport_error', output)

    def test_provider_json_error(self):
        r = response()
        r.json.return_value = {'error': {'message': 'SECRET'}}
        result, _, _, output = self.run_editor([r, r])
        self.assertEqual(result['title'], 'fallback')
        self.assertNotIn('SECRET', output)

    def test_free_model_guard(self):
        for model in ('paid/model', 'openrouter/auto', None):
            cfg = copy.deepcopy(CFG)
            cfg['models']['editor'] = model
            with self.assertRaisesRegex(ValueError, 'free'):
                self.run_editor([], cfg)

    def test_valid_empty_edition_is_not_fabricated(self):
        expected = {'title': 'Нет подходящих новостей', 'items': []}
        self.assertEqual(self.run_editor([response(json.dumps(expected))])[0], expected)


if __name__ == '__main__':
    unittest.main()
