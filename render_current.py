"""Refresh presentation without pretending the existing content is newer."""
from datetime import datetime
import news


def main():
    cfg = news.read_json('config.json')
    feed = news.read_json('docs/feed.json')
    updated = datetime.fromisoformat(feed['updated_at'])
    page = news.render_html({'title': 'Новости Игоря', 'items': feed['items']}, updated, cfg)
    (news.ROOT / 'docs' / 'index.html').write_text(page, encoding='utf-8')


if __name__ == '__main__':
    main()
