"""
ai_analyst.py — Модуль 3 v2: LLM-аналитик с поддержкой двух режимов.

Этап 1: classify(market)  — БЕЗ интернета. Только текст рынка.
  🦢 SNIPER   — три критерия: асимметрия / механизм / недооценка толпы
  ⚡ CONVEYOR — три критерия: нетир-1 матч / реальные шансы / причина недооценки
  Кэш: 24 часа.

Этап 2: score(market, cls) — С интернетом. Только для кандидатов.
  🦢 SNIPER   — новости + Gemini → оценка вероятности черного лебедя
  ⚡ CONVEYOR — статистика команды + Gemini → оценка шансов андердога
  Кэш: 12 часов.

deep_analyze(market_id)   — Claude Sonnet: глубокий анализ по /analyze команде.
weekly_portfolio_review() — Claude Sonnet: еженедельная ревизия портфеля.
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

import config
import db

logger = logging.getLogger(__name__)


# =============================================================================
# Низкоуровневые вызовы API
# =============================================================================

def _gemini_raw(prompt: str, max_tokens: int = 500, temperature: float = 0.3) -> str | None:
    """
    Базовый вызов Gemini — возвращает сырой текст ответа или None.
    Используется как основа для call_gemini (JSON) и call_gemini_text (свободный текст).
    """
    if not config.GEMINI_API_KEY or config.GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=60)
            if not resp.ok:
                logger.error("Gemini API [%d/3] HTTP %d: %s",
                             attempt, resp.status_code, resp.text[:500])
                if resp.status_code in (400, 401, 403):
                    return None
                if attempt < 3:
                    time.sleep(2)
                continue
            data = resp.json()
            candidates = data.get("candidates") or []
            if candidates:
                return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            logger.warning("Gemini: пустой ответ. JSON: %s", str(data)[:300])
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("Gemini API [%d/3] сетевая ошибка: %s", attempt, e)
            if attempt < 3:
                time.sleep(2)
    return None


def call_gemini_text(prompt: str, max_tokens: int = 3000) -> str | None:
    """
    Вызов Gemini для свободного текстового ответа (анализ, ревизия).
    Используется как основной AI для deep_analyze и weekly_review,
    т.к. Anthropic блокирует запросы с VPS/облачных серверов (HTTP 403).
    """
    return _gemini_raw(prompt, max_tokens=max_tokens, temperature=0.7)


def call_gemini(prompt: str) -> dict | None:
    """
    Вызов Gemini для JSON-ответов (classify, score).
    Возвращает распарсенный dict или None при любой ошибке.
    """
    text = _gemini_raw(prompt, max_tokens=500, temperature=0.3)
    if text:
        return _parse_json(text)
    return None


def call_openrouter(prompt: str, system_prompt: str = "") -> str | None:
    """
    Вызов Claude через OpenRouter — обходит блокировку Anthropic на РФ/VPS IP.

    OpenRouter — легальный прокси-сервис для LLM API.
    Регистрация: https://openrouter.ai | Ключ → config.OPENROUTER_API_KEY
    """
    api_key = getattr(config, "OPENROUTER_API_KEY", "")
    if not api_key:
        return None

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model":      getattr(config, "CLAUDE_OPENROUTER_MODEL", "anthropic/claude-sonnet-4-5"),
        "max_tokens": 3000,
        "messages":   messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/black-swan-hunter",
        "X-Title":       "Black Swan Hunter",
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=payload, headers=headers, timeout=60
            )
            if not resp.ok:
                logger.error("OpenRouter [%d/3] HTTP %d: %s",
                             attempt, resp.status_code, resp.text[:400])
                if resp.status_code in (400, 401, 403):
                    return None
                if attempt < 3:
                    time.sleep(3)
                continue
            choices = resp.json().get("choices") or []
            if choices:
                return choices[0].get("message", {}).get("content", "")
            logger.warning("OpenRouter: пустой ответ.")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("OpenRouter [%d/3] сетевая ошибка: %s", attempt, e)
            if attempt < 3:
                time.sleep(3)
    return None


def call_claude(prompt: str, system_prompt: str = "") -> str | None:
    """
    Прямой вызов Anthropic API. Блокируется на российских/VPS серверах (HTTP 403).
    Поддерживает CLAUDE_PROXY из config.py для маршрутизации через прокси.
    """
    if not config.ANTHROPIC_API_KEY or config.ANTHROPIC_API_KEY == "YOUR_ANTHROPIC_API_KEY":
        logger.debug("ANTHROPIC_API_KEY не настроен.")
        return None

    headers = {
        "x-api-key":         config.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      config.CLAUDE_MODEL,
        "max_tokens": 3000,
        "messages":   [{"role": "user", "content": prompt}],
    }
    if system_prompt:
        payload["system"] = system_prompt

    proxy_url = getattr(config, "CLAUDE_PROXY", "")
    proxies   = {"https": proxy_url, "http": proxy_url} if proxy_url else None

    for attempt in range(1, 4):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                json=payload, headers=headers,
                timeout=60, proxies=proxies
            )
            if not resp.ok:
                logger.error("Claude API [%d/3] HTTP %d: %s",
                             attempt, resp.status_code, resp.text[:500])
                if resp.status_code in (400, 401, 403):
                    return None
                if attempt < 3:
                    time.sleep(3)
                continue
            content = resp.json().get("content") or []
            if content:
                return content[0].get("text", "")
            logger.warning("Claude: пустой ответ.")
            return None
        except requests.exceptions.RequestException as e:
            logger.warning("Claude API [%d/3] сетевая ошибка: %s", attempt, e)
            if attempt < 3:
                time.sleep(3)
    return None


def call_claude_any(prompt: str, system_prompt: str = "") -> str | None:
    """
    Умный вызов Claude — пробует все доступные маршруты:
      1. OpenRouter (обходит блокировку РФ/VPS) — если задан OPENROUTER_API_KEY
      2. Прямой Anthropic API (работает с локальных IP)
    """
    result = call_openrouter(prompt, system_prompt)
    if result:
        logger.debug("Claude ответил через OpenRouter.")
        return result
    result = call_claude(prompt, system_prompt)
    if result:
        logger.debug("Claude ответил через прямой API.")
        return result
    return None




def _parse_json(text: str) -> dict | None:
    """
    Извлекает JSON из ответа LLM — убирает markdown-обёртки и лишний текст.
    Возвращает dict или None.
    """
    if not text:
        return None
    text = text.strip()
    # Убираем ```json ... ```
    match = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if match:
        text = match.group(1).strip()
    # Ищем { ... }
    start, end = text.find("{"), text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError as e:
        logger.debug("JSON parse error: %s | snippet: %s", e, text[start:start+100])
        return None


# =============================================================================
# Поиск новостей
# =============================================================================

# Стоп-слова и паттерны для fallback-упрощения запроса
_QUERY_STOP = {
    "will", "by", "would", "could", "should", "does", "is", "are", "was",
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "not",
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
    "nov", "dec", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "2024", "2025", "2026", "2027",
    "1st", "2nd", "3rd", "4th", "31st", "30th", "28th",
}


def make_search_query(question: str) -> str:
    """
    Из вопроса рынка делает короткий поисковый запрос (3-5 ключевых слов).

    Сначала пробует через Gemini Flash, fallback — механическая чистка.
    Примеры:
      'Ukraine agrees not to join NATO by March 31?' → 'Ukraine NATO agreement 2026'
      'Will Solana dip to $130 January 12-18?'       → 'Solana price crash 2026'
    """
    prompt = (
        f'Из этого вопроса рынка предсказаний сформируй поисковый запрос '
        f'для DuckDuckGo из 3-5 ключевых слов для поиска СВЕЖИХ новостей по теме. '
        f'Без дат "by March 31" и вопросительных конструкций типа "Will". '
        f'Добавь год "2026" если тема актуальна сейчас. '
        f'Верни ТОЛЬКО сам запрос, без кавычек и пояснений.\n\n'
        f'Вопрос: "{question}"'
    )
    try:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 30},
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        candidates = data.get("candidates") or []
        if candidates:
            text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            if text and len(text) < 120:
                return text
    except Exception as e:
        logger.debug("make_search_query Gemini ошибка: %s", e)

    # Fallback: механическая чистка
    import re
    words = re.sub(r'[^\w\s]', ' ', question).split()
    keywords = [
        w for w in words
        if w.lower() not in _QUERY_STOP and len(w) > 2
    ]
    return " ".join(keywords[:5]) + " 2026"


def _ddgs_search(query: str, max_results: int) -> list[dict]:
    """
    Пытается найти новости через ddgs (новое имя пакета).
    Fallback на старое имя duckduckgo_search.
    """
    try:
        from ddgs import DDGS
        with DDGS() as d:
            return list(d.text(query, max_results=max_results))
    except ImportError:
        pass
    try:
        from duckduckgo_search import DDGS  # type: ignore
        with DDGS() as d:
            return list(d.text(query, max_results=max_results))
    except ImportError:
        logger.warning("Ни ddgs, ни duckduckgo_search не найдены. pip install ddgs")
    except Exception as e:
        logger.debug("DDGS fallback import ошибка: %s", e)
    return []


def _html_ddg_search(query: str, max_results: int = 5) -> list[dict]:
    """
    Резервный поиск через HTML-интерфейс DuckDuckGo (без API-библиотеки).
    Парсит заголовки и сниппеты из HTML-ответа вручную.
    """
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; BSH/2.0)"},
            timeout=15,
        )
        logger.debug("HTML DDG response: %s %s", resp.url, resp.status_code)
        if resp.status_code != 200:
            return []

        results = []
        import re as _re
        # Извлекаем блоки результатов: <a class="result__a" href="...">title</a> ... snippet
        links  = _re.findall(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>([^<]+)</a>', resp.text)
        snips  = _re.findall(r'<a class="result__snippet"[^>]*>([^<]+)</a>', resp.text)
        for i, (href, title) in enumerate(links[:max_results]):
            body = snips[i].strip() if i < len(snips) else ""
            results.append({"title": title.strip(), "body": body, "href": href})
        return results
    except Exception as e:
        logger.debug("HTML DDG поиск ошибка: %s", e)
    return []


def search_news(question: str, max_results: int = 5) -> tuple[list[dict], str]:
    """
    Ищет свежие новости по теме рынка.

    Алгоритм (три попытки):
      1. Умный запрос через make_search_query (Gemini/fallback) + DDGS
      2. Упрощённый запрос (2-3 слова) + DDGS
      3. Упрощённый запрос + HTML DDG

    Возвращает (results, query_used).
    Логирует сам запрос и количество результатов — чтобы видеть что ищется.
    """
    # Попытка 1: умный запрос
    query1 = make_search_query(question)
    results = _ddgs_search(query1, max_results)
    logger.info("Поиск (1): «%s» → %d результатов", query1, len(results))
    if results:
        return results, query1

    # Попытка 2: упрощённый запрос (2-3 главных слова)
    import re as _re
    raw_words = _re.sub(r'[^\w\s]', ' ', question).split()
    key_words = [w for w in raw_words if w.lower() not in _QUERY_STOP and len(w) > 3][:3]
    query2 = " ".join(key_words) if key_words else query1[:30]
    if query2 != query1:
        results = _ddgs_search(query2, max_results)
        logger.info("Поиск (2): «%s» → %d результатов", query2, len(results))
        if results:
            return results, query2

    # Попытка 3: HTML-парсинг DDG
    query3 = query2 or query1
    results = _html_ddg_search(query3, max_results)
    logger.info("Поиск (3/HTML): «%s» → %d результатов", query3, len(results))
    return results, query3


def _format_news(results: list[dict]) -> str:
    """Форматирует результаты поиска в нумерованный список для промпта."""
    if not results:
        return "Актуальных новостей/данных не найдено."
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body  = r.get("body", r.get("snippet", ""))[:200]
        href  = r.get("href", "")
        lines.append(f"{i}. {title}\n   {body}\n   URL: {href}")
    return "\n".join(lines)


# =============================================================================
# Этап 1: classify — без интернета, разные промпты по mode
# =============================================================================

def _build_classify_prompt_sniper(market: dict, price: float, prob_pct: float) -> str:
    return f"""Ты — строгий классификатор рынков предсказаний для стратегии "Чёрный лебедь".

