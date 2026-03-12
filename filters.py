"""
filters.py — Модуль 2 v2: Двойная воронка фильтрации по режимам.

🦢 SNIPER (геополитика/макро):
  L1: Цена 0.01–0.10
  L2: Объём ≥500, срок 1–90 дней
  L3: Keyword-фильтр (только для некатег. рынков)
  L4: LLM — черный лебедь? (асимметрия/механизм/недооценка)

⚡ CONVEYOR (спорт/киберспорт):
  L1: Цена 0.01–0.15
  L2: Объём ≥100, ближайшие 48 часов
  L3: Пропускается (рынки уже из спорт-тега)
  L4: LLM — андердог с реальными шансами?

Тест: `python filters.py` — выводит отфильтрованные рынки.
"""

import json
import logging
from datetime import datetime, timezone

import config
import db

logger = logging.getLogger(__name__)


# =============================================================================
# Вспомогательные функции
# =============================================================================

def _get_prices(market: dict) -> dict[str, float]:
    """
    Парсит JSON-строку outcome_prices в словарь {outcome: price}.
    Fallback: если пустой — пробует last_trade_price как цену "Yes".
    """
    raw = market.get("outcome_prices", "{}")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return {k: float(v) for k, v in prices.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # Fallback
    ltp = market.get("last_trade_price")
    if ltp:
        try:
            return {"Yes": float(ltp)}
        except (ValueError, TypeError):
            pass
    return {}


def _parse_end_dt(end_date_str: str | None) -> datetime | None:
    """Парсит строку даты в aware datetime (UTC). Возвращает None при ошибке."""
    if not end_date_str:
        return None
    try:
        s = end_date_str.strip()
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _days_until(end_date_str: str | None) -> float | None:
    """Количество дней от сейчас до даты экспирации."""
    dt = _parse_end_dt(end_date_str)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 86400


def _hours_until(end_date_str: str | None) -> float | None:
    """Количество часов от сейчас до даты экспирации (для конвейера)."""
    dt = _parse_end_dt(end_date_str)
    if dt is None:
        return None
    return (dt - datetime.now(timezone.utc)).total_seconds() / 3600


def _text_contains_any(text: str, keywords: list[str]) -> bool:
    """Проверяет регистронезависимое вхождение любого ключевого слова в текст."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _detect_keyword_tier(market: dict) -> str | None:
    """
    Определяет категорию рынка по WHITELIST_KEYWORDS (keyword-метод).
    Используется для некатегоризированных рынков в Level 3.
    Возвращает название категории или None.
    """
    text = " ".join(filter(None, [
        market.get("question", ""),
        market.get("description", ""),
    ]))
    for tier, keywords in config.WHITELIST_KEYWORDS.items():
        if _text_contains_any(text, keywords):
            return tier
    return None


# =============================================================================
# Level 1: Фильтр цены
# =============================================================================

def level1_price(market: dict) -> tuple[bool, list[tuple[str, float]]]:
    """
    Level 1 (SNIPER): Оставляем рынки с хотя бы одним outcome в SNIPER_MIN_PRICE..MAX_PRICE.
    Возвращает (True, список_подходящих_исходов) или (False, []).
    """
    prices = _get_prices(market)
    cheap = [
        (outcome, price)
        for outcome, price in prices.items()
        if config.SNIPER_MIN_PRICE <= price <= config.SNIPER_MAX_PRICE
    ]
    return bool(cheap), cheap


def level1_price_conveyor(market: dict) -> tuple[bool, list[tuple[str, float]]]:
    """
    Level 1 (CONVEYOR): Диапазон 0.01–0.15 — шире, подходит для спортивных андердогов.
    """
    prices = _get_prices(market)
    cheap = [
        (outcome, price)
        for outcome, price in prices.items()
        if config.CONVEYOR_MIN_PRICE <= price <= config.CONVEYOR_MAX_PRICE
    ]
    return bool(cheap), cheap


# =============================================================================
# Level 2: Фильтр ликвидности, времени, активности
# =============================================================================

def _calc_spread_meta(market: dict) -> dict:
    """Вычисляет spread_pct и spread_warning (общий хелпер)."""
    meta = {"spread_pct": None, "spread_warning": False}
    bid = market.get("best_bid") or 0
    ask = market.get("best_ask") or 0
    if ask > 0 and bid >= 0:
        sp = (ask - bid) / ask * 100
        meta["spread_pct"] = round(sp, 1)
        if sp > config.MAX_SPREAD_PCT:
            meta["spread_warning"] = True
    return meta


def level2_liquidity(market: dict) -> tuple[bool, dict]:
    """
    Level 2 (SNIPER): Объём ≥ SNIPER_MIN_VOLUME, срок 1–90 дней.
    """
    meta = {"days_left": None, "hours_left": None, "spread_pct": None, "spread_warning": False}

    if not market.get("active", 1):
        return False, meta
    if not market.get("enable_order_book", 1):
        return False, meta
    if (market.get("volume") or 0) < config.SNIPER_MIN_VOLUME:
        return False, meta

    days = _days_until(market.get("end_date"))
    meta["days_left"] = round(days, 1) if days is not None else None
    if days is None:
        return False, meta
    if not (config.SNIPER_MIN_DAYS_EXPIRY <= days <= config.SNIPER_MAX_DAYS_EXPIRY):
        return False, meta

    meta.update(_calc_spread_meta(market))
    return True, meta


def level2_liquidity_conveyor(market: dict) -> tuple[bool, dict]:
    """
    Level 2 (CONVEYOR): Объём ≥ CONVEYOR_MIN_VOLUME, событие в ближайшие 48 часов.
    hours_left добавляется в метрики для алерта.
    """
    meta = {"days_left": None, "hours_left": None, "spread_pct": None, "spread_warning": False}

    if not market.get("active", 1):
        return False, meta
    if (market.get("volume") or 0) < config.CONVEYOR_MIN_VOLUME:
        return False, meta

    hours = _hours_until(market.get("end_date"))
    meta["hours_left"] = round(hours, 1) if hours is not None else None
    meta["days_left"]  = round(hours / 24, 1) if hours is not None else None
    if hours is None:
        return False, meta
    # Только матчи в ближайшие N часов, и не уже закончившиеся
    if not (0 < hours <= config.CONVEYOR_MAX_HOURS_TO_EVENT):
        return False, meta

    meta.update(_calc_spread_meta(market))
    return True, meta


# =============================================================================
# Level 3: Keyword-фильтр (только для некатегоризированных sniper-рынков)
# =============================================================================

def level3_keywords(market: dict) -> tuple[bool, str | None]:
    """
    Level 3 (SNIPER): Keyword-фильтр для рынков без целевого тега (source_tag_id == 0).

    Рынки из SNIPER_TAG_IDS пропускают этот уровень.
    Для некатег.: отсекает blacklist, ищет whitelist-категорию.

    Возвращает (True, keyword_tier | None) или (False, None).
    """
    source_tag = market.get("source_tag_id", 0)

    if source_tag in config.SNIPER_TAG_IDS:
        return True, None

    text = " ".join(filter(None, [
        market.get("question", ""),
        market.get("description", ""),
    ]))
    if _text_contains_any(text, config.BLACKLIST_KEYWORDS):
        return False, None

    tier = _detect_keyword_tier(market)
    return True, tier


# =============================================================================
# Level 4: Двухэтапный LLM-классификатор (единый для sniper и conveyor)
# =============================================================================

def level4_llm(market: dict) -> tuple[bool, dict | None]:
    """
    Level 4: Двухэтапная AI-классификация.

    Этап 1 — classify(): БЕЗ интернета, только текст рынка.
      Если is_candidate=False → рынок отклонён, дальше не идёт.
    Этап 2 — score(): С DuckDuckGo-новостями, только для кандидатов.
      Возвращает score + reasoning + catalyst на основе свежих данных.

    Fallback при недоступности AI:
      - classify недоступен → пропускаем в GRAY без AI-данных
      - score недоступен → пропускаем в GRAY с пометкой score_unavailable

    Зонирование:
      - is_candidate=False → отклонён
      - is_candidate=True, score >= AI_MIN_SCORE_TO_ALERT, category != OTHER → GREEN
      - is_candidate=True, score < AI_MIN_SCORE_TO_ALERT или category == OTHER → GRAY
    """
    if not config.AI_CLASSIFICATION_ENABLED:
        return True, None

    from ai_analyst import classify, score as ai_score

    # --- Этап 1: classify (без интернета) ---
    cls = classify(market)

    if cls is None:
        # AI полностью недоступен → пропускаем в GRAY
        return True, None

    if not cls.get("is_candidate"):
        logger.debug(
            "Level 4 [classify] отклонил: %s | %s",
            market.get("question", "")[:50],
            cls.get("reject_reason", "—")[:60]
        )
        return False, cls

    # --- Этап 2: score (с новостями) — только для кандидатов ---
    sc = ai_score(market, cls)

    if sc is None:
        # Score недоступен → кандидат без скора, идёт в GRAY
        logger.warning(
            "Level 4 [score] недоступен для кандидата: %s → GRAY без скора",
            market.get("question", "")[:50]
        )
        cls["score"] = None
        cls["score_unavailable"] = True
        return True, cls

    # Объединяем: данные classify + score
    combined = {**cls, **{k: sc[k] for k in
                           ("score", "estimated_prob_pct", "reasoning",
                            "key_catalyst", "model", "from_cache")},
                "score_unavailable": False}
    return True, combined


# =============================================================================
# Определение зоны (green / gray) — учитывает mode
# =============================================================================

def _determine_zone(market: dict, keyword_tier: str | None,
                    cls: dict | None) -> tuple[str, str | None]:
    """
    Определяет зону рынка (green/gray) и финальную категорию.

    Зона GREEN:
      SNIPER:   is_candidate=True + score >= SNIPER_MIN_AI_SCORE + category != OTHER
      CONVEYOR: is_candidate=True + score >= CONVEYOR_MIN_AI_SCORE
    """
    source_tag_id = market.get("source_tag_id", 0)
    mode = market.get("mode", "sniper")

    # Приоритет категории: LLM > keyword > тег
    category = None
    if cls and cls.get("category") and cls["category"] not in ("OTHER", None):
        category = cls["category"]
    elif keyword_tier:
        category = keyword_tier
    else:
        tag_names = {v: k for k, v in config.POLYMARKET_TAGS.items()}
        category = tag_names.get(source_tag_id)

    if not cls:
        return "gray", category

    score    = cls.get("score") or 0
    cat      = cls.get("category", "OTHER")
    min_score = config.CONVEYOR_MIN_AI_SCORE if mode == "conveyor" else config.SNIPER_MIN_AI_SCORE

    if cls.get("is_candidate") and score >= min_score:
        if mode == "conveyor" or cat != "OTHER":
            return "green", category or cat
    return "gray", category or cat


# =============================================================================
# Пайплайны по режимам
# =============================================================================

def _ai_counter_ok(cnt: dict) -> bool:
    """Проверяет, не достигнут ли лимит AI-запросов за цикл."""
    if cnt.get("ai_new", 0) >= config.AI_MAX_PER_CYCLE:
        logger.warning(
            "Достигнут лимит AI_MAX_PER_CYCLE=%d за цикл.", config.AI_MAX_PER_CYCLE
        )
        return False
    return True


def _build_result_entry(market: dict, cheap_outcomes: list, meta: dict,
                        keyword_tier: str | None, cls: dict | None) -> dict:
    """Собирает финальный словарь рынка с добавленными полями фильтрации."""
    zone, category = _determine_zone(market, keyword_tier, cls)
    return {
        **market,
        "cheap_outcomes":    cheap_outcomes,
        "days_left":         meta.get("days_left"),
        "hours_left":        meta.get("hours_left"),
        "spread_pct":        meta.get("spread_pct"),
        "spread_warning":    meta.get("spread_warning", False),
        "zone":              zone,
        "category":          category,
        "ai_score":          cls.get("score") if cls else None,
        "ai_estimated_prob": cls.get("estimated_prob_pct") if cls else None,
        "ai_reasoning":      cls.get("reasoning") if cls else None,
        "ai_catalyst":       cls.get("key_catalyst") if cls else None,
    }


def run_sniper_filters(markets: list[dict], ai_cnt: dict) -> list[dict]:
    """
    Пайплайн для режима 🦢 SNIPER.
    L1: цена 0.01–0.10 | L2: объём≥500, 1–90 дней | L3: keywords | L4: LLM черный лебедь
    ai_cnt — общий счётчик AI-запросов (передаётся по ссылке для лимита AI_MAX_PER_CYCLE).
    """
    cnt = {"L1": 0, "L2": 0, "L3": 0, "L4": 0}
    result = []

    for market in markets:
        ok, cheap = level1_price(market)
        if not ok:
            continue
        cnt["L1"] += 1

        ok, meta = level2_liquidity(market)
        if not ok:
            continue
        cnt["L2"] += 1

        ok, keyword_tier = level3_keywords(market)
        if not ok:
            continue
        cnt["L3"] += 1

        if not _ai_counter_ok(ai_cnt):
            break
        ok, cls = level4_llm(market)
        if not ok:
            continue
        cnt["L4"] += 1
        if cls and not cls.get("from_cache"):
            ai_cnt["ai_new"] = ai_cnt.get("ai_new", 0) + 1

        result.append(_build_result_entry(market, cheap, meta, keyword_tier, cls))

    logger.info(
        "🦢 SNIPER воронка: %d → L1=%d → L2=%d → L3=%d → L4=%d → итого=%d",
        len(markets), cnt["L1"], cnt["L2"], cnt["L3"], cnt["L4"], len(result)
    )
    return result


def run_conveyor_filters(markets: list[dict], ai_cnt: dict) -> list[dict]:
    """
    Пайплайн для режима ⚡ CONVEYOR.
    L1: цена 0.01–0.15 | L2: объём≥100, событие в 48ч | L3: пропускается | L4: LLM андердог
    """
    cnt = {"L1": 0, "L2": 0, "L4": 0}
    result = []

    for market in markets:
        ok, cheap = level1_price_conveyor(market)
        if not ok:
            continue
        cnt["L1"] += 1

        ok, meta = level2_liquidity_conveyor(market)
        if not ok:
            continue
        cnt["L2"] += 1

        # L3 пропускается для конвейера — рынки уже из спортивного тега

        if not _ai_counter_ok(ai_cnt):
            break
        ok, cls = level4_llm(market)
        if not ok:
            continue
        cnt["L4"] += 1
        if cls and not cls.get("from_cache"):
            ai_cnt["ai_new"] = ai_cnt.get("ai_new", 0) + 1

        result.append(_build_result_entry(market, cheap, meta, None, cls))

    logger.info(
        "⚡ CONVEYOR воронка: %d → L1=%d → L2=%d → L4=%d → итого=%d",
        len(markets), cnt["L1"], cnt["L2"], cnt["L4"], len(result)
    )
    return result


# =============================================================================
# Главная функция воронки
# =============================================================================

def run_filters(markets: list[dict]) -> list[dict]:
    """
    Разделяет рынки по mode и прогоняет через соответствующий пайплайн.

    Каждый прошедший рынок обогащается:
      cheap_outcomes, days_left, hours_left, spread_pct, spread_warning,
      zone (green/gray), category, ai_score, ai_estimated_prob,
      ai_reasoning, ai_catalyst

    Возвращает отсортированный список:
    green первые (по score DESC), затем gray (по price ASC).
    """
    sniper_markets   = [m for m in markets if m.get("mode", "sniper") == "sniper"]
    conveyor_markets = [m for m in markets if m.get("mode") == "conveyor"]

    # Общий счётчик AI-запросов (лимит AI_MAX_PER_CYCLE на весь цикл)
    ai_cnt: dict = {"ai_new": 0}

    result = []
    if config.SNIPER_ENABLED:
        result.extend(run_sniper_filters(sniper_markets, ai_cnt))
    if config.CONVEYOR_ENABLED:
        result.extend(run_conveyor_filters(conveyor_markets, ai_cnt))

    logger.info(
        "Фильтрация завершена: %d рынков → %d прошли (🦢 sniper: %d | ⚡ conveyor: %d)",
        len(markets),
        len(result),
        len([m for m in result if m.get("mode", "sniper") == "sniper"]),
        len([m for m in result if m.get("mode") == "conveyor"]),
    )

    # Сортировка: green по score DESC, затем gray по цене ASC
    def sort_key(m):
        zone_order = 0 if m["zone"] == "green" else 1
        if m["zone"] == "green":
            return (zone_order, -(m.get("ai_score") or 0))
        min_p = min(p for _, p in m["cheap_outcomes"]) if m["cheap_outcomes"] else 1.0
        return (zone_order, min_p)

    result.sort(key=sort_key)
    return result


# =============================================================================
# Тест: python filters.py
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [filters] %(message)s",
        datefmt="%H:%M:%S",
    )

    db.init_db()
    markets = db.get_all_active_markets()

    if not markets:
        print("⚠️  База пустая. Сначала: python scanner.py")
        exit(1)

    filtered  = run_filters(markets)
    green     = [m for m in filtered if m["zone"] == "green"]
    gray      = [m for m in filtered if m["zone"] == "gray"]
    sniper_g  = [m for m in green if m.get("mode", "sniper") == "sniper"]
    conveyor_g = [m for m in green if m.get("mode") == "conveyor"]

    print(f"\n{'='*65}")
    print(f"Black Swan Hunter v2 — Результаты фильтрации")
    print(f"{'='*65}")
    print(f"Рынков в БД: {len(markets)} → После фильтров: {len(filtered)}")
    print(f"  🟢 GREEN: {len(green)} (🦢 sniper: {len(sniper_g)} | ⚡ conveyor: {len(conveyor_g)})")
    print(f"  ⚪ GRAY:  {len(gray)}")
    print()

    if sniper_g:
        print(f"🦢 SNIPER — GREEN ({len(sniper_g)}):")
        print("-" * 65)
        for m in sniper_g[:10]:
            for outcome, price in m["cheap_outcomes"]:
                score_str = f"AI:{m['ai_score']}/10" if m.get("ai_score") else "no AI"
                spread_str = " ⚠️ WIDE" if m.get("spread_warning") else ""
                print(f"  ${price:.3f} [{outcome}] [{m.get('category','?')}] "
                      f"[{m.get('days_left','?'):.0f}д] [{score_str}]{spread_str}")
                print(f"    {m['question'][:60]}")

    if conveyor_g:
        print(f"\n⚡ CONVEYOR — GREEN ({len(conveyor_g)}):")
        print("-" * 65)
        for m in conveyor_g[:10]:
            for outcome, price in m["cheap_outcomes"]:
                score_str = f"AI:{m['ai_score']}/10" if m.get("ai_score") else "no AI"
                hrs = m.get("hours_left")
                time_str = f"{hrs:.0f}ч" if hrs is not None else "?"
                print(f"  ${price:.3f} [{outcome}] [{score_str}] через {time_str}")
                print(f"    {m['question'][:60]}")

    if gray:
        print(f"\n⚪ GRAY ZONE ({len(gray)}):")
        print("-" * 65)
        for m in gray[:10]:
            for outcome, price in m["cheap_outcomes"]:
                score_str = f"AI:{m['ai_score']}/10" if m.get("ai_score") else "no AI"
                mode_icon = "⚡" if m.get("mode") == "conveyor" else "🦢"
                print(f"  {mode_icon} ${price:.3f} [{outcome}] [{m.get('days_left','?'):.0f}д] "
                      f"[{score_str}] {m['question'][:50]}")
