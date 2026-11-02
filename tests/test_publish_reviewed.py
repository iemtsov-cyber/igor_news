import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import news
import publish_reviewed as reviewed

NOW = datetime.fromisoformat('2026-11-02T18:00:00+03:00')
CFG = {'timezone': 'Europe/Moscow', 'edition': {}, 'dedupe': {'title_similarity_threshold': .99}, 'memory': {'max_history_items': 2000}}
ITEM = {'story_id': 'reviewed-20261102-1', 'headline': 'Космический телескоп исследовал атмосферу', 'body': 'Подтверждённое описание результата.', 'category': 'space', 'published_at': '2026-11-02T10:00:00Z', 'source_urls': ['https://example.org/space']}
DOC = {'reviewed': True, 'reviewed_at': NOW.isoformat(), 'items': [ITEM]}


class ReviewedPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch = patch.object(news, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Offline Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'config.json').write_text(json.dumps(CFG))
        (self.root / 'docs').mkdir()
        self.old = [{'story_id': f'old-{i}', 'headline': f'Архивный материал номер {i}', 'body': 'Исторический текст', 'category': 'technology', 'added_at': '2026-10-01T10:00:00+03:00'} for i in range(873)]
        self.old = [news.public_story(x, CFG, NOW) for x in self.old]
        (self.root / 'docs/feed.json').write_text(json.dumps({'updated_at': '2026-10-01T10:00:00+03:00', 'items': self.old}))
        (self.root / 'docs/index.html').write_text('OLD PAGE')
        (self.root / 'state.json').write_text(json.dumps({'version': 1, 'seen': []}))
        (self.root / 'reviewed_editions').mkdir()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def commit_doc(self, doc=DOC, name='edition.json'):
        (self.root / 'reviewed_editions' / name).write_text(json.dumps(doc))
        self.git('add', '.')
        self.git('commit', '-qm', 'Offline reviewed fixture')

    def test_publish_preserves_all_history_and_is_idempotent(self):
        self.commit_doc()
        self.assertEqual(len(reviewed.publish_reviewed(now=NOW)), 1)
        feed = json.loads((self.root / 'docs/feed.json').read_text())
        self.assertEqual(len(feed['items']), 874)
        by_id = {item['story_id']: item for item in feed['items']}
        for old in self.old:
            self.assertEqual(by_id[old['story_id']], old)
        before = {path: (self.root / path).read_bytes() for path in ('docs/feed.json', 'docs/index.html', 'state.json')}
        self.assertEqual(reviewed.publish_reviewed(now=NOW.replace(hour=19)), [])
        for path, contents in before.items():
            self.assertEqual((self.root / path).read_bytes(), contents)

    def test_all_files_validate_before_any_writes(self):
        self.commit_doc()
        self.commit_doc(dict(DOC, reviewed=False), 'bad.json')
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        with self.assertRaises(reviewed.ReviewValidationError):
            reviewed.publish_reviewed(now=NOW)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.git' not in p.parts}
        self.assertEqual(before, after)

    def test_staging_and_uncommitted_approval_never_read(self):
        self.commit_doc()
        (self.root / 'reviewed_editions/edition.json').write_text('INVALID WORKTREE')
        (self.root / 'staging').mkdir()
        (self.root / 'staging/evil.json').write_text('INVALID STAGING')
        self.assertEqual(len(reviewed.publish_reviewed(now=NOW)), 1)

    def test_validation_dates_ids_and_limits(self):
        for mutate in (lambda d: d.update(reviewed=1), lambda d: d.update(reviewed_at='2026-11-02'),
                       lambda d: d.update(items=[]), lambda d: d.update(items=[ITEM] * 16),
                       lambda d: d['items'][0].update(source_urls=['javascript:alert(1)']),
                       lambda d: d['items'][0].update(published_at='bad'),
                       lambda d: d['items'][0].update(story_id='bad"id')):
            doc = copy.deepcopy(DOC)
            mutate(doc)
            with self.assertRaises(reviewed.ReviewValidationError):
                reviewed.validate_reviewed(doc)
        date_only = copy.deepcopy(DOC)
        date_only['items'][0]['published_at'] = '2026-11-02'
        self.assertEqual(len(reviewed.validate_reviewed(date_only)), 1)

if __name__ == '__main__':
    unittest.main()
