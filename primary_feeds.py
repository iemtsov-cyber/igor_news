"""Verified public publisher feeds, no article crawling or paid APIs.

URLs fetched successfully on 2026-11-02. Summaries are publisher claims,
not independent verification; they remain untrusted evidence for the editor.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET


FEEDS = (
    {'url': 'https://www.nasa.gov/feed/', 'domain': 'nasa.gov', 'name': 'NASA', 'batch': 'space'},
    {'url': 'https://www.esa.int/rssfeed/Our_Activities/Space_Science', 'domain': 'esa.int', 'name': 'ESA', 'batch': 'space'},
    {'url': 'https://openai.com/news/rss.xml', 'domain': 'openai.com', 'name': 'OpenAI', 'batch': 'ai'},
    {'url': 'https://news.mit.edu/rss/topic/artificial-intelligence2', 'domain': 'news.mit.edu', 'name': 'MIT News', 'batch': 'ai'},
)
MAX_FEED_BYTES = 2_000_000
ATOM = '{http://www.w3.org/2005/Atom}'
DC_DATE = '{http://purl.org/dc/elements/1.1/}date'


class SummaryText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.hidden += 1
        elif tag in ('p', 'br', 'div', 'li') and not self.hidden:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ('p', 'div', 'li') and not self.hidden:
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_summary(value: str, limit: int = 1200) -> str:
    parser = SummaryText()
    parser.feed(value or '')
    text = ' '.join(''.join(parser.parts).split())
    if len(text) > limit:
        text = text[:limit - 1].rsplit(' ', 1)[0] + '…'
    return text


def official_url(url: str, domain: str) -> bool:
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        return (parsed.scheme == 'https' and not parsed.username and not parsed.password
                and parsed.port in (None, 443)
                and (host == domain or host.endswith('.' + domain)))
    except ValueError:
        return False


def publication_date(value: str, now: datetime, cfg: dict):
    try:
        try:
            date = parsedate_to_datetime(value)
        except (ValueError, TypeError):
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        cutoff = now - timedelta(hours=int(cfg.get('lookback_hours', 8)) + 2)
        return date if cutoff <= date <= now + timedelta(minutes=10) else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def parse_feed(content: bytes, feed: dict, cfg: dict, now: datetime) -> list[dict]:
    if len(content) > MAX_FEED_BYTES or b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise ValueError('unsafe_or_oversized_feed')
    root = ET.fromstring(content)
    is_atom = root.tag == ATOM + 'feed'
    nodes = root.findall(ATOM + 'entry') if is_atom else root.findall('./channel/item')
    results, seen = [], set()
    limit = max(0, min(30, int(cfg.get('web_search', {}).get('max_candidates_per_batch', 12))))
    for node in nodes:
        if len(results) >= limit:
            break
        if is_atom:
            title = node.findtext(ATOM + 'title') or ''
            links = node.findall(ATOM + 'link')
            url = next((link.get('href', '') for link in links if link.get('rel', 'alternate') == 'alternate'), '')
            # updated is not evidence of initial publication: do not relabel old news.
            raw_date = node.findtext(ATOM + 'published') or ''
            summary = node.findtext(ATOM + 'summary') or ''
        else:
            title = node.findtext('title') or ''
            url = (node.findtext('link') or '').strip()
            raw_date = node.findtext('pubDate') or node.findtext(DC_DATE) or ''
            summary = node.findtext('description') or ''
        title = plain_summary(title, 300)
        summary = plain_summary(summary)
        published = publication_date(raw_date, now, cfg)
        if not title or not published or not official_url(url, feed['domain']) or url in seen:
            continue
        # Some NASA APOD entries put a whole navigation page in description.
        # Skip rather than silently present menu text as article evidence.
        if len(summary) < 40 or re.search(r'Archive Submissions Index Search Calendar|skip to (?:main )?content|enable javascript', summary, re.I):
            continue
        seen.add(url)
        results.append({
            'title': title, 'url': url, 'published_at': published.isoformat(),
            'source_domain': urlsplit(url).hostname.lower(),
            'source_name': feed['name'], 'rss_source_name': feed['name'],
            'category': feed['batch'], 'collection_batch': feed['batch'],
            'what_happened': summary, 'evidence_scope': 'publisher_feed_summary',
            'is_comment_or_meme': False, 'comment_or_meme_text': '',
            'confidence': 0.8, 'why_interesting': '',
        })
    return results


def collect_primary(session, batch, cfg, now):
    """Fetch at most the static allowlist; caller owns selection/publication."""
    feeds = [feed for feed in FEEDS if feed['batch'] == batch]
    items, successful = [], 0
    for feed in feeds:
        try:
            response = session.get(feed['url'], timeout=(5, 20))
            response.raise_for_status()
            if not official_url(response.url, feed['domain']):
                raise ValueError('off_publisher_redirect')
            items.extend(parse_feed(response.content, feed, cfg, now))
            successful += 1
        except Exception as exc:
            # No exception text: it can include redirects, payloads or headers.
            print(f"Primary feed {feed['name']}: {type(exc).__name__}")
    if feeds and not successful:
        raise RuntimeError('all_primary_feeds_failed')
    return items
