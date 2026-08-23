from __future__ import annotations

import argparse
import json
import os
import re
import html
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError

ROOT = Path(__file__).resolve().parent




# The collector can consume ~50-60k TPM per web-search request.  On a 200k TPM
# tier, firing several requests back-to-back creates a burst even though the total
# daily allowance is ample.  Keep starts far enough apart so at most ~3 large
# requests sit in the same rolling minute.
MIN_API_INTERVAL_SECONDS = 25.0
_last_api_call_started = 0.0


def _pace_api_calls(label: str) -> None:
    global _last_api_call_started
    now = time.monotonic()
    remaining = MIN_API_INTERVAL_SECONDS - (now - _last_api_call_started)
    if _last_api_call_started and remaining > 0:
        print(f"API pacing before {label}: waiting {remaining:.1f}s...")
        time.sleep(remaining)
    _last_api_call_started = time.monotonic()


def create_response_with_retry(client: OpenAI, *, label: str, max_retries: int = 7, **kwargs):
    """Call Responses API with pacing and conservative exponential backoff.

    Failed requests also consume rate-limit capacity, so we deliberately wait
    longer than the server's minimum hint instead of immediately hammering it.
    """
    global _last_api_call_started
    for attempt in range(max_retries + 1):
        _pace_api_calls(label)
        try:
            return client.responses.create(**kwargs)
        except RateLimitError as exc:
            if attempt >= max_retries:
                raise
            match = re.search(r"try again in\s+([0-9.]+)s", str(exc), re.IGNORECASE)
            hinted = float(match.group(1)) if match else 0.0
            # A failed attempt itself counts against TPM. Give the rolling window
            # time to drain; the delay grows if the tier remains saturated.
            backoff = min(90.0, 25.0 * (1.55 ** attempt))
            wait = max(hinted + 5.0, backoff)
            print(
                f"Rate limit for {label}; waiting {wait:.1f}s and retrying "
                f"({attempt + 1}/{max_retries})..."
            )
            time.sleep(wait)
            # Do not impose another full pacing delay immediately after backoff.
            _last_api_call_started = time.monotonic() - MIN_API_INTERVAL_SECONDS

def read_json(name: str) -> dict[str, Any]:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def read_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def normalize_domain(url_or_domain: str) -> str:
    value = url_or_domain.strip().lower()
    if "://" in value:
        value = urlparse(value).netloc
    if value.startswith("www."):
        value = value[4:]
    return value.split(":", 1)[0]


def normalize_title(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^a-zа-я0-9]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def source_for_domain(sources: list[dict[str, Any]], domain: str) -> dict[str, Any] | None:
    d = normalize_domain(domain)
    best = None
    best_len = -1
    for source in sources:
        for candidate in source.get("domains", []):
            c = normalize_domain(candidate)
            if d == c or d.endswith("." + c):
                if len(c) > best_len:
                    best = source
                    best_len = len(c)
    return best


def batch_domains(sources: list[dict[str, Any]], batch: str) -> list[str]:
    domains: list[str] = []
    for source in sources:
        if source.get("batch") == batch:
            domains.extend(source.get("domains", []))
    return sorted(set(normalize_domain(d) for d in domains))




def load_state() -> dict[str, Any]:
    path = ROOT / "state.json"
    if not path.exists():
        return {"version": 1, "seen": []}
    return json.loads(path.read_text(encoding="utf-8"))


def recent_history(state: dict[str, Any], cfg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    days = int(cfg.get("memory", {}).get("history_days", 7))
    cutoff = now - timedelta(days=days)
    result = []
    for item in state.get("seen", []):
        try:
            ts = datetime.fromisoformat(item.get("shown_at", ""))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=now.tzinfo)
        except Exception:
            continue
        if ts >= cutoff:
            result.append(item)
    return result