## Рынок
Вопрос: "{market.get('question', '')}"
Описание: "{(market.get('description') or '')[:300]}"
Цена: ${price:.3f} ({prob_pct}% вероятность по рынку)
Объём: ${market.get('volume', 0):,.0f} | Экспирация: {(market.get('end_date') or '')[:10]}

## Критерии кандидата — ВСЕ ТРИ должны выполняться
1. АСИММЕТРИЯ: событие бинарное, последствия масштабные (затрагивает рынки/страны/индустрии). НЕ "компания выпустит продукт".
2. МЕХАНИЗМ: реальная цепочка реализации (напряжённость→инцидент→эскалация, регулятор→иск→запрет). НЕ фантастика.
3. НЕДООЦЕНКА ТОЛПОЙ: есть причина (событие вне фокуса аудитории Polymarket, нелинейный/каскадный сценарий, нет инсайдеров у большинства).

## Автоматически НЕ кандидат если:
- Результат зависит от одного человека ("Will X tweet Y?") — непрогнозируемо
- Есть очевидный инсайдерский edge против нас (корп. решения, даты релизов)
- Чисто развлекательное без системных последствий
- Нет реального механизма реализации в срок экспирации

Ответь СТРОГО в JSON (без markdown):
{{
  "category": "<geopolitics | crypto_risk | macro | health | politics_us | politics_world | tech_risk | climate | OTHER>",
  "is_black_swan_candidate": <true или false>,
  "confidence": <1-10, насколько уверен в решении>,
  "reject_reason": "<если false: какой из критериев не выполнен и почему. Если true: null>"
}}"""


def _build_classify_prompt_conveyor(market: dict, price: float, prob_pct: float) -> str:
    return f"""Ты — спортивный аналитик. Оцени киберспортивный/спортивный матч для ставки на андердога.

