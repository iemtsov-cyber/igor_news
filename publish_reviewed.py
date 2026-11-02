"""Publish explicitly reviewed, committed editions. Never read staging/ or call APIs."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import news


class ReviewValidationError(ValueError):
    pass


def valid_date(value, *, aware_only=False):
    if not isinstance(value, str):
        return False
    try:
        if not aware_only and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            date.fromisoformat(value)
            return True
        return datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def validate_reviewed(document):
    if not isinstance(document, dict) or document.get('reviewed') is not True:
        raise ReviewValidationError('explicit_review_required')
    if not valid_date(document.get('reviewed_at'), aware_only=True):
        raise ReviewValidationError('invalid_review_timestamp')
    items = document.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 15:
        raise ReviewValidationError('reviewed_edition_requires_1_to_15_items')
    ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ReviewValidationError('invalid_item')
        sid = item.get('story_id')
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', sid) or sid in ids:
            raise ReviewValidationError('invalid_or_duplicate_story_id')
        ids.add(sid)
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ('headline', 'body', 'category')):
            raise ReviewValidationError('missing_story_text')
        urls = item.get('source_urls')
        if not isinstance(urls, list) or not urls or any(not news.safe_source_url(url) for url in urls):
            raise ReviewValidationError('invalid_sources')
        if not valid_date(item.get('published_at')):
            raise ReviewValidationError('invalid_publication_date')
        for key in ('source_names', 'preference_tags'):
            if key in item and (not isinstance(item[key], list) or any(not isinstance(v, str) for v in item[key])):
                raise ReviewValidationError('invalid_story_list')
        if 'aside' in item and not isinstance(item['aside'], str):
            raise ReviewValidationError('invalid_aside')
        for key in ('is_internet_element', 'is_random_top'):
            if key in item and not isinstance(item[key], bool):
                raise ReviewValidationError('invalid_story_flag')
        if 'added_at' in item and not valid_date(item['added_at'], aware_only=True):
            raise ReviewValidationError('invalid_added_at')
        if 'source_dates' in item:
            records = item['source_dates']
            if not isinstance(records, list) or any(not isinstance(record, dict) or record.get('url') not in urls or not valid_date(record.get('published_at')) for record in records):
                raise ReviewValidationError('invalid_source_dates')
    return items


def committed_reviewed_documents(root: Path):
    """Use HEAD blobs, not working-tree files, symlinks, or uncommitted approvals."""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], stderr=subprocess.DEVNULL)
    listing = git('ls-tree', '-r', '-z', 'HEAD', '--', 'reviewed_editions/')
    documents = []
    for entry in listing.split(b'\0'):
        if not entry:
            continue
        metadata, path_bytes = entry.split(b'\t', 1)
        mode, kind, _ = metadata.split()
        path = path_bytes.decode('utf-8')
        if not re.fullmatch(r'reviewed_editions/[^/]+\.json', path):
            continue
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ReviewValidationError('review_file_must_be_regular')
        document = json.loads(git('show', 'HEAD:' + path))
        documents.append(document)
    return documents


def publish_reviewed(root=None, now=None):
    root = Path(root or news.ROOT)
    # All files must validate before any output, directory or state is written.
    documents = committed_reviewed_documents(root)
    items = []
    seen = {}
    for document in documents:
        for item in validate_reviewed(document):
            sid = item['story_id']
            if sid in seen and item != seen[sid]:
                raise ReviewValidationError('conflicting_reviewed_story_id')
            if sid not in seen:
                seen[sid] = item
                items.append(item)
    if not items:
        return []
    if root.resolve() != news.ROOT.resolve():
        raise ReviewValidationError('news_root_mismatch')
    cfg = news.read_json('config.json')
    now = now or datetime.now(ZoneInfo(cfg.get('timezone', 'Europe/Moscow')))
    state = news.load_state()
    existing = news.load_master_feed(cfg, now)
    edition = {'title': 'Новости Игоря', 'items': items, 'editor_status': 'review_required'}
    _, _, _, added = news.save_outputs(edition, [], cfg, now, existing)
    if added:
        news.update_state(state, {'items': added}, cfg, now)
    return added


def main():
    added = publish_reviewed()
    print(f'Reviewed publication: {len(added)} new items')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