def update_state(state: dict[str, Any], edition: dict[str, Any], cfg: dict[str, Any], now: datetime) -> None:
    seen = list(state.get("seen", []))
    for item in edition.get("items", []):
        seen.append({
            "shown_at": now.isoformat(),
            "headline": item.get("headline", ""),
            "category": item.get("category", "")
        })
    max_items = int(cfg.get("memory", {}).get("max_history_items", 250))
    state["seen"] = seen[-max_items:]
    state["version"] = 1
    (ROOT / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def candidate_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "source_domain": {"type": "string"},
                        "published_at": {"type": "string"},
                        "category": {"type": "string"},
                        "what_happened": {"type": "string"},
                        "why_interesting": {"type": "string"},
                        "is_comment_or_meme": {"type": "boolean"},
                        "comment_or_meme_text": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                    },
                    "required": [
                        "title", "url", "source_domain", "published_at", "category",
                        "what_happened", "why_interesting", "is_comment_or_meme",
                        "comment_or_meme_text", "confidence"
                    ]
                }
            }
        },
        "required": ["items"]
    }


def edition_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "category": {"type": "string"},
                        "headline": {"type": "string"},
                        "body": {"type": "string"},
                        "aside": {"type": "string"},
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                        "source_names": {"type": "array", "items": {"type": "string"}},
                        "importance": {"type": "number", "minimum": 0, "maximum": 10},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1}
                    },
                    "required": [
                        "category", "headline", "body", "aside", "source_urls",
                        "source_names", "importance", "confidence"
                    ]
                }
            }
        },
        "required": ["title", "items"]
    }


def collect_batch(
    client: OpenAI,
    batch_name: str,
    batch_prompt: str,
    domains: list[str],
    cfg: dict[str, Any],
    now: datetime,
) -> list[dict[str, Any]]:
    """Collect one thematic batch and survive truncated structured output.

    Structured Outputs can still be incomplete when max_output_tokens is reached.
    A daily news run should not die because one batch produced a cut-off JSON string,
    so we retry that batch with a smaller requested list and, after a few attempts,
    skip only that batch rather than aborting the whole edition.
    """
    lookback = int(cfg["lookback_hours"])
    since = now - timedelta(hours=lookback)
    configured_max = int(cfg["web_search"]["max_candidates_per_batch"])
    candidate_limit = configured_max
    json_retries = int(cfg["web_search"].get("json_retries", 2))

    for json_attempt in range(json_retries + 1):
        prompt = f"""
Сегодня {now:%Y-%m-%d %H:%M} по часовому поясу {cfg['timezone']}.
Ищи материалы, опубликованные или относящиеся в первую очередь к периоду после {since:%Y-%m-%d %H:%M}.

Редакционная задача этого пакета:
{batch_prompt}

Верни до {candidate_limit} кандидатов. Не добивай список слабыми материалами.
Пиши КОМПАКТНО: what_happened — максимум 2 коротких предложения; why_interesting — максимум 1 короткое предложение; comment_or_meme_text — максимум 1 короткое предложение.
Для каждого кандидата дай реальный URL найденной страницы и ее домен.
Если это Reddit-комментарий или мем, поле is_comment_or_meme=true, а comment_or_meme_text содержит только реально найденную формулировку или очень короткое точное описание мема; ничего не выдумывай.
Если дата на странице неясна, published_at оставь пустой строкой.
""".strip()

        response = create_response_with_retry(
            client,
            label=f"collector:{batch_name}",
            model=cfg["models"]["collector"],
            max_output_tokens=int(cfg["web_search"].get("collector_max_output_tokens", 5000)),
            tools=[{
                "type": "web_search",
                "filters": {"allowed_domains": domains},
                "search_context_size": cfg["web_search"]["search_context_size"]
            }],
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": f"candidate_batch_{batch_name}",
                    "strict": True,
                    "schema": candidate_schema()
                }
            }
        )

        status = getattr(response, "status", "completed")
        if status == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", "unknown") if details else "unknown"
            print(f"Incomplete structured output for {batch_name}: {reason}.")
        else:
            try:
                payload = json.loads(response.output_text)
                return payload.get("items", [])
            except json.JSONDecodeError as exc:
                print(
                    f"Malformed/truncated JSON for {batch_name}: {exc}. "
                    f"Will retry with a smaller candidate list."
                )

        if json_attempt < json_retries:
            candidate_limit = max(5, candidate_limit - 3)
            wait = 20.0
            print(
                f"Retrying {batch_name} structured output in {wait:.0f}s "
                f"with limit {candidate_limit} ({json_attempt + 1}/{json_retries})..."
            )
            time.sleep(wait)
        else:
            print(f"Skipping batch {batch_name}: structured output stayed incomplete after retries.")

    return []


