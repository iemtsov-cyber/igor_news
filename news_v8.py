from __future__ import annotations

import html
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
import xml.etree.ElementTree as ET

import requests
from zoneinfo import ZoneInfo

import news as base


GOOGLE_NEWS_SEARCH = "https://news.google.com/rss/search"
GOOGLE_NEWS_TOP = "https://news.google.com/rss"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

BATCH_CATEGORY = {
    "russia_core": "russia",
    "world_core": "world",
    "science_tech": "technology",
    "moscow_food": "moscow",
    "culture_sport": "culture",
    "internet_culture": "internet_culture",
    "random_top": "random_top",
}


def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(value.split())


def google_news_url(batch: str, domains: list[str], lookback_hours: int) -> str:
    if batch == "random_top":
        return f"{GOOGLE_NEWS_TOP}?hl=ru&gl=RU&ceid=RU:ru"

    hours = max(4, min(24, lookback_hours))
    if batch == "internet_culture":
        query = f'(мем OR meme OR Reddit OR viral OR "интернет-феномен") when:{hours}h'
    elif domains:
        domain_query = " OR ".join(f"site:{d}" for d in domains)
        query = f"({domain_query}) when:{hours}h"
    else:
        query = f"новости when:{hours}h"
    return f"{GOOGLE_NEWS_SEARCH}?q={quote_plus(query)}&hl=ru&gl=RU&ceid=RU:ru"


def rss_candidates(
    session: requests.Session,
    batch: str,
    domains: list[str],
    cfg: dict,
    now: datetime,
) -> list[dict]:
    url = google_news_url(batch, domains, int(cfg.get("lookback_hours", 8)))
    response = session.get(url, timeout=25)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    limit = int(cfg.get("web_search", {}).get("max_candidates_per_batch", 8))
    cutoff = now - timedelta(hours=max(8, int(cfg.get("lookback_hours", 8)) + 2))
    seen: set[str] = set()
    items: list[dict] = []

    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)

        published_raw = (node.findtext("pubDate") or "").strip()
        published_iso = ""
        if published_raw:
            try:
                published = parsedate_to_datetime(published_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=now.tzinfo)
                published_local = published.astimezone(now.tzinfo)
                if batch != "random_top" and published_local < cutoff:
                    continue
                published_iso = published_local.isoformat()
            except Exception:
                pass

        source_node = node.find("source")
        source_name = (source_node.text or "").strip() if source_node is not None else ""
        source_url = source_node.attrib.get("url", "") if source_node is not None else ""
        source_domain = base.normalize_domain(source_url) if source_url else "news.google.com"
        description = strip_html(node.findtext("description") or "")
        if description == title:
            description = ""

        is_internet = batch == "internet_culture"
        items.append({
            "title": title,
            "url": link,
            "source_domain": source_domain,
            "published_at": published_iso,
            "category": BATCH_CATEGORY.get(batch, "other"),
            "what_happened": description[:600] or title,
            "why_interesting": "",
            "is_comment_or_meme": is_internet,
            "comment_or_meme_text": title if is_internet else "",
            "confidence": 0.78 if source_domain != "news.google.com" else 0.62,
            "rss_source_name": source_name,
            "collection_batch": batch,
        })
        if len(items) >= limit:
            break

    if batch == "random_top" and items:
        items = [random.SystemRandom().choice(items)]
    return items


def extract_json(text: str) -> dict:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def fallback_edition(candidates: list[dict], cfg: dict, now: datetime) -> dict:
    target = int(cfg["edition"].get("target_items", 6))
    max_items = int(cfg["edition"].get("max_items", 8))
    chosen: list[dict] = []

    random_items = [x for x in candidates if x.get("collection_batch") == "random_top"]
    internet_items = [x for x in candidates if x.get("collection_batch") == "internet_culture"]
    for special in (random_items[:1] + internet_items[:1]):
        if special not in chosen:
            chosen.append(special)

    ranked = sorted(
        candidates,
        key=lambda x: (float(x.get("source_weight", 0.4)), float(x.get("confidence", 0.5))),
        reverse=True,
    )
    for item in ranked:
        if item not in chosen:
            chosen.append(item)
        if len(chosen) >= min(target, max_items):
            break

    out = []
    for item in chosen[:max_items]:
        batch = item.get("collection_batch", "")
        source_name = item.get("source_name") or item.get("rss_source_name") or item.get("source_domain", "")
        out.append({
            "category": item.get("category", "other"),
            "headline": item.get("title", ""),
            "body": item.get("what_happened", "") or item.get("title", ""),
            "aside": item.get("comment_or_meme_text", "") if item.get("is_comment_or_meme") else "",
            "source_urls": [item.get("url", "")] if item.get("url") else [],
            "source_names": [source_name] if source_name else [],
            "importance": 6.0,
            "confidence": float(item.get("confidence", 0.6)),
            "preference_tags": [item.get("category", "other")],
            "is_random_top": batch == "random_top",
            "is_internet_element": batch == "internet_culture",
        })
    return {"title": f"Новости Игоря — {now:%d.%m.%Y %H:%M}", "items": out}


