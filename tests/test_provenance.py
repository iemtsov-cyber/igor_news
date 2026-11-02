import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import news

NOW = datetime.fromisoformat('2026-11-02T17:00:00+03:00')
CFG = {'dedupe': {'title_similarity_threshold': .79}, 'timezone': 'Europe/Moscow', 'edition': {}}


class ProvenanceTests(unittest.TestCase):
    def item(self):
        return {'category': 'technology', 'story_id': 'stable-old-id', 'headline': 'Новая модель чипа', 'body': 'Проверенный текст.',
                'added_at': '2026-10-01T12:00:00+03:00', 'source_urls': ['https://example.org/news?id=1'],
                'source_names': ['Первоисточник'], 'published_at': '2026-10-01T10:00:00+03:00',
                'source_dates': [{'url': 'https://example.org/news?id=1', 'published_at': '2026-10-01T10:00:00+03:00'}]}

    def test_provenance_and_identity_survive_normalization(self):
        item = self.item()
        result = news.public_story(item, CFG, NOW)
        for key in ('source_urls', 'source_names', 'published_at', 'source_dates', 'added_at', 'story_id'):
            self.assertEqual(result[key], item[key])
        self.assertEqual(news.public_story(result, CFG, NOW), result)

    def test_no_retroactive_provenance(self):
        item = {'story_id': 'old', 'headline': 'Архив', 'body': 'История', 'added_at': '2026-09-01T10:00:00+03:00'}
        result = news.public_story(item, CFG, NOW)
        for key in ('source_urls', 'source_names', 'published_at', 'source_dates'):
            self.assertNotIn(key, result)
        self.assertEqual(result['added_at'], item['added_at'])
        self.assertEqual(result['story_id'], 'old')

    def test_reject_unsafe_urls_dates_and_unmatched_dates(self):
        item = self.item()
        item['source_urls'] += ['javascript:alert(1)', 'https://user:pass@example.org/private', 'file:///tmp/a']
        item['published_at'] = '2026-11-02T12:00:00'
        item['source_dates'].append({'url': 'https://unverified.test/', 'published_at': '2026-11-02T10:00:00Z'})
        clean = news.public_story(item, CFG, NOW)
        self.assertEqual(clean['source_urls'], ['https://example.org/news?id=1'])
        self.assertNotIn('published_at', clean)
        self.assertEqual(len(clean['source_dates']), 1)

    def test_source_url_duplicate_not_reposted_after_rewrite(self):
        old = self.item()
        candidate = {'headline': 'Совершенно другой заголовок', 'body': 'Другой текст',
                     'source_urls': ['https://example.org/news?utm_source=feed&id=1#section']}
        before = copy.deepcopy(old)
        merged, added = news.merge_feed([old], [candidate], CFG, NOW)
        self.assertEqual(added, [])
        self.assertEqual(merged, [before])
        self.assertEqual(old, before)

    def test_different_article_query_is_not_duplicate(self):
        merged, added = news.merge_feed([self.item()], [{'headline': 'Космический аппарат достиг Марса', 'body': 'Другой сюжет', 'source_urls': ['https://example.org/news?id=2']}], CFG, NOW)
        self.assertEqual(len(added), 1)

    def test_feed_reload_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'docs').mkdir()
            (root / 'docs/feed.json').write_text(json.dumps({'items': [self.item()]}))
            with patch.object(news, 'ROOT', root):
                result = news.load_master_feed(CFG, NOW)
            self.assertEqual(result[0]['source_dates'], self.item()['source_dates'])
            self.assertEqual(result[0]['story_id'], 'stable-old-id')

    def test_empty_edition_keeps_live_dates_and_archive_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = root / 'docs'
            (docs / 'archive').mkdir(parents=True)
            (docs / 'data').mkdir()
            files = {'feed.json': json.dumps({'updated_at': '2026-10-01T12:00:00+03:00', 'items': [self.item()]}),
                     'index.html': 'Обновлено 01.10.2026', 'archive/2026-10-01.html': 'old archive',
                     'data/2026-10-01.json': 'old data'}
            for name, value in files.items():
                (docs / name).write_text(value)
            with patch.object(news, 'ROOT', root):
                _, _, _, added = news.save_outputs({'items': [], 'editor_status': 'unavailable'}, [], CFG, NOW, [self.item()])
            self.assertEqual(added, [])
            for name, value in files.items():
                self.assertEqual((docs / name).read_text(), value)
            health = json.loads((docs / 'health.json').read_text())
            self.assertEqual(health['status'], 'degraded')
            self.assertEqual(health['last_content_update_at'], '2026-10-01T12:00:00+03:00')
            self.assertEqual(health['checked_at'], NOW.isoformat())
            self.assertFalse((docs / 'data/2026-11-02.json').exists())


if __name__ == '__main__':
    unittest.main()

class DatePrecisionTests(unittest.TestCase):
    def test_calendar_day_preserved_without_invented_time(self):
        self.assertEqual(news.source_date('2026-11-02'), '2026-11-02')
        self.assertEqual(news.source_date('2026-02-30'), '')
        self.assertEqual(news.source_date('2026-11-02T12:00:00'), '')