def enrich_candidates(candidates: list[dict[str, Any]], sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in candidates:
        source = source_for_domain(sources, item.get("source_domain", ""))
        if source is None and item.get("url"):
            source = source_for_domain(sources, item["url"])
        item = dict(item)
        if source:
            item["source_id"] = source["id"]
            item["source_name"] = source["name"]
            item["source_role"] = source["role"]
            item["source_weight"] = source.get("editorial_weight", 0.5)
            item["factual_weight"] = source.get("factual_weight", source.get("editorial_weight", 0.5))
        else:
            item["source_id"] = "unknown"
            item["source_name"] = item.get("source_domain", "unknown")
            item["source_role"] = "unknown"
            item["source_weight"] = 0.3
            item["factual_weight"] = 0.3
        out.append(item)
    return out


def dedupe_candidates(candidates: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    # First drop exact URL duplicates, keeping the richer/higher-confidence record.
    by_url: dict[str, dict[str, Any]] = {}
    no_url: list[dict[str, Any]] = []
    for item in candidates:
        url = item.get("url", "").strip()
        if not url:
            no_url.append(item)
            continue
        old = by_url.get(url)
        if old is None or item.get("confidence", 0) > old.get("confidence", 0):
            by_url[url] = item
    pool = list(by_url.values()) + no_url

    # Then collapse near-identical titles. This is deliberately conservative:
    # the editor model gets the remaining overlaps and can merge them semantically.
    kept: list[dict[str, Any]] = []
    for item in sorted(pool, key=lambda x: (x.get("confidence", 0), x.get("source_weight", 0)), reverse=True):
        if any(similarity(item.get("title", ""), k.get("title", "")) >= threshold for k in kept):
            continue
        kept.append(item)
    return kept


def build_source_legend(sources: list[dict[str, Any]]) -> dict[str, Any]:
    legend = {}
    for source in sources:
        legend[source["id"]] = {
            "name": source["name"],
            "role": source["role"],
            "editorial_weight": source.get("editorial_weight", 0.5),
            "factual_weight": source.get("factual_weight", source.get("editorial_weight", 0.5)),
            "notes": source.get("notes", "")
        }
    return legend


def edit_edition(
    client: OpenAI,
    candidates: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    cfg: dict[str, Any],
    policy: str,
    editor_prompt: str,
    now: datetime,
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    edition_cfg = cfg["edition"]
    data = {
        "now": now.isoformat(),
        "timezone": cfg["timezone"],
        "edition": edition_cfg,
        "category_weights": cfg["category_weights"],
        "soft_targets": cfg["soft_targets"],
        "source_legend": build_source_legend(sources),
        "recently_shown": history,
        "candidates": candidates
    }

    prompt = f"""
{editor_prompt}

Ниже редакционная политика:
---
{policy}
---

Собери выпуск из данных ниже.
Цель: около {edition_cfg['target_items']} сюжетов, допустимо от {edition_cfg['min_items']} до {edition_cfg['max_items']}.
Политика — не более {round(edition_cfg['max_politics_share']*100)}% выпуска.
Не более {edition_cfg['max_reddit_items']} сюжетов, где главным интересом является Reddit, и не более {edition_cfg['max_meme_items']} чисто мемного сюжета.
Не включай один и тот же сюжет дважды. Если разные источники описывают одно событие — объедини их и сохрани все полезные URL в source_urls.
Сверься с recently_shown: не повторяй уже показанный сюжет, если не произошло существенного нового развития. Если развитие существенное — можно включить, но текст должен ясно сообщать, что именно изменилось.
В source_names используй человекочитаемые названия источников из source_legend.
Поле aside оставь пустой строкой, если нет реально хорошего комментария, мема или странной детали.

ДАННЫЕ:
{json.dumps(data, ensure_ascii=False)}
""".strip()

    for attempt in range(2):
        response = create_response_with_retry(
            client,
            label="editor",
            model=cfg["models"]["editor"],
            max_output_tokens=9000,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "news_edition",
                    "strict": True,
                    "schema": edition_schema()
                },
                "verbosity": "medium"
            }
        )
        status = getattr(response, "status", "completed")
        if status == "completed":
            try:
                return json.loads(response.output_text)
            except json.JSONDecodeError as exc:
                print(f"Editor returned malformed JSON: {exc}.")
        else:
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", "unknown") if details else "unknown"
            print(f"Editor output incomplete: {reason}.")

        if attempt == 0:
            print("Retrying editor after 30s...")
            time.sleep(30)

    raise RuntimeError("Editor could not produce a complete structured edition after retry.")


def apply_hard_limits(edition: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    # Final cheap guardrails. We do not try to second-guess the editor beyond hard caps.
    items = edition.get("items", [])
    max_items = int(cfg["edition"]["max_items"])
    edition["items"] = items[:max_items]
    return edition


def section_for(category: str) -> str:
    c = category.lower()
    if any(x in c for x in ["russia", "росс", "business", "econom", "бизнес", "эконом"]):
        return "Россия, бизнес и экономика"
    if any(x in c for x in ["world", "polit", "мир", "полит"]):
        return "Мир"
    if any(x in c for x in ["science", "tech", " ai", "наук", "тех", "ии"]):
        return "Наука и технологии"
    if any(x in c for x in ["moscow", "restaurant", "food", "gastr", "product", "моск", "рест", "еда", "продукт"]):
        return "Москва, еда и рестораны"
    if any(x in c for x in ["cinema", "culture", "film", "кино", "культур"]):
        return "Кино и культура"
    if any(x in c for x in ["sport", "chess", "спорт", "шах"]):
        return "Спорт и шахматы"
    if any(x in c for x in ["meme", "internet", "reddit", "мем", "интернет"]):
        return "Интернет"
    return "Еще интересное"


def render_markdown(edition: dict[str, Any], cfg: dict[str, Any]) -> str:
    show_links = bool(cfg["edition"].get("show_links", False))
    show_sources = bool(cfg["edition"].get("show_source_names", False))
    show_sections = bool(cfg["edition"].get("show_section_headers", True))

    lines = [f"# {edition.get('title', 'Новости Игоря')}", ""]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for item in edition.get("items", []):
        section = section_for(item.get("category", ""))
        if section not in grouped:
            order.append(section)
        grouped[section].append(item)

    number = 1
    for section in order:
        if show_sections:
            lines += [f"## {section}", ""]
        for item in grouped[section]:
            lines.append(f"### {number}. {item['headline']}")
            lines.append("")
            lines.append(item["body"].strip())
            aside = item.get("aside", "").strip()
            if aside:
                lines += ["", aside]
            if show_sources and item.get("source_names"):
                lines += ["", "Источники: " + ", ".join(item["source_names"])]
            if show_links and item.get("source_urls"):
                lines += ["", "Ссылки: " + " | ".join(item["source_urls"])]
            lines += ["", ""]
            number += 1
    return "\n".join(lines).rstrip() + "\n"



def render_html(edition: dict[str, Any], now: datetime, archive_mode: bool = False) -> str:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for item in edition.get("items", []):
        section = section_for(item.get("category", ""))
        if section not in grouped:
            order.append(section)
        grouped[section].append(item)

    nav_archive = "index.html" if archive_mode else "archive/index.html"
    nav_home = "../index.html" if archive_mode else "index.html"
    parts: list[str] = []
    number = 1
    for section in order:
        cards: list[str] = []
        for item in grouped[section]:
            aside = item.get("aside", "").strip()
            aside_html = f'<div class="aside">{html.escape(aside)}</div>' if aside else ""
            cards.append(
                f'<article class="story"><div class="num">{number}</div>'
                f'<div class="story-body"><h3>{html.escape(item.get("headline", ""))}</h3>'
                f'<p>{html.escape(item.get("body", ""))}</p>{aside_html}</div></article>'
            )
            number += 1
        parts.append(f'<section><h2>{html.escape(section)}</h2>{"".join(cards)}</section>')

    title = html.escape(edition.get("title", "Новости Игоря"))
    generated = now.strftime("%d.%m.%Y · %H:%M МСК")
    home_link = f'<a href="{nav_home}">Сегодня</a>' if archive_mode else ""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="Персональная новостная лента: сигнал вместо потока.">
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f5; --text:#171717; --muted:#717171; --line:#e7e7e2; --accent:#1f4d3f; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#111; --text:#f3f3f0; --muted:#aaa; --line:#30302e; --accent:#91c9b5; }} }}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.5}}
.wrap{{max-width:820px;margin:0 auto;padding:28px 20px 70px}}
header{{padding:28px 0 22px;border-bottom:1px solid var(--line);margin-bottom:30px}}
h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(36px,7vw,62px);line-height:.98;margin:0 0 14px;letter-spacing:-1.5px}}
.deck{{font-size:17px;color:var(--muted);margin:0 0 12px}}
.meta{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted)}} .meta a{{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}}
section{{margin:38px 0}} h2{{font-size:14px;letter-spacing:.08em;text-transform:uppercase;margin:0 0 8px;color:var(--accent)}}
.story{{display:grid;grid-template-columns:34px 1fr;gap:10px;padding:22px 0;border-top:1px solid var(--line)}} .num{{font-family:Georgia,serif;font-size:16px;color:var(--muted);padding-top:4px}}
h3{{font-family:Georgia,"Times New Roman",serif;font-size:25px;line-height:1.16;margin:0 0 9px;letter-spacing:-.25px}} p{{margin:0;font-size:17px}}
.aside{{margin-top:12px;padding-left:13px;border-left:3px solid var(--accent);font-size:15px;color:var(--muted)}} footer{{border-top:1px solid var(--line);padding-top:22px;margin-top:48px;font-size:13px;color:var(--muted)}}
@media(max-width:560px){{.wrap{{padding:18px 16px 50px}} header{{padding-top:20px}} .story{{grid-template-columns:28px 1fr}} h3{{font-size:22px}} p{{font-size:16px}}}}
</style>
</head>
<body><main class="wrap">
<header><h1>{title}</h1><p class="deck">Сигнал вместо потока. Только то, на чём стоило остановиться.</p>
<div class="meta"><span>{generated}</span><a href="{nav_archive}">Архив выпусков</a>{home_link}</div></header>
{"".join(parts)}
<footer>Новости Игоря · автоматическая персональная редакция</footer>
</main></body></html>"""


def render_archive_index(docs_dir: Path) -> str:
    files = sorted((docs_dir / "archive").glob("*.html"), reverse=True)
    items: list[str] = []
    for f in files:
        if f.name == "index.html":
            continue
        label = f.stem
        try:
            label = datetime.strptime(label, "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            pass
        items.append(f'<li><a href="{html.escape(f.name)}">{html.escape(label)}</a></li>')
    listing = "".join(items) or "<li>Пока нет сохранённых выпусков.</li>"
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Архив · Новости Игоря</title>
<style>body{{max-width:760px;margin:40px auto;padding:0 20px;font:17px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}h1{{font:42px/1 Georgia,serif}}a{{color:inherit}}li{{margin:10px 0}}</style></head><body><a href="../index.html">← Сегодня</a><h1>Архив выпусков</h1><ul>{listing}</ul></body></html>"""

