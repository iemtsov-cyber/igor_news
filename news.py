from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
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
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "preference_tags": {"type": "array", "items": {"type": "string"}},
                        "is_random_top": {"type": "boolean"},
                        "is_internet_element": {"type": "boolean"}
                    },
                    "required": [
                        "category", "headline", "body", "aside", "source_urls",
                        "source_names", "importance", "confidence", "preference_tags",
                        "is_random_top", "is_internet_element"
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
            tools=[({
                "type": "web_search",
                "search_context_size": cfg["web_search"]["search_context_size"],
                **({"filters": {"allowed_domains": domains}} if domains else {})
            })],
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
    require_internet_element: bool = False,
    require_random_top: bool = False,
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
        "require_internet_element": require_internet_element,
        "require_random_top": require_random_top,
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
Если require_internet_element=true, обязательно включи минимум один лёгкий интернет-элемент из найденных кандидатов; если false — включай только если он действительно хорош. Предпочтительно встроить его в aside обычного сюжета; не создавай отдельную рубрику ради мема. Ничего не выдумывай.
Если require_random_top=true, обязательно включи ровно один сюжет из collection_batch=random_top; если false — не добавляй новый random_top. Это контролируемая случайность, а не основной редакционный приоритет.
Не включай один и тот же сюжет дважды. Если разные источники описывают одно событие — объедини их и сохрани все полезные URL в source_urls.
Сверься с recently_shown: не повторяй уже показанный сюжет, если не произошло существенного нового развития. Если развитие существенное — можно включить, но текст должен ясно сообщать, что именно изменилось.
В source_names используй человекочитаемые названия источников из source_legend; для random_top с source_id=unknown можно использовать source_name/source_domain самого кандидата.
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
    return "Еще интересное"


def category_from_section(section: str) -> str:
    s = section.lower()
    if "россия" in s or "бизнес" in s:
        return "russia"
    if s.strip() == "мир":
        return "world"
    if "наука" in s or "технолог" in s:
        return "technology"
    if "москва" in s or "еда" in s or "ресторан" in s:
        return "moscow"
    if "кино" in s or "культур" in s:
        return "culture"
    if "спорт" in s or "шах" in s:
        return "sports"
    return "internet_culture"


def stable_story_id(item: dict[str, Any]) -> str:
    seed = normalize_title(item.get("headline", "")) + "|" + normalize_title(item.get("body", ""))[:240]
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def normalize_tags(item: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    raw = item.get("preference_tags", []) or []
    tags: list[str] = []
    for value in raw:
        tag = " ".join(str(value).strip().lower().split())
        if tag and tag not in tags:
            tags.append(tag)
    category = " ".join(str(item.get("category", "")).strip().lower().split())
    if category and category not in tags:
        tags.insert(0, category)
    limit = int(cfg.get("personalization", {}).get("max_tags_per_story", 6))
    return tags[:limit]


def public_story(item: dict[str, Any], cfg: dict[str, Any], now: datetime) -> dict[str, Any]:
    clean = {
        "category": item.get("category", "other"),
        "headline": item.get("headline", "").strip(),
        "body": item.get("body", "").strip(),
        "aside": item.get("aside", "").strip(),
        "importance": float(item.get("importance", 5)),
        "confidence": float(item.get("confidence", 0.7)),
        "preference_tags": normalize_tags(item, cfg),
        "is_random_top": bool(item.get("is_random_top", False)),
        "is_internet_element": bool(item.get("is_internet_element", False)),
        "added_at": item.get("added_at") or now.isoformat(),
    }
    clean["story_id"] = item.get("story_id") or stable_story_id(clean)
    return clean


def parse_legacy_html(path: Path, cfg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Migrate the last v5 page once, so the first v6 update does not erase it."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    result: list[dict[str, Any]] = []
    section_re = re.compile(r"<section><h2>(.*?)</h2>(.*?)</section>", re.S | re.I)
    story_re = re.compile(
        r'<article class="story">.*?<h3>(.*?)</h3>\s*<p>(.*?)</p>(?:<div class="aside">(.*?)</div>)?.*?</article>',
        re.S | re.I,
    )
    for section_html, body_html in section_re.findall(text):
        section = html.unescape(re.sub(r"<.*?>", "", section_html)).strip()
        category = category_from_section(section)
        for headline_html, body_text_html, aside_html in story_re.findall(body_html):
            headline = html.unescape(re.sub(r"<.*?>", "", headline_html)).strip()
            body = html.unescape(re.sub(r"<.*?>", "", body_text_html)).strip()
            aside = html.unescape(re.sub(r"<.*?>", "", aside_html)).strip() if aside_html else ""
            if not headline:
                continue
            item = {
                "category": category,
                "headline": headline,
                "body": body,
                "aside": aside,
                "preference_tags": [category],
                "is_random_top": False,
                "is_internet_element": bool(aside) or category == "internet_culture",
                "added_at": now.isoformat(),
                "importance": 5,
                "confidence": 0.7,
            }
            result.append(public_story(item, cfg, now))
    return result


def load_master_feed(cfg: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    docs = ROOT / "docs"
    path = docs / "feed.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [public_story(x, cfg, now) for x in payload.get("items", [])]
        except Exception as exc:
            print(f"Не удалось прочитать docs/feed.json: {exc}. Пробую миграцию HTML.")
    return parse_legacy_html(docs / "index.html", cfg, now)


def story_day(item: dict[str, Any], tz: ZoneInfo) -> str:
    try:
        dt = datetime.fromisoformat(item.get("added_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz).strftime("%Y-%m-%d")
    except Exception:
        return ""


def merge_feed(existing: list[dict[str, Any]], new_items: list[dict[str, Any]], cfg: dict[str, Any], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    threshold = float(cfg["dedupe"]["title_similarity_threshold"])
    merged = list(existing)
    added: list[dict[str, Any]] = []
    for raw in new_items:
        item = public_story(raw, cfg, now)
        if any(similarity(item["headline"], old.get("headline", "")) >= threshold for old in merged):
            continue
        merged.append(item)
        added.append(item)
    return merged, added


def fallback_story(candidate: dict[str, Any], cfg: dict[str, Any], now: datetime, *, random_top: bool = False, internet: bool = False) -> dict[str, Any]:
    body_parts = [candidate.get("what_happened", "").strip(), candidate.get("why_interesting", "").strip()]
    body = " ".join(x for x in body_parts if x)
    return {
        "category": candidate.get("category", "random_top" if random_top else "internet_culture"),
        "headline": candidate.get("title", "").strip(),
        "body": body,
        "aside": candidate.get("comment_or_meme_text", "").strip() if internet else "",
        "source_urls": [candidate.get("url", "")] if candidate.get("url") else [],
        "source_names": [candidate.get("source_name", candidate.get("source_domain", ""))],
        "importance": 5.5,
        "confidence": float(candidate.get("confidence", 0.65)),
        "preference_tags": [candidate.get("category", "other"), "случайно из топа" if random_top else "интернет-культура"],
        "is_random_top": random_top,
        "is_internet_element": internet,
        "added_at": now.isoformat(),
    }


def enforce_required_specials(
    edition: dict[str, Any],
    candidates: list[dict[str, Any]],
    cfg: dict[str, Any],
    now: datetime,
    require_random_top: bool,
    require_internet_element: bool,
) -> dict[str, Any]:
    items = list(edition.get("items", []))
    max_items = int(cfg["edition"]["max_items"])

    def room_for(item: dict[str, Any]) -> None:
        nonlocal items
        if len(items) >= max_items:
            for idx in range(len(items) - 1, -1, -1):
                if not items[idx].get("is_random_top") and not items[idx].get("is_internet_element"):
                    items.pop(idx)
                    break
        if len(items) < max_items:
            items.append(item)

    if require_random_top and not any(x.get("is_random_top") for x in items):
        pool = [c for c in candidates if c.get("collection_batch") == "random_top"]
        if pool:
            room_for(fallback_story(pool[0], cfg, now, random_top=True))

    if require_internet_element and not any(x.get("is_internet_element") for x in items):
        pool = [c for c in candidates if c.get("collection_batch") == "internet_culture" or c.get("is_comment_or_meme")]
        if pool:
            room_for(fallback_story(pool[0], cfg, now, internet=True))

    edition["items"] = items
    return edition


def render_markdown(edition: dict[str, Any], cfg: dict[str, Any]) -> str:
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
            lines += [f"### {number}. {item['headline']}", "", item.get("body", "").strip()]
            if item.get("aside", "").strip():
                lines += ["", item["aside"].strip()]
            lines += ["", ""]
            number += 1
    return "\n".join(lines).rstrip() + "\n"


def render_html(edition: dict[str, Any], now: datetime, cfg: dict[str, Any], archive_mode: bool = False) -> str:
    tz = ZoneInfo(cfg["timezone"])
    items = list(edition.get("items", []))
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_day[story_day(item, tz) or now.strftime("%Y-%m-%d")].append(item)
    day_order = sorted(by_day.keys(), reverse=True)

    nav_archive = "index.html" if archive_mode else "archive/index.html"
    nav_home = "../index.html" if archive_mode else "index.html"
    parts: list[str] = []
    number = 1
    for day in day_order:
        day_items = by_day[day]
        if not archive_mode or len(day_order) > 1:
            try:
                day_label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
            except ValueError:
                day_label = day
            parts.append(f'<div class="day-divider"><span>{html.escape(day_label)}</span></div>')
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        section_order: list[str] = []
        for item in day_items:
            section = section_for(item.get("category", ""))
            if section not in grouped:
                section_order.append(section)
            grouped[section].append(item)
        for section in section_order:
            cards: list[str] = []
            group_items = sorted(grouped[section], key=lambda x: x.get("added_at", ""), reverse=True)
            for item in group_items:
                aside = item.get("aside", "").strip()
                aside_html = f'<div class="aside">{html.escape(aside)}</div>' if aside else ""
                story_id = item.get("story_id") or stable_story_id(item)
                tags_json = html.escape(json.dumps(item.get("preference_tags", []), ensure_ascii=False), quote=True)
                category_attr = html.escape(item.get("category", "other"), quote=True)
                cards.append(
                    f'<article class="story" data-story-id="{story_id}" data-category="{category_attr}" data-tags="{tags_json}">'
                    f'<div class="num">{number}</div><div class="story-body"><h3>{html.escape(item.get("headline", ""))}</h3>'
                    f'<p>{html.escape(item.get("body", ""))}</p>{aside_html}'
                    f'<div class="feedback" aria-label="Настроить будущую ленту">'
                    f'<button class="vote vote-up" type="button" title="Больше такого">👍 <span>Больше такого</span></button>'
                    f'<button class="vote vote-down" type="button" title="Меньше такого">👎 <span>Меньше такого</span></button>'
                    f'</div></div></article>'
                )
                number += 1
            parts.append(f'<section><h2>{html.escape(section)}</h2>{"".join(cards)}</section>')

    title = html.escape(edition.get("title", "Новости Игоря"))
    generated = now.strftime("%d.%m.%Y · %H:%M МСК")
    home_link = f'<a href="{nav_home}">Лента</a>' if archive_mode else ""
    p_cfg = cfg.get("personalization", {})
    js_cfg = json.dumps({
        "hideThreshold": float(p_cfg.get("hide_threshold", -1.6)),
        "categoryWeight": float(p_cfg.get("category_vote_weight", 0.8)),
        "tagWeight": float(p_cfg.get("tag_vote_weight", 0.45)),
    })
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="Персональная новостная лента: сигнал вместо потока.">
<style>
:root {{ color-scheme: light dark; --bg:#f7f7f5; --text:#171717; --muted:#717171; --line:#e7e7e2; --accent:#1f4d3f; --soft:#efefeb; }}
@media (prefers-color-scheme:dark) {{ :root {{ --bg:#111; --text:#f3f3f0; --muted:#aaa; --line:#30302e; --accent:#91c9b5; --soft:#1d1d1b; }} }}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.5}}
.wrap{{max-width:820px;margin:0 auto;padding:28px 20px 70px}} header{{padding:28px 0 22px;border-bottom:1px solid var(--line);margin-bottom:24px}}
h1{{font-family:Georgia,"Times New Roman",serif;font-size:clamp(36px,7vw,62px);line-height:.98;margin:0 0 14px;letter-spacing:-1.5px}}
.deck{{font-size:17px;color:var(--muted);margin:0 0 12px}} .meta{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--muted)}} .meta a{{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}}
.day-divider{{display:flex;align-items:center;gap:12px;margin:40px 0 10px;color:var(--muted);font-size:13px;font-weight:600}} .day-divider:after{{content:"";height:1px;background:var(--line);flex:1}}
section{{margin:30px 0}} h2{{font-size:14px;letter-spacing:.08em;text-transform:uppercase;margin:0 0 8px;color:var(--accent)}}
.story{{display:grid;grid-template-columns:34px 1fr;gap:10px;padding:22px 0;border-top:1px solid var(--line)}} .num{{font-family:Georgia,serif;font-size:16px;color:var(--muted);padding-top:4px}}
h3{{font-family:Georgia,"Times New Roman",serif;font-size:25px;line-height:1.16;margin:0 0 9px;letter-spacing:-.25px}} p{{margin:0;font-size:17px}}
.aside{{margin-top:12px;padding-left:13px;border-left:3px solid var(--accent);font-size:15px;color:var(--muted)}}
.feedback{{display:flex;gap:8px;margin-top:14px}} .vote{{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:999px;padding:6px 10px;font:12px/1.2 inherit;cursor:pointer}} .vote:hover{{color:var(--text);background:var(--soft)}} .vote.active{{color:var(--text);border-color:var(--accent);background:var(--soft)}}
.pref-hidden{{display:none}} .pref-note{{padding:10px 12px;margin:0 0 18px;border:1px solid var(--line);border-radius:10px;color:var(--muted);font-size:13px}} .pref-note button{{border:0;background:none;color:var(--accent);cursor:pointer;font:inherit;text-decoration:underline}}
footer{{border-top:1px solid var(--line);padding-top:22px;margin-top:48px;font-size:13px;color:var(--muted)}}
@media(max-width:560px){{.wrap{{padding:18px 16px 50px}} header{{padding-top:20px}} .story{{grid-template-columns:28px 1fr}} h3{{font-size:22px}} p{{font-size:16px}} .vote span{{display:none}}}}
</style>
</head>
<body><main class="wrap">
<header><h1>{title}</h1><p class="deck">Сигнал вместо потока. Новое добавляется каждые четыре часа и не исчезает.</p>
<div class="meta"><span>Обновлено {generated}</span><a href="{nav_archive}">Архив по дням</a>{home_link}</div></header>
<div id="pref-note" class="pref-note" hidden>По вашим оценкам скрыто новых сюжетов: <strong id="hidden-count">0</strong>. <button id="show-hidden" type="button">Показать</button></div>
{"".join(parts)}
<footer>Новости Игоря · лайки хранятся только в этом браузере и влияют на будущие похожие сюжеты</footer>
</main>
<script>
(() => {{
  const CFG = {js_cfg};
  const KEY = 'igor_news_preferences_v1';
  const blank = () => ({{ category: {{}}, tags: {{}}, feedback: {{}}, known: {{}}, hidden: {{}} }});
  let state;
  try {{ state = Object.assign(blank(), JSON.parse(localStorage.getItem(KEY) || '{{}}')); }} catch (_) {{ state = blank(); }}
  const save = () => localStorage.setItem(KEY, JSON.stringify(state));
  const cards = [...document.querySelectorAll('.story[data-story-id]')];
  const parseTags = card => {{ try {{ return JSON.parse(card.dataset.tags || '[]'); }} catch (_) {{ return []; }} }};
  const score = card => {{
    const cat = (card.dataset.category || '').toLowerCase();
    const tags = parseTags(card);
    let s = Number(state.category[cat] || 0);
    if (tags.length) s += tags.reduce((a,t) => a + Number(state.tags[String(t).toLowerCase()] || 0), 0) / tags.length;
    return s;
  }};
  const setButtons = card => {{
    const v = Number(state.feedback[card.dataset.storyId] || 0);
    card.querySelector('.vote-up')?.classList.toggle('active', v === 1);
    card.querySelector('.vote-down')?.classList.toggle('active', v === -1);
  }};
  let hidden = 0;
  cards.forEach(card => {{
    const id = card.dataset.storyId;
    if (state.hidden[id]) {{ card.classList.add('pref-hidden'); hidden++; }}
    else if (!state.known[id] && score(card) <= CFG.hideThreshold) {{
      state.hidden[id] = true; card.classList.add('pref-hidden'); hidden++;
    }}
    state.known[id] = true;
    setButtons(card);
  }});
  save();
  const note = document.getElementById('pref-note');
  const count = document.getElementById('hidden-count');
  if (hidden > 0 && note && count) {{ note.hidden = false; count.textContent = String(hidden); }}
  document.getElementById('show-hidden')?.addEventListener('click', () => {{
    document.querySelectorAll('.pref-hidden').forEach(x => x.classList.remove('pref-hidden'));
    if (note) note.hidden = true;
  }});
  const vote = (card, wanted) => {{
    const id = card.dataset.storyId;
    const previous = Number(state.feedback[id] || 0);
    const next = previous === wanted ? 0 : wanted;
    const delta = next - previous;
    const cat = (card.dataset.category || '').toLowerCase();
    if (cat) state.category[cat] = Number(state.category[cat] || 0) + delta * CFG.categoryWeight;
    parseTags(card).forEach(t => {{
      const k = String(t).toLowerCase();
      state.tags[k] = Number(state.tags[k] || 0) + delta * CFG.tagWeight;
    }});
    state.feedback[id] = next;
    state.hidden[id] = false;
    card.classList.remove('pref-hidden');
    save(); setButtons(card);
  }};
  cards.forEach(card => {{
    card.querySelector('.vote-up')?.addEventListener('click', () => vote(card, 1));
    card.querySelector('.vote-down')?.addEventListener('click', () => vote(card, -1));
  }});
}})();
</script>
</body></html>"""


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
<style>body{{max-width:760px;margin:40px auto;padding:0 20px;font:17px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}h1{{font:42px/1 Georgia,serif}}a{{color:inherit}}li{{margin:10px 0}}</style></head><body><a href="../index.html">← Лента</a><h1>Архив по дням</h1><ul>{listing}</ul></body></html>"""


def save_outputs(
    new_edition: dict[str, Any],
    candidates: list[dict[str, Any]],
    cfg: dict[str, Any],
    now: datetime,
    existing_feed: list[dict[str, Any]],
) -> tuple[Path, Path, list[dict[str, Any]], list[dict[str, Any]]]:
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    docs_dir = ROOT / "docs"
    archive_dir = docs_dir / "archive"
    data_dir = docs_dir / "data"
    archive_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    stamp = now.strftime("%Y-%m-%d_%H%M")
    day = now.strftime("%Y-%m-%d")
    md_path = out_dir / f"digest_{stamp}.md"
    json_path = out_dir / f"digest_{stamp}.json"

    merged, added = merge_feed(existing_feed, new_edition.get("items", []), cfg, now)
    full_edition = {"title": "Новости Игоря", "items": merged}
    md_path.write_text(render_markdown({"title": new_edition.get("title", "Новости Игоря"), "items": added}, cfg), encoding="utf-8")
    internal = {"generated_at": now.isoformat(), "edition": new_edition, "candidate_count": len(candidates), "candidates": candidates}
    json_path.write_text(json.dumps(internal, ensure_ascii=False, indent=2), encoding="utf-8")

    feed_payload = {"updated_at": now.isoformat(), "items": merged}
    (docs_dir / "feed.json").write_text(json.dumps(feed_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    tz = ZoneInfo(cfg["timezone"])
    today_items = [x for x in merged if story_day(x, tz) == day]
    today_payload = {"date": day, "updated_at": now.isoformat(), "items": today_items}
    (data_dir / f"{day}.json").write_text(json.dumps(today_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (docs_dir / "index.html").write_text(render_html(full_edition, now, cfg, archive_mode=False), encoding="utf-8")
    (archive_dir / f"{day}.html").write_text(render_html({"title": "Новости Игоря", "items": today_items}, now, cfg, archive_mode=True), encoding="utf-8")
    (archive_dir / "index.html").write_text(render_archive_index(docs_dir), encoding="utf-8")
    (docs_dir / ".nojekyll").touch()
    return md_path, json_path, merged, added




def reconcile_special_flags(edition: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    random_urls = {c.get("url", "") for c in candidates if c.get("collection_batch") == "random_top" and c.get("url")}
    internet_urls = {c.get("url", "") for c in candidates if c.get("collection_batch") == "internet_culture" and c.get("url")}
    for item in edition.get("items", []):
        urls = set(item.get("source_urls", []) or [])
        item["is_random_top"] = bool(urls & random_urls)
        if urls & internet_urls:
            item["is_internet_element"] = True
        else:
            item["is_internet_element"] = bool(item.get("is_internet_element", False) and item.get("aside", "").strip())
    return edition

def source_health(candidates: list[dict[str, Any]]) -> str:
    counts = Counter(c.get("source_name", "unknown") for c in candidates)
    top = ", ".join(f"{name}: {count}" for name, count in counts.most_common(8))
    return top or "нет кандидатов"


def main() -> int:
    parser = argparse.ArgumentParser(description="Персональная лента 'Новости Игоря'")
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
    today = now.strftime("%Y-%m-%d")
    existing_feed = load_master_feed(cfg, now)
    today_items = [x for x in existing_feed if story_day(x, tz) == today]

    min_internet = int(cfg["edition"].get("min_internet_elements_per_day", 1))
    require_internet_element = sum(1 for x in today_items if x.get("is_internet_element")) < min_internet
    # One lottery story in every normal four-hour update. Manual --batch runs obey only requested batches.
    require_random_top = bool(cfg["edition"].get("random_top_items_per_update", 1)) and not args.batch

    print(f"В ленте уже {len(existing_feed)} сюжетов; сегодня {len(today_items)}.")
    print(f"Сегодня нужен интернет-элемент: {require_internet_element}; случайный топ: {require_random_top}")

    batches = cfg["collection_batches"]
    if args.batch:
        selected = list(args.batch)
    else:
        selected = list(batches.keys())
        if not require_internet_element and "internet_culture" in selected:
            selected.remove("internet_culture")

    # Validate ordinary whitelisted batches; random_top deliberately has no domain filter.
    for batch in selected:
        if batch not in batches:
            raise SystemExit(f"Неизвестный batch: {batch}")
        allow_any = bool(batches[batch].get("allow_any_domain", False))
        domains = [] if allow_any else batch_domains(sources, batch)
        if not domains and not allow_any:
            raise SystemExit(f"У batch {batch} нет доменов в sources.json")
        print(f"{batch}: {'любой разумный домен' if allow_any else str(len(domains)) + ' domains'}")

    if args.dry_run:
        print("Конфиги валидны. API не вызывался.")
        return 0

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Нет OPENAI_API_KEY. Скопируйте .env.example в .env и добавьте ключ.")

    client = OpenAI()
    candidates: list[dict[str, Any]] = []
    for batch in selected:
        allow_any = bool(batches[batch].get("allow_any_domain", False))
        domains = [] if allow_any else batch_domains(sources, batch)
        print(f"Собираю {batch}...")
        items = collect_batch(client, batch, batches[batch]["prompt"], domains, cfg, now)
        if batch == "random_top" and items:
            # One real lottery ticket from the top pool; the editor cannot cherry-pick it back into familiarity.
            items = [random.SystemRandom().choice(items)]
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
    edition = edit_edition(
        client, candidates, sources, cfg, policy, editor_prompt, now, history,
        require_internet_element=require_internet_element,
        require_random_top=require_random_top,
    )
    edition = reconcile_special_flags(edition, candidates)
    edition = apply_hard_limits(edition, cfg)
    edition = enforce_required_specials(
        edition, candidates, cfg, now,
        require_random_top=require_random_top,
        require_internet_element=require_internet_element,
    )

    md_path, json_path, merged_feed, added = save_outputs(edition, candidates, cfg, now, existing_feed)
    update_state(state, {"items": added}, cfg, now)

    print(f"Добавлено в ленту: {len(added)}; всего: {len(merged_feed)}")
    print(f"Готово: {md_path}")
    print(f"Внутренний архив запуска: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
