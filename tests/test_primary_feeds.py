import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
import primary_feeds as p

NOW = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
CFG = {'lookback_hours': 8}
FEED = p.FEEDS[0]
DESCRIPTION = 'NASA tested a new instrument for observing the atmosphere of distant planets.'


def rss(date='Mon, 02 Nov 2026 12:00:00 +0000', url='https://science.nasa.gov/article', description=DESCRIPTION):
    return f'<rss><channel><item><title>New instrument</title><link>{url}</link><pubDate>{date}</pubDate><description><![CDATA[{description}]]></description></item></channel></rss>'.encode()


class PrimaryFeedTests(unittest.TestCase):
    def test_rss_publisher_summary(self):
        result = p.parse_feed(rss(), FEED, CFG, NOW)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['what_happened'], DESCRIPTION)
        self.assertEqual(result[0]['evidence_scope'], 'publisher_feed_summary')
        self.assertEqual(result[0]['published_at'], '2026-11-02T12:00:00+00:00')

    def test_atom_publication_date_not_updated(self):
        atom = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Science</title><link href="https://www.nasa.gov/article"/><published>2026-11-02T12:00:00Z</published><summary>{DESCRIPTION}</summary></entry></feed>'
        self.assertEqual(len(p.parse_feed(atom.encode(), FEED, CFG, NOW)), 1)
        self.assertEqual(p.parse_feed(atom.replace('published', 'updated').encode(), FEED, CFG, NOW), [])

    def test_dates_fail_closed(self):
        for date in ('', 'bad', '2026-11-02T12:00:00', '2026-10-30T12:00:00Z', '2026-11-02T14:11:00Z'):
            self.assertEqual(p.parse_feed(rss(date=date), FEED, CFG, NOW), [])

    def test_publisher_domain_and_scheme(self):
        for url in ('http://www.nasa.gov/article', 'https://nasa.gov.evil.test/a',
                    'https://user:password@www.nasa.gov/a', 'https://www.nasa.gov:444/a', 'javascript:alert(1)', 'https://[bad'):
            self.assertEqual(p.parse_feed(rss(url=url), FEED, CFG, NOW), [])

    def test_html_removed_and_bounded(self):
        summary = p.plain_summary('<script>SECRET</script><p>' + DESCRIPTION + '</p><img src="x">' + ' detail' * 400)
        self.assertNotIn('SECRET', summary)
        self.assertNotIn('<', summary)
        self.assertLessEqual(len(summary), 1200)
        self.assertIn(DESCRIPTION, summary)

    def test_no_navigation_or_thin_evidence(self):
        for description in ('short', 'Archive Submissions Index Search Calendar Navigation on this site'):
            self.assertEqual(p.parse_feed(rss(description=description), FEED, CFG, NOW), [])

    def test_entities_rejected(self):
        with self.assertRaises(ValueError):
            p.parse_feed(b'<!DOCTYPE rss><rss/>', FEED, CFG, NOW)

    def test_mocked_collection_no_article_requests(self):
        session = Mock()
        def get(url, **kwargs):
            response = Mock(url=url, content=rss())
            if 'esa.int' in url:
                response.content = rss(url='https://www.esa.int/article')
            return response
        session.get.side_effect = get
        result = p.collect_primary(session, 'space', CFG, NOW)
        self.assertEqual(len(result), 2)
        self.assertEqual(session.get.call_count, 2)
        for call in session.get.call_args_list:
            self.assertIn(call.args[0], [f['url'] for f in p.FEEDS])
            self.assertEqual(call.kwargs['timeout'], (5, 20))

    def test_all_fetch_failures_are_explicit(self):
        session = Mock()
        session.get.side_effect = RuntimeError('private detail')
        with self.assertRaisesRegex(RuntimeError, 'all_primary_feeds_failed'):
            p.collect_primary(session, 'ai', CFG, NOW)

    def test_off_publisher_redirect_rejected(self):
        session = Mock()
        session.get.return_value = Mock(url='https://other.test/', content=rss())
        with self.assertRaisesRegex(RuntimeError, 'all_primary_feeds_failed'):
            p.collect_primary(session, 'space', CFG, NOW)

if __name__ == '__main__':
    unittest.main()
