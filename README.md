# Новости Игоря

Персональная новостная лента: сбор свежих материалов → редакционный отбор → чистый ежедневный выпуск → GitHub Pages.

## Как это работает

1. `sources.json` задаёт разрешённые источники, их роль и вес.
2. `config.json` задаёт баланс рубрик и ограничения выпуска.
3. `news.py` делает тематические web-search проходы через OpenAI Responses API.
4. Кандидаты дедуплицируются и проходят редакторский отбор по `editorial_policy.md`.
5. Внутренние результаты пишутся локально в `output/` и не коммитятся.
6. Публичный сайт генерируется в `docs/` без URL источников и без внутренних JSON.
7. `state.json` хранит только заголовки уже показанных сюжетов, чтобы не повторяться.
8. GitHub Actions запускает выпуск ежедневно и деплоит `docs/` в GitHub Pages.

## Первый запуск на GitHub

### 1. Добавьте файлы проекта в репозиторий

Загрузите содержимое этого проекта в корень репозитория.

### 2. Добавьте секрет OpenAI

GitHub → repository `Settings` → `Secrets and variables` → `Actions` → `New repository secret`.

Имя:

`OPENAI_API_KEY`

Значение — ваш OpenAI API key.

### 3. Включите GitHub Pages

GitHub → `Settings` → `Pages` → `Build and deployment` → `Source` → `GitHub Actions`.

### 4. Запустите первый выпуск вручную

GitHub → `Actions` → `Daily News + GitHub Pages` → `Run workflow`.

После успешного запуска сайт будет доступен по адресу вида:

`https://<username>.github.io/<repository>/`

Для этого репозитория ожидаемый адрес:

`https://iemtsov-cyber.github.io/igor_news/`

## Расписание

Workflow запускается ежедневно в 07:00 по Москве (04:00 UTC).

Файл расписания:

`.github/workflows/daily-news.yml`

## Локальная проверка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python news.py --dry-run
python news.py
```

## Безопасность

- `.env` не коммитится.
- `OPENAI_API_KEY` хранится только в GitHub Secrets.
- `output/` не коммитится.
- публичные HTML-страницы не содержат внутренних URL и полного списка кандидатов.
- `state.json` не содержит URL источников.


## Rate-limit pacing

The collector intentionally spaces OpenAI API calls by 25 seconds and uses conservative exponential backoff on HTTP 429. This is designed for a 200k TPM tier while preserving all six editorial collection passes.


## v4 reliability
Collector batches request smaller candidate lists and retry incomplete/malformed structured JSON. One bad batch is skipped instead of aborting the whole daily edition.