def save_outputs(edition: dict[str, Any], candidates: list[dict[str, Any]], cfg: dict[str, Any], now: datetime) -> tuple[Path, Path]:
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    docs_dir = ROOT / "docs"
    archive_dir = docs_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime("%Y-%m-%d_%H%M")
    day = now.strftime("%Y-%m-%d")
    md_path = out_dir / f"digest_{stamp}.md"
    json_path = out_dir / f"digest_{stamp}.json"

    md_path.write_text(render_markdown(edition, cfg), encoding="utf-8")
    archive = {
        "generated_at": now.isoformat(),
        "edition": edition,
        "candidate_count": len(candidates),
        "candidates": candidates
    }
    json_path.write_text(json.dumps(archive, ensure_ascii=False, indent=2), encoding="utf-8")

    # Public Pages files never contain source URLs or internal candidate data.
    (docs_dir / "index.html").write_text(render_html(edition, now, archive_mode=False), encoding="utf-8")
    (archive_dir / f"{day}.html").write_text(render_html(edition, now, archive_mode=True), encoding="utf-8")
    (archive_dir / "index.html").write_text(render_archive_index(docs_dir), encoding="utf-8")
    (docs_dir / ".nojekyll").touch()
    return md_path, json_path


def source_health(candidates: list[dict[str, Any]]) -> str:
    counts = Counter(c.get("source_name", "unknown") for c in candidates)
    top = ", ".join(f"{name}: {count}" for name, count in counts.most_common(8))
    return top or "нет кандидатов"


