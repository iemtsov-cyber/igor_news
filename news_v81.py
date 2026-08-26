from __future__ import annotations

import re

import news_v8 as v8


# v8.1: editorial quality guardrails before the AI editor sees the RSS pool.
# Interfax remains in sources.json as a fact-checking source, but we do not use
# its political wire headlines as automatic discovery material.
DROP_RSS_SOURCES = {"interfax.ru"}
DROP_RSS_NAMES = {"интерфакс"}

STATEMENT_ONLY_RE = re.compile(
    r"\b(считает|считают|счёл|сочла|заявил|заявила|заявили|обвинил|обвинила|обвинили|"
    r"назвал|назвала|назвали|предупредил|предупредила|предупредили|допустил|допустила|"
    r"допустили|пообещал|пообещала|пообещали|призвал|призвала|призвали|раскритиковал|"
    r"раскритиковала|раскритиковали|оценил|оценила|оценили|рассказал|рассказала|рассказали)\b",
    re.IGNORECASE,
)

# If the same headline also contains a concrete act/outcome, keep it for the editor.
CONCRETE_ACTION_RE = re.compile(
    r"\b(принял|приняла|приняли|подписал|подписала|подписали|утвердил|утвердила|утвердили|"
    r"ввёл|ввела|ввели|отменил|отменила|отменили|запустил|запустила|запустили|открыл|"
    r"открыла|открыли|закрыл|закрыла|закрыли|купил|купила|купили|продал|продала|продали|"
    r"подал иск|подала иск|подали иск|оштрафовал|оштрафовала|оштрафовали|арестовал|"
    r"арестовала|арестовали|начал|начала|начали|снизил|снизила|снизили|повысил|повысила|"
    r"повысили|сократил|сократила|сократили|увеличил|увеличила|увеличили|суд|решение суда|"
    r"закон|указ|постановление|договор|сделка|банкротство|выборы|результаты)\b",
    re.IGNORECASE,
)

CLICKBAIT_LEAD_RE = re.compile(
    r"^(стало известно|выяснилось|раскрыто|названы|назван|названа|эксперты рассказали|"
    r"учёные рассказали|ученые рассказали|вот почему|вот что известно)\s*[:—-]?\s*",
    re.IGNORECASE,
)


def clean_rss_title(title: str, source_name: str = "") -> str:
    text = (title or "").strip()
    if source_name:
        # Google News frequently appends " - Source" to the title.
        text = re.sub(rf"\s+[—-]\s+{re.escape(source_name)}\s*$", "", text, flags=re.IGNORECASE)
    text = CLICKBAIT_LEAD_RE.sub("", text)
    return text.strip(" \t\n\r\"'«»—-")


def is_statement_only(title: str) -> bool:
    return bool(STATEMENT_ONLY_RE.search(title)) and not bool(CONCRETE_ACTION_RE.search(title))


_original_rss_candidates = v8.rss_candidates
_original_fallback_edition = v8.fallback_edition


def rss_candidates(session, batch, domains, cfg, now):
    items = _original_rss_candidates(session, batch, domains, cfg, now)
    filtered = []
    for item in items:
        source_name = (item.get("rss_source_name") or item.get("source_name") or "").strip()
        source_domain = (item.get("source_domain") or "").lower()
        item = dict(item)
        item["title"] = clean_rss_title(item.get("title", ""), source_name)
        if item.get("what_happened"):
            item["what_happened"] = clean_rss_title(item["what_happened"], source_name)

        if batch in {"russia_core", "world_core"}:
            if source_domain in DROP_RSS_SOURCES or source_name.lower() in DROP_RSS_NAMES:
                print(f"  editorial filter: пропускаю RSS-источник {source_name or source_domain}")
                continue
            if is_statement_only(item.get("title", "")):
                print(f"  editorial filter: заявление без действия — {item.get('title', '')[:100]}")
                continue

        if item.get("title"):
            filtered.append(item)
    return filtered


def fallback_edition(candidates, cfg, now):
    edition = _original_fallback_edition(candidates, cfg, now)
    # No AI is available in fallback mode, so at minimum remove mechanical RSS
    # source suffixes/clickbait lead-ins. Full rewriting is done by OpenRouter.
    for item in edition.get("items", []):
        item["headline"] = clean_rss_title(item.get("headline", ""))
        if item.get("body") == item.get("headline"):
            item["body"] = item.get("headline", "")
    return edition


# Monkey-patch v8's globals; its main() resolves them at runtime.
v8.rss_candidates = rss_candidates
v8.fallback_edition = fallback_edition


if __name__ == "__main__":
    raise SystemExit(v8.main())