## Матч
Вопрос: "{market.get('question', '')}"
Описание: "{(market.get('description') or '')[:200]}"
Цена андердога: ${price:.3f} ({prob_pct}% по рынку)
Объём: ${market.get('volume', 0):,.0f} | До события: {(market.get('end_date') or '')[:16]}

## Критерии — кандидат если ВСЕ ТРИ:
1. НЕ тир-1 матч крупной лиги в стадии плей-офф/финала (NBA финал, Champions League финал) — там цены эффективные.
2. Андердог имеет реальные шансы — не аутсайдер против абсолютного чемпиона мира.
3. Есть потенциальная причина недооценки: малоизвестная команда с сильным последним сезоном, смена состава у фаворита, особенности карты/формата/домашнего поля.

## Автоматически НЕ кандидат если:
- Не спортивный/игровой матч (новости, события, другое)
- Очевидная разница в классе: фаворит 10x сильнее андердога без сомнений
- Матч уже начался или слишком далеко (>48ч) — это обрабатывается на уровне L2

Ответь СТРОГО в JSON (без markdown):
{{
  "category": "<esports | football | basketball | tennis | mma | other_sport>",
  "is_underdog_opportunity": <true или false>,
  "confidence": <1-10>,
  "reject_reason": "<если false: почему НЕ кандидат. Если true: null>"
}}"""


def classify(market: dict) -> dict | None:
    """
    Этап 1 Level 4: классификация рынка — БЕЗ интернета.

    🦢 SNIPER:   три критерия черного лебедя (асимметрия/механизм/недооценка)
    ⚡ CONVEYOR: три критерия спортивного андердога

    Возвращает нормализованный dict с is_candidate (унифицировано для обоих режимов).
    Кэш: AI_CACHE_HOURS (24ч).
    """
    if not config.AI_CLASSIFICATION_ENABLED:
        return None

    market_id = market["id"]
    mode      = market.get("mode", "sniper")

    cached = db.get_cached_classification(market_id, config.AI_CACHE_HOURS, stage="classify")
    if cached:
        cached["from_cache"] = True
        cached["is_candidate"] = bool(cached.get("is_candidate"))
        return cached

    price    = _get_min_price(market)
    prob_pct = round(price * 100, 1)

    if mode == "conveyor":
        prompt = _build_classify_prompt_conveyor(market, price, prob_pct)
    else:
        prompt = _build_classify_prompt_sniper(market, price, prob_pct)

    result_raw = call_gemini(prompt)
    used_model = "gemini"

    if result_raw is None:
        logger.warning("Gemini недоступен для classify (%s), пробую Claude (any)...", mode)
        text = call_claude_any(prompt, system_prompt="Отвечай только в формате JSON, без пояснений.")
        if text:
            result_raw = _parse_json(text)
            used_model = "claude"

    if result_raw is None:
        logger.warning("Оба AI недоступны для classify: %s", market_id[:20])
        return None

    # Нормализуем is_candidate: sniper → is_black_swan_candidate, conveyor → is_underdog_opportunity
    if mode == "conveyor":
        is_candidate = bool(result_raw.get("is_underdog_opportunity", False))
        default_cat  = result_raw.get("category", "other_sport")
    else:
        is_candidate = bool(result_raw.get("is_black_swan_candidate", False))
        default_cat  = result_raw.get("category", "OTHER")

    cls = {
        "market_id":           market_id,
        "stage":               "classify",
        "category":            str(default_cat),
        "is_candidate":        is_candidate,
        "confidence":          int(result_raw.get("confidence", 5)),
        "score":               None,
        "estimated_prob_pct":  None,
        "reasoning":           None,
        "key_catalyst":        None,
        "reject_reason":       result_raw.get("reject_reason"),
        "model":               used_model,
        "classified_at":       datetime.now(timezone.utc).isoformat(),
        "from_cache":          False,
    }
    db.save_classification(cls)

    status = "✅ КАНДИДАТ" if is_candidate else "❌ отклонён"
    mode_icon = "⚡" if mode == "conveyor" else "🦢"
    logger.info(
        "%s classify [%s]: %s cat=%s conf=%d/10 | %s",
        mode_icon, market.get("question", "")[:40], status, cls["category"],
        cls["confidence"], cls.get("reject_reason") or ""
    )
    return cls


# =============================================================================
# Этап 2: score — с интернетом, разные промпты по mode
# =============================================================================

def _build_score_prompt_sniper(market: dict, price: float, prob_pct: float,
                                cls: dict, news_text: str) -> str:
    return f"""Ты — квант-аналитик prediction markets. Оцени "чёрного лебедя" на основе свежих данных.