def main() -> int:
    parser = argparse.ArgumentParser(description="Персональный выпуск 'Новости Игоря'")
    parser.add_argument("--dry-run", action="store_true", help="Проверить конфиги без вызова API")
    parser.add_argument("--batch", action="append", help="Запустить только указанный collection batch; можно повторять")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = read_json("config.json")
    source_doc = read_json("sources.json")
    sources = source_doc["sources"]
    policy = read_text("editorial_policy.md")
    editor_prompt = read_text("prompts/editor.md")
    state = load_state()

    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)

    # Validate batches and domains before spending any API calls.
    batches = cfg["collection_batches"]
    selected = args.batch or list(batches.keys())
    for batch in selected:
        if batch not in batches:
            raise SystemExit(f"Неизвестный batch: {batch}")
        domains = batch_domains(sources, batch)
        if not domains:
            raise SystemExit(f"У batch {batch} нет доменов в sources.json")
        print(f"{batch}: {len(domains)} domains")

    if args.dry_run:
        print("Конфиги валидны. API не вызывался.")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Нет OPENAI_API_KEY. Скопируйте .env.example в .env и добавьте ключ.")

    client = OpenAI()
    candidates: list[dict[str, Any]] = []
    for batch in selected:
        domains = batch_domains(sources, batch)
        print(f"Собираю {batch}...")
        items = collect_batch(client, batch, batches[batch]["prompt"], domains, cfg, now)
        for item in items:
            item["collection_batch"] = batch
        candidates.extend(items)
        print(f"  найдено: {len(items)}")

    candidates = enrich_candidates(candidates, sources)
    candidates = dedupe_candidates(candidates, float(cfg["dedupe"]["title_similarity_threshold"]))
    print(f"После дедупликации: {len(candidates)}")
    print("Источники:", source_health(candidates))

    history = recent_history(state, cfg, now)
    print(f"История за последние {cfg.get('memory', {}).get('history_days', 7)} дн.: {len(history)} сюжетов")
    edition = edit_edition(client, candidates, sources, cfg, policy, editor_prompt, now, history)
    edition = apply_hard_limits(edition, cfg)
    md_path, json_path = save_outputs(edition, candidates, cfg, now)
    update_state(state, edition, cfg, now)

    print(f"Готово: {md_path}")
    print(f"Архив: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