def openrouter_edition(
    session: requests.Session,
    candidates: list[dict],
    sources: list[dict],
    cfg: dict,
    policy: str,
    editor_prompt: str,
    now: datetime,
    history: list[dict],
    require_internet_element: bool,
    require_random_top: bool,
) -> dict:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("OPENROUTER_API_KEY не задан — использую RSS fallback без AI.")
        return fallback_edition(candidates, cfg, now)

    edition_cfg = cfg["edition"]
    payload_data = {
        "now": now.isoformat(),
        "timezone": cfg["timezone"],
        "edition": edition_cfg,
        "category_weights": cfg.get("category_weights", {}),
        "soft_targets": cfg.get("soft_targets", {}),
        "recently_shown": history[-120:],
        "require_internet_element": require_internet_element,
        "require_random_top": require_random_top,
        "candidates": candidates,
    }
    prompt = f"""
Ты редактор персональной новостной ленты «Новости Игоря».

{editor_prompt}

Редакционная политика:
{policy}

Собери ТОЛЬКО из предоставленных RSS-кандидатов новый четырёхчасовой блок.
Не выдумывай факты, цифры, цитаты и комментарии. Если в RSS есть только заголовок — не достраивай отсутствующие подробности.
Цель: {edition_cfg['target_items']} сюжетов, допустимо {edition_cfg['min_items']}–{edition_cfg['max_items']}.
Не повторяй recently_shown без существенного нового развития.
Политика — максимум {round(edition_cfg['max_politics_share'] * 100)}% блока.
Если require_random_top=true, включи ровно один candidate с collection_batch=random_top.
Если require_internet_element=true и есть подходящий internet_culture candidate, включи минимум один; мем/комментарий не выдумывать.
Верни только JSON по заданной схеме.

ДАННЫЕ:
{json.dumps(payload_data, ensure_ascii=False)}
""".strip()

    request_body = {
        "model": cfg.get("models", {}).get("editor", "openrouter/free"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": int(cfg.get("editor_max_output_tokens", 6500)),
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "news_edition",
                "strict": True,
                "schema": base.edition_schema(),
            },
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://iemtsov-cyber.github.io/igor_news/",
        "X-Title": "Igor News",
    }

    for attempt in range(2):
        try:
            response = session.post(OPENROUTER_URL, headers=headers, json=request_body, timeout=120)
            if response.status_code >= 400:
                raise RuntimeError(f"OpenRouter HTTP {response.status_code}: {response.text[:500]}")
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            edition = extract_json(str(content))
            if isinstance(edition.get("items"), list):
                print(f"OpenRouter editor: {len(edition['items'])} сюжетов.")
                return edition
        except Exception as exc:
            print(f"OpenRouter editor failed ({attempt + 1}/2): {exc}")
            if attempt == 0:
                request_body.pop("response_format", None)
                request_body["messages"][0]["content"] += "\nВерни один валидный JSON-объект без markdown."

    print("OpenRouter недоступен — продолжаю с RSS fallback.")
    return fallback_edition(candidates, cfg, now)


def main() -> int:
    cfg = base.read_json("config.json")
    sources_doc = base.read_json("sources.json")
    sources = sources_doc.get("sources", sources_doc if isinstance(sources_doc, list) else [])
    policy = base.read_text("editorial_policy.md")
    editor_prompt = base.read_text("prompts/editor.md")
    state = base.load_state()

    tz = ZoneInfo(cfg["timezone"])
    now = datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    existing_feed = base.load_master_feed(cfg, now)
    today_items = [x for x in existing_feed if base.story_day(x, tz) == today]

    min_internet = int(cfg["edition"].get("min_internet_elements_per_day", 1))
    require_internet_element = sum(1 for x in today_items if x.get("is_internet_element")) < min_internet
    require_random_top = bool(cfg["edition"].get("random_top_items_per_update", 1))

    print(f"В ленте уже {len(existing_feed)} сюжетов; сегодня {len(today_items)}.")
    print(f"RSS mode; интернет-элемент нужен: {require_internet_element}; случайный топ: {require_random_top}")

    batches = cfg["collection_batches"]
    selected = list(batches.keys())
    if not require_internet_element and "internet_culture" in selected:
        selected.remove("internet_culture")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 IgorNews/1.0"})
    candidates: list[dict] = []
    for batch in selected:
        allow_any = bool(batches[batch].get("allow_any_domain", False))
        domains = [] if allow_any else base.batch_domains(sources, batch)
        print(f"RSS собираю {batch}...")
        try:
            items = rss_candidates(session, batch, domains, cfg, now)
        except Exception as exc:
            print(f"RSS batch {batch} failed: {exc}")
            items = []
        candidates.extend(items)
        print(f"  найдено: {len(items)}")

    candidates = base.enrich_candidates(candidates, sources)
    candidates = base.dedupe_candidates(candidates, float(cfg["dedupe"]["title_similarity_threshold"]))
    print(f"После дедупликации RSS: {len(candidates)}")

    history = base.recent_history(state, cfg, now)
    edition = openrouter_edition(
        session, candidates, sources, cfg, policy, editor_prompt, now, history,
        require_internet_element=require_internet_element,
        require_random_top=require_random_top,
    )
    edition = base.reconcile_special_flags(edition, candidates)
    edition = base.apply_hard_limits(edition, cfg)
    edition = base.enforce_required_specials(
        edition, candidates, cfg, now,
        require_random_top=require_random_top,
        require_internet_element=require_internet_element,
    )

    md_path, json_path, merged_feed, added = base.save_outputs(edition, candidates, cfg, now, existing_feed)
    base.update_state(state, {"items": added}, cfg, now)
    print(f"Добавлено в ленту: {len(added)}; всего: {len(merged_feed)}")
    print(f"Готово: {md_path}")
    print(f"Внутренний архив запуска: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