## Рынок
Вопрос: "{market.get('question', '')}"
Цена: ${price:.3f} (рынок оценивает вероятность в {prob_pct}%)
Категория: {cls.get("category", "?")}
Объём: ${market.get('volume', 0):,.0f} | Экспирация: {(market.get('end_date') or '')[:10]}

## Свежие новости (DuckDuckGo, последние дни)
{news_text}

Ответь СТРОГО в JSON (без markdown):
{{
  "score": <1-10, где 10 = событие явно недооценено, реальный шанс выше {prob_pct}%>,
  "estimated_probability_pct": <твоя оценка реальной вероятности в %>,
  "reasoning": "<2-3 предложения: что из новостей меняет оценку>",
  "key_catalyst": "<конкретный триггер или сигнал за следующие дни/недели>"
}}"""


def _build_score_prompt_conveyor(market: dict, price: float, prob_pct: float,
                                  cls: dict, news_text: str) -> str:
    return f"""Ты — спортивный аналитик. Оцени шансы андердога на основе статистики и новостей.

## Матч
Вопрос: "{market.get('question', '')}"
Цена андердога: ${price:.3f} ({prob_pct}% по рынку)
Вид спорта: {cls.get("category", "?")}
Объём: ${market.get('volume', 0):,.0f}

## Свежие данные (статистика/состав/форма)
{news_text}

