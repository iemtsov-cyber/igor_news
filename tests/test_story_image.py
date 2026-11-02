import unittest
from datetime import datetime, timezone
import news

IMAGE = {'url': 'https://assets.science.nasa.gov/image.jpg?w=1920&h=1200', 'alt': 'Облако на Марсе',
         'caption': 'Облако возле вулкана', 'credit': 'ESA / DLR', 'license_name': 'CC BY-SA 3.0 IGO',
         'license_url': 'https://creativecommons.org/licenses/by-sa/3.0/igo/',
         'source_url': 'https://science.nasa.gov/image-article/mars/'}
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
CFG = {'timezone': 'Europe/Moscow', 'edition': {}}
ITEM = {'story_id': 'image-test', 'headline': 'Облако на Марсе', 'body': 'Снимок орбитального аппарата.', 'category': 'space'}


class StoryImageTests(unittest.TestCase):
    def test_image_preserved_through_normalization(self):
        normalized = news.public_story(dict(ITEM, image=IMAGE), CFG, NOW)
        self.assertEqual(normalized['image'], IMAGE)
        self.assertEqual(news.public_story(normalized, CFG, NOW), normalized)
        self.assertEqual(normalized['story_id'], ITEM['story_id'])

    def test_legacy_has_no_image(self):
        self.assertNotIn('image', news.public_story(ITEM, CFG, NOW))
        self.assertEqual(news.render_story_image(None), '')
        rendered = news.render_html({'items': [news.public_story(ITEM, CFG, NOW)]}, NOW, CFG)
        self.assertNotIn('<img ', rendered)

    def test_render_responsive_uncropped_and_attributed(self):
        rendered = news.render_html({'items': [news.public_story(dict(ITEM, image=IMAGE), CFG, NOW)]}, NOW, CFG)
        self.assertIn('loading="lazy"', rendered)
        self.assertIn('width:100%;height:auto', rendered)
        self.assertNotIn('object-fit:cover', rendered)
        self.assertIn('CC BY-SA 3.0 IGO', rendered)
        self.assertIn('ESA / DLR', rendered)
        self.assertIn('https://science.nasa.gov/image-article/mars/', rendered)
        self.assertIn('w=1920&amp;h=1200', rendered)

    def test_text_and_url_attributes_escaped(self):
        image = dict(IMAGE, alt='" onerror="alert(1)', caption='<script>alert(1)</script>', credit='A & B')
        rendered = news.render_story_image(image)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('alt="" onerror=', rendered)
        self.assertIn('&quot; onerror=&quot;', rendered)
        self.assertIn('A &amp; B', rendered)

    def test_newest_section_precedes_older_same_day_section(self):
        old = news.public_story(dict(ITEM, story_id='older-id', category='moscow', headline='Старый сюжет', added_at='2026-11-02T10:00:00+03:00'), CFG, NOW)
        new = news.public_story(dict(ITEM, story_id='newer-id', category='space', headline='Новый сюжет', added_at='2026-11-02T18:00:00+03:00'), CFG, NOW)
        rendered = news.render_html({'items': [old, new]}, NOW, CFG)
        self.assertLess(rendered.index('Новый сюжет'), rendered.index('Старый сюжет'))
        self.assertIn('older-id', rendered)
        self.assertIn('newer-id', rendered)

    def test_unsafe_image_or_attribution_url_drops_image(self):
        for field in ('url', 'license_url', 'source_url'):
            for value in ('http://example.org/x', 'javascript:alert(1)', 'https://user:password@example.org/a', 'file:///tmp/x'):
                self.assertIsNone(news.story_image(dict(IMAGE, **{field: value})))
                self.assertEqual(news.render_story_image(dict(IMAGE, **{field: value})), '')
        self.assertIsNone(news.story_image(dict(IMAGE, credit='')))

if __name__ == '__main__':
    unittest.main()
