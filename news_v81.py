"""Editorial safety gate for new editions; historical items remain untouched."""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

import news_v8 as v8

CLICKBAIT_RE = re.compile(r'(стало известно|вы не поверите|шокирующ|сенсаци[яи]|вот почему|вот что|раскрыт[аоы]?|названы|назван[ао]?|что случилось|какие .{0,30}нужно|взорвал[ао]? интернет)', re.I)
ROUTINE_RE = re.compile(r'((?:военн\w*|фронтов\w*) сводк[аиу]|военн\w* учени|учени\w* (?:КНДР|Северной Кореи)|обстрел\w*|линия фронта|потери противника|уничтожен\w* (?:танк|дрон)|заявил\w* о готовности)', re.I)
MEME_RE = re.compile(r'(нов\w* мем|мем\w* тренд|viral meme|мем.{0,45}(?:распростран|популяр|набра|тренд)|(?:распростран|популяр).{0,45}мем)', re.I)
MOURNING_RE = re.compile(r'(умер|скончал|погиб|некролог)', re.I)
STATEMENT_RE = re.compile(r'\b(заявил|обвинил|пообещал|призвал|раскритиковал)\w*\b', re.I)
ACTION_RE = re.compile(r'\b(запуст|открыл|принял|подписал|утвердил|увеличил|снизил|вырос|упал|разработал|представил)\w*', re.I)


def clean_rss_title(title: str, source_name: str = '') -> str:
    text = (title or '').strip()
    if source_name:
        text = re.sub(rf'\s+[—-]\s+{re.escape(source_name)}\s*$', '', text, flags=re.I)
    return text.strip()


def is_statement_only(title: str) -> bool:
    return bool(STATEMENT_RE.search(title)) and not bool(ACTION_RE.search(title))


def candidate_is_safe(item, now, cfg):
    title = item.get('title', '')
    try:
        published = datetime.fromisoformat(item.get('published_at', ''))
        if published.tzinfo is None:
            return False
        if not now - timedelta(hours=int(cfg.get('lookback_hours', 8)) + 2) <= published <= now + timedelta(minutes=10):
            return False
    except (ValueError, TypeError):
        return False
    if urlparse(item.get('url', '')).scheme not in {'http', 'https'}:
        return False
    if not item.get('source_domain') or item.get('source_domain') == 'news.google.com':
        return False
    if not title or ROUTINE_RE.search(title) or is_statement_only(title):
        return False
    if item.get('collection_batch') == 'internet_culture':
        return bool(MEME_RE.search(title)) and not MOURNING_RE.search(title)
    return True


_original_rss_candidates = v8.rss_candidates


def rss_candidates(session, batch, domains, cfg, now):
    items = _original_rss_candidates(session, batch, domains, cfg, now)
    filtered = []
    for raw in items:
        item = dict(raw)
        item['title'] = clean_rss_title(item.get('title', ''), item.get('rss_source_name', ''))
        # Google News descriptions may concatenate unrelated linked headlines.
        # Only the feed headline is evidence; never give the editor extra invented detail.
        item['what_happened'] = item['title']
        item['evidence_scope'] = 'rss_headline_only'
        if candidate_is_safe(item, now, cfg):
            filtered.append(item)
    return filtered


def fallback_edition(candidates, cfg, now):
    # An outage must not turn unedited RSS into a published editorial edition.
    return {'title': 'Новости Игоря', 'items': [], 'editor_status': 'unavailable'}


def validate_edition(edition, candidates, cfg, now, allow_meme=True):
    by_url = {c['url']: c for c in candidates if candidate_is_safe(c, now, cfg)}
    accepted, seen = [], set()
    meme_count = 0
    for raw in edition.get('items', []):
        if not isinstance(raw, dict):
            continue
        urls = raw.get('source_urls', [])
        if not isinstance(urls, list) or not urls or any(u not in by_url for u in urls):
            continue
        refs = [by_url[u] for u in urls]
        headline, body = raw.get('headline', '').strip(), raw.get('body', '').strip()
        text = headline + ' ' + body + ' ' + raw.get('aside', '')
        evidence = ' '.join(c['title'] for c in refs)
        if not headline or not body or not re.search('[А-Яа-яЁё]', headline) or not re.search('[А-Яа-яЁё]', body):
            continue
        if CLICKBAIT_RE.search(headline) or '?' in headline or '!' in headline or ROUTINE_RE.search(text):
            continue
        # Numbers may not be invented by the editor. This is conservative, not a full fact checker.
        if set(re.findall(r'\d+(?:[.,]\d+)?', text)) - set(re.findall(r'\d+(?:[.,]\d+)?', evidence)):
            continue
        if len(body) > 700 or any(u in seen for u in urls):
            continue
        meme = bool(raw.get('is_internet_element')) or raw.get('category') in {'memes', 'internet_culture'} or any(c.get('collection_batch') == 'internet_culture' for c in refs)
        if meme and (not MEME_RE.search(evidence) or MOURNING_RE.search(evidence)):
            continue
        if meme and (not allow_meme or meme_count >= 1):
            continue
        item = dict(raw)
        item['is_random_top'] = False
        item['is_internet_element'] = meme
        item['source_names'] = list(dict.fromkeys(c.get('rss_source_name') or c['source_domain'] for c in refs))
        item['source_dates'] = [{'url': c['url'], 'published_at': c['published_at']} for c in refs]
        item['published_at'] = max(c['published_at'] for c in refs)
        accepted.append(item)
        seen.update(urls)
        meme_count += int(meme)
        if len(accepted) >= int(cfg['edition']['max_items']):
            break
    result = dict(edition)
    result['items'] = accepted
    if edition.get('items') and not accepted:
        result['editor_status'] = 'validation_failed'
    else:
        result.setdefault('editor_status', 'success')
    return result


v8.rss_candidates = rss_candidates
v8.fallback_edition = fallback_edition
v8.validate_edition = validate_edition

if __name__ == '__main__':
    raise SystemExit(v8.main())