Ответь СТРОГО в JSON (без markdown):
{{
  "score": <1-10, где 10 = андердог явно недооценён, реальный шанс значительно выше {prob_pct}%>,
  "estimated_probability_pct": <твоя оценка реального шанса победы андердога в %>,
  "reasoning": "<2-3 предложения: на основе статистики/формы/составов>",
  "key_catalyst": "<главный фактор в пользу андердога: форма, состав, особенности формата>"
}}"""


def score(market: dict, cls: dict) -> dict | None:
    """
    Этап 2 Level 4: скоринг кандидата через Gemini + DuckDuckGo.

    Вызывается ТОЛЬКО если classify() вернул is_candidate=True.
    🦢 SNIPER:   ищет новости → оценивает вероятность черного лебедя
    ⚡ CONVEYOR: ищет статистику команды → оценивает шансы андердога

    Кэш: 12 часов.
    """
    market_id = market["id"]
    mode      = market.get("mode", "sniper")
    SCORE_CACHE_HOURS = 12

    cached = db.get_cached_classification(market_id, SCORE_CACHE_HOURS, stage="score")
    if cached:
        cached["from_cache"] = True
        return cached

    price    = _get_min_price(market)
    prob_pct = round(price * 100, 1)

    logger.info("Ищу данные для score (%s): %s", mode, market.get("question", "")[:50])
    news_results, search_query = search_news(market.get("question", ""), max_results=5)
    news_text = _format_news(news_results)
    has_news  = bool(news_results)

    if mode == "conveyor":
        prompt = _build_score_prompt_conveyor(market, price, prob_pct, cls, news_text)
    else:
        prompt = _build_score_prompt_sniper(market, price, prob_pct, cls, news_text)

    result_raw = call_gemini(prompt)
    used_model = "gemini"

    if result_raw is None:
        logger.warning("Gemini недоступен для score (%s), пробую Claude (any)...", mode)
        text = call_claude_any(prompt, system_prompt="Отвечай только в формате JSON, без пояснений.")
        if text:
            result_raw = _parse_json(text)
            used_model = "claude"

    if result_raw is None:
        logger.warning("Оба AI недоступны для score: %s", market_id[:20])
        return None

    score_val = int(result_raw.get("score", 5))
    est_prob  = float(result_raw.get("estimated_probability_pct", prob_pct))
    reasoning = str(result_raw.get("reasoning", ""))
    if not has_news:
        reasoning = "⚠️ Новости не найдены — скор без контекста, может быть неточным. " + reasoning

    sc = {
        "market_id":           market_id,
        "stage":               "score",
        "category":            cls.get("category"),
        "is_candidate":        1,
        "confidence":          None,
        "score":               score_val,
        "estimated_prob_pct":  est_prob,
        "reasoning":           reasoning,
        "key_catalyst":        str(result_raw.get("key_catalyst", "")),
        "reject_reason":       None,
        "model":               used_model,
        "classified_at":       datetime.now(timezone.utc).isoformat(),
        "from_cache":          False,
    }
    db.save_classification(sc)

    mode_icon = "⚡" if mode == "conveyor" else "🦢"
    logger.info(
        "%s score [%s]: %d/10 | рынок %.1f%% → AI %.1f%% | данных: %d",
        mode_icon, market.get("question", "")[:40],
        score_val, prob_pct, est_prob, len(news_results)
    )
    return sc


# =============================================================================
# Обратная совместимость: classify_market = classify + score в одном вызове
# =============================================================================

def classify_market(market: dict) -> dict | None:
    """
    Объединяет оба этапа: classify() → если кандидат → score().

    Возвращает объединённый dict со всеми полями для alerter/filters,
    или None если AI недоступен.
    """
    cls = classify(market)
    if cls is None:
        return None

    if not cls.get("is_candidate"):
        return cls  # Отклонён на этапе 1, score не нужен

    sc = score(market, cls)
    if sc is None:
        # score недоступен — отправляем без скора, с пометкой
        cls["score"] = None
        cls["estimated_prob_pct"] = None
        cls["reasoning"] = None
        cls["key_catalyst"] = None
        cls["score_unavailable"] = True
        return cls

    # Объединяем: данные classify + данные score
    return {**cls, **{k: sc[k] for k in ("score", "estimated_prob_pct",
                                          "reasoning", "key_catalyst",
                                          "model", "from_cache")},
            "score_unavailable": False}


def _get_min_price(market: dict) -> float:
    """
    Возвращает минимальную цену среди всех outcome рынка.
    Использует outcome_prices, с fallback на last_trade_price.
    """
    prices_raw = market.get("outcome_prices", "{}")
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if prices:
            return min(float(p) for p in prices.values())
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return float(market.get("last_trade_price") or 0)


# =============================================================================
# Режим 2: deep_analyze — глубокий анализ (Claude)
# =============================================================================

def deep_analyze(market_id: str) -> str:
    """
    Режим 2: Глубокий анализ рынка через Claude Sonnet.

    Вызывается по команде /analyze {market_id}.
    Шаги: данные из БД → новости DuckDuckGo → предыдущая Gemini-классификация → Claude.
    Результат сохраняется в deep_analyses и возвращается для отправки в Telegram.
    """
    if not config.AI_DEEP_ANALYSIS_ENABLED:
        return "⚠️ Глубокий анализ отключён (AI_DEEP_ANALYSIS_ENABLED = False)."

    market = db.get_market_by_id(market_id)
    if not market:
        return f"⚠️ Рынок не найден: <code>{market_id}</code>"

    question    = market.get("question", "")
    description = (market.get("description") or "")[:500]
    end_date    = (market.get("end_date") or "")[:10]
    volume      = market.get("volume", 0)
    best_bid    = market.get("best_bid", 0)
    best_ask    = market.get("best_ask", 0)
    price       = _get_min_price(market)
    prob_pct    = round(price * 100, 1)

    # days_left
    try:
        from datetime import timezone as _tz
        end_dt = datetime.fromisoformat(end_date) if end_date else None
        if end_dt:
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=_tz.utc)
            days_left = (end_dt - datetime.now(_tz.utc)).days
        else:
            days_left = "?"
    except Exception:
        days_left = "?"

    # Ищем новости
    logger.info("Ищу новости для deep_analyze: %s", question[:50])
    news_results, _ = search_news(question, max_results=5)
    news_text = _format_news(news_results)

    # Предыдущая классификация Gemini (если есть оба этапа)
    prev_cls  = db.get_cached_classification(market["id"], 48, stage="classify")
    prev_sc   = db.get_cached_classification(market["id"], 48, stage="score")
    gemini_block = ""
    if prev_cls or prev_sc:
        category   = (prev_cls or {}).get("category", "—")
        sc_score   = (prev_sc or {}).get("score", "—")
        sc_prob    = (prev_sc or {}).get("estimated_prob_pct", "—")
        sc_cat     = (prev_sc or {}).get("key_catalyst", "—")
        gemini_block = f"""
