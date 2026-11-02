import copy
import unittest
from datetime import datetime, timezone
import news_v81 as news

NOW = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
CFG = {'lookback_hours': 8, 'edition': {'max_items': 15}}
C = {'title': 'Компания представила новый процессор', 'url': 'https://example.org/article', 'source_domain': 'example.org', 'rss_source_name': 'Издание', 'published_at': '2026-11-02T12:00:00+00:00', 'collection_batch': 'engineering'}
I = {'headline': 'Компания представила процессор', 'body': 'Компания представила новый процессор.', 'source_urls': [C['url']], 'aside': '', 'category': 'engineering', 'importance': 7}

class EditorialTests(unittest.TestCase):
    def test_dates_required_for_all_batches(self):
        for date in ['', 'bad', '2026-11-02T12:00:00', '2026-10-02T12:00:00+00:00', '2026-11-03T12:00:00+00:00']:
            self.assertFalse(news.candidate_is_safe(dict(C, published_at=date, collection_batch='random_top'), NOW, CFG))
        self.assertTrue(news.candidate_is_safe(C, NOW, CFG))
    def test_routine_noise_rejected_major_negative_not_banned(self):
        self.assertFalse(news.candidate_is_safe(dict(C, title='КНДР начала военные учения'), NOW, CFG))
        self.assertTrue(news.candidate_is_safe(dict(C, title='В Испании произошла крупная железнодорожная катастрофа'), NOW, CFG))
    def test_no_raw_fallback(self):
        self.assertEqual(news.fallback_edition([C], CFG, NOW)['items'], [])
    def test_unknown_sources_and_numbers_rejected(self):
        for item in [dict(I, source_urls=['https://invented.test/']), dict(I, body='Компания представила 250 процессоров.')]:
            self.assertEqual(news.validate_edition({'items':[item]}, [C], CFG, NOW)['items'], [])
    def test_clickbait_and_non_russian_rejected(self):
        for title in ['Стало известно о новом процессоре', 'Что представила компания?', 'A new processor']:
            self.assertEqual(news.validate_edition({'items':[dict(I, headline=title)]}, [C], CFG, NOW)['items'], [])
    def test_provenance_added_and_duplicate_removed(self):
        result=news.validate_edition({'items':[I,I]}, [C], CFG, NOW)['items']
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['source_dates'],[{'url':C['url'],'published_at':C['published_at']}])
    def test_meme_is_not_any_headline_about_meme_person(self):
        self.assertFalse(news.candidate_is_safe(dict(C, collection_batch='internet_culture', title='Умер герой интернет-мемов'), NOW, CFG))
        c=dict(C, collection_batch='internet_culture', title='Новый мем о роботе набрал популярность')
        self.assertTrue(news.candidate_is_safe(c, NOW, CFG))
        self.assertEqual(news.validate_edition({'items':[I]}, [c], CFG, NOW, allow_meme=False)['items'],[])

if __name__ == '__main__': unittest.main()

class PipelineTests(unittest.TestCase):
    def test_all_sources_failed_report_degraded(self):
        from unittest.mock import patch
        import news_v8 as v
        from pathlib import Path
        with patch.object(Path, 'write_text', return_value=0), \
             patch.object(v, 'rss_candidates', side_effect=RuntimeError('offline')), \
             patch.object(v.base, 'load_state', return_value={}), \
             patch.object(v.base, 'load_master_feed', return_value=[]), \
             patch.object(v.base, 'recent_history', return_value=[]), \
             patch.object(v.base, 'update_state'), \
             patch.object(v.base, 'save_outputs', return_value=(Path('x'), Path('y'), [], [])) as save:
            v.main()
            self.assertIn(save.call_args.args[0]['editor_status'], {'degraded', 'review_required'})
    def test_meme_from_other_batch_respects_daily_limit(self):
        c=dict(C, title='Новый мем о роботе набрал популярность')
        item=dict(I, is_internet_element=True)
        self.assertEqual(news.validate_edition({'items':[item]}, [c], CFG, NOW, allow_meme=False)['items'], [])
    def test_economic_summary_not_blanket_banned(self):
        self.assertTrue(news.candidate_is_safe(dict(C, title='Экономическая сводка: ВВП вырос'), NOW, CFG))