## Предварительная оценка AI (Gemini, этапы 1+2)
Категория: {category}
Скор: {sc_score}/10
Оценка вероятности: {sc_prob}%
Катализатор: {sc_cat}
"""

    prompt = f"""Ты — квант-аналитик рынков предсказаний. Проведи глубокий анализ.

## Рынок
Вопрос: "{question}"
Описание: "{description}"
Текущая цена: ${price:.3f} (рынок оценивает вероятность в {prob_pct}%)
Объём торгов: ${volume:,.0f}
Спред: bid ${best_bid:.3f} / ask ${best_ask:.3f}
Экспирация: {end_date} (через {days_left} дней)
{gemini_block}
## Свежие новости по теме (DuckDuckGo)
{news_text}

## Задача
Дай развёрнутый анализ строго по этой структуре:

1. **Ситуация**: Что сейчас происходит? (3-4 предложения)
2. **Факторы ЗА** реализацию события: 2-3 конкретных фактора
3. **Факторы ПРОТИВ**: 2-3 фактора, почему рынок может быть прав
4. **Оценка вероятности**: X% (конкретное число)
5. **Edge**: разница с рыночной ценой ({prob_pct}%). >2x = интересно, <1.5x = не стоит
6. **Спред-анализ**: при текущем спреде bid/ask стоит ли входить или ставить лимитный ордер?
7. **Рекомендация**: ИНТЕРЕСНО / НЕЙТРАЛЬНО / ПРОПУСТИТЬ — и почему одним предложением
8. **Что отслеживать**: какой сигнал может резко изменить вероятность в ближайшие дни?"""

    system = (
        "Ты — опытный квант-аналитик prediction markets. "
        "Отвечай структурированно, конкретно, без воды. "
        "Давай честную оценку даже если она неудобна пользователю."
    )

    # Порядок: OpenRouter Claude → Gemini → прямой Claude
    logger.info("Запрашиваю deep_analyze (Claude/Gemini): %s", question[:40])
    response = call_claude_any(prompt, system_prompt=system)

    if not response:
        logger.warning("Claude недоступен для deep_analyze, пробую Gemini...")
        response = call_gemini_text(prompt + f"\n\nКонтекст: {system}", max_tokens=3000)

    if not response:
        return "⚠️ AI-анализ временно недоступен. Проверь API-ключи в config.py"

    header = (
        f"🔬 <b>ГЛУБОКИЙ АНАЛИЗ</b>\n"
        f"📌 {question[:70]}\n"
        f"💰 ${price:.3f} ({prob_pct}%) | Объём: ${volume:,.0f} | "
        f"Спред: {best_bid:.3f}/{best_ask:.3f}\n"
        f"─────────────────────\n\n"
    )
    full_text = header + response

    now_iso = datetime.now(timezone.utc).isoformat()
    db.save_deep_analysis(market["id"], full_text, "claude", now_iso)

    return full_text


# =============================================================================
# Режим 3: weekly_portfolio_review — еженедельная ревизия (Claude)
# =============================================================================

def weekly_portfolio_review() -> str:
    """
    Режим 3: Еженедельная AI-ревизия активных ставок через Claude.

    Вызывается автоматически каждое воскресенье из main.py.
    Для каждой ставки берёт текущую цену из БД и ищет свежие новости.
    """
    active_bets = db.get_active_bets()

    if not active_bets:
        return (
            "📊 <b>AI-РЕВИЗИЯ ПОРТФЕЛЯ</b>\n\n"
            "<i>Активных ставок нет.</i>"
        )

    bet_blocks = []
    for bet in active_bets:
        market = db.get_market_by_id(bet["market_id"])
        current_price = bet["entry_price"]
        if market:
            cp = _get_min_price(market)
            if cp > 0:
                current_price = cp

        entry = bet["entry_price"]
        change_pct = ((current_price - entry) / entry * 100) if entry > 0 else 0
        end_date = ""
        if market:
            end_date = (market.get("end_date") or "")[:10]
            try:
                from datetime import timezone as _tz
                end_dt = datetime.fromisoformat(end_date)
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=_tz.utc)
                days_left = (end_dt - datetime.now(_tz.utc)).days
            except Exception:
                days_left = "?"
        else:
            days_left = "?"

        news_list, _ = search_news(bet["question"] or "", max_results=2)
        news_snippet = news_list[0].get("body", "")[:150] if news_list else "Новостей не найдено."

        bet_blocks.append(
            f'- "{bet["question"]}"\n'
            f'  Купил по: ${entry:.3f} → Сейчас: ${current_price:.3f} ({change_pct:+.0f}%)\n'
            f'  Вложил: ${bet["amount_usd"]:.2f}\n'
            f'  Моя причина: "{bet.get("reason") or "не указана"}"\n'
            f'  Новости: {news_snippet}\n'
            f'  До экспирации: {days_left} дн.'
        )

    bets_text = "\n\n".join(bet_blocks)

    prompt = f"""Проведи еженедельную ревизию моего портфеля ставок на Polymarket.

## Мои активные ставки:
{bets_text}

Для каждой ставки определи одно из трёх:
- ДЕРЖАТЬ — ситуация не изменилась или усилилась в мою пользу
- ДОКУПИТЬ — появились новые факторы, стоит увеличить позицию
- ТРЕВОГА — ситуация ухудшилась, рынок скорректировался

В конце: общая оценка портфеля и главный риск на следующую неделю."""

    system = (
        "Ты — портфельный аналитик prediction markets. "
        "Будь конкретен, честен, практичен. "
        "Помни: пользователь не может автоматически продать позицию."
    )

    logger.info("Запрашиваю weekly_portfolio_review (Claude/Gemini)...")
    response = call_claude_any(prompt, system_prompt=system)

    if not response:
        logger.warning("Claude недоступен для weekly review, пробую Gemini...")
        response = call_gemini_text(prompt + f"\n\nКонтекст: {system}", max_tokens=3000)

    if not response:
        return "⚠️ <b>AI-ревизия недоступна.</b> Проверь API-ключи в config.py"

    now = datetime.now(timezone.utc)
    header = (
        f"🔬 <b>AI-РЕВИЗИЯ ПОРТФЕЛЯ</b>\n"
        f"<i>{now.strftime('%d.%m.%Y')} | {len(active_bets)} активных ставок</i>\n"
        f"─────────────────────\n\n"
    )
    return header + response


# =============================================================================
# Тест: python ai_analyst.py
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [ai] %(message)s",
        datefmt="%H:%M:%S",
    )

    db.init_db()
    markets = db.get_all_active_markets()

    if not markets:
        print("⚠️  База пустая. Сначала: python scanner.py")
        exit(1)

    # Берём первый рынок для теста
    test_market = markets[0]
    print(f"\n{'='*60}")
    print(f"Тест на рынке: {test_market['question'][:60]}")
    print(f"{'='*60}\n")

    # Тест 1: classify_market
    print("1️⃣  classify_market (Gemini Level 4)...")
    if config.GEMINI_API_KEY == "YOUR_GEMINI_API_KEY":
        print("   ⚠️  GEMINI_API_KEY не настроен")
    else:
        cls = classify_market(test_market)
        if cls:
            print(f"   ✅ category={cls['category']} | candidate={cls['is_candidate']} | score={cls['score']}/10")
            print(f"   reasoning: {cls['reasoning'][:80]}")
        else:
            print("   ❌ classify вернул None")

    print()

    # Тест 2: поиск новостей
    print("2️⃣  Поиск новостей (ddgs + fallback)...")
    question_test = test_market["question"]
    smart_query = make_search_query(question_test)
    print(f"   Вопрос: {question_test[:60]}")
    print(f"   Умный запрос: {smart_query}")
    results, used_query = search_news(question_test, max_results=3)
    print(f"   Использован запрос: «{used_query}» → {len(results)} результатов")
    for r in results:
        print(f"   • {r.get('title','')[:70]}")
