"""
panic_monitor.py — Модуль 6 v2+: Мониторинг резких движений + Режим Стервятник.

Две независимые функции:
  run_panic_check(markets)    — Паника: крупные рынки прыгнули ≥15 п.п. за час.
  check_price_movers(markets) — Стервятник 🦅: дешёвые рынки тихо выросли на 50%+.
    Разница:
    - Паника реактивная — реагируем на движение В КРУПНЫХ рынках.
    - Стервятник проактивный — ловим момент входа пока рынок ещё дешёвый.
"""

import json
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

import config
import db
from alerter import (
    send_telegram_message,
    format_panic_alert,
    format_cluster_panic_alert,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Определение категории рынка для кластерного анализа
# =============================================================================

def _get_market_category(market: dict) -> str:
    """
    Определяет категорию рынка для группировки при кластерной панике.

    Приоритет: AI-классификация из кэша → source_tag_id → "unknown".
    """
    # Пробуем кэш AI-классификации
    cls = db.get_cached_classification(market["id"], 48)
    if cls and cls.get("category") and cls["category"] != "OTHER":
        return cls["category"]

    # Из source_tag_id
    tag_names = {v: k for k, v in config.POLYMARKET_TAGS.items()}
    tag_name  = tag_names.get(market.get("source_tag_id", 0))
    return tag_name or "unknown"


# =============================================================================
# Основная логика паники
# =============================================================================

def check_single_panic(market: dict) -> bool:
    """
    Проверяет отдельный рынок на "одиночную панику".

    Условие: one_hour_price_change >= PANIC_THRESHOLD_PCT
    и объём >= PANIC_VOLUME_THRESHOLD.

    Возвращает True если паника обнаружена.
    """
    if market.get("volume", 0) < config.PANIC_VOLUME_THRESHOLD:
        return False
    change = abs(market.get("one_hour_price_change") or 0)
    return change >= config.PANIC_THRESHOLD_PCT


def find_related_cheap_markets(panic_market: dict,
                                all_markets: list[dict]) -> list[dict]:
    """
    Ищет дешёвые рынки (1-10 центов), тематически близкие к запаниковавшему.

    "Близкие" = та же категория или пересечение слов из question (≥2 слова).
    Возвращает прошедшие ценовой фильтр рынки для паник-скана.
    """
    from filters import run_filters, level1_price

    panic_cat   = _get_market_category(panic_market)
    panic_words = {
        w.lower() for w in (panic_market.get("question") or "").split()
        if len(w) > 4
    }

    candidates = []
    for m in all_markets:
        if m["id"] == panic_market["id"]:
            continue
        cat   = _get_market_category(m)
        words = {w.lower() for w in (m.get("question") or "").split() if len(w) > 4}

        if cat == panic_cat or len(panic_words & words) >= 2:
            ok, cheap = level1_price(m)
            if ok:
                candidates.append(m)

    logger.info("Паник-скан: %d кандидатов для %s",
                len(candidates), panic_market.get("question", "")[:40])
    return candidates


def check_cluster_panic(markets: list[dict]) -> list[tuple[str, list[dict]]]:
    """
    Проверяет наличие кластерной паники:
    >CLUSTER_PANIC_MIN рынков в одной категории с oneDayPriceChange > threshold.

    Возвращает список (category, список_рынков) для категорий с кластерной паникой.
    """
    by_category = defaultdict(list)

    for market in markets:
        day_change = abs(market.get("one_day_price_change") or 0)
        if day_change >= config.CLUSTER_PANIC_DAY_CHANGE:
            cat = _get_market_category(market)
            by_category[cat].append(market)

    clusters = [
        (cat, ms) for cat, ms in by_category.items()
        if len(ms) >= config.CLUSTER_PANIC_MIN
    ]
    return clusters


# =============================================================================
# Главная функция
# =============================================================================

def run_panic_check(all_markets: list[dict]) -> list[dict]:
    """
    Полная проверка на панику по результатам последнего скана.

    v2: использует поля one_hour_price_change и one_day_price_change
    прямо из API — не нужны исторические данные из price_history.

    Алгоритм:
    1. Одиночная паника: рынок прыгнул ≥15 п.п. за час → экстренный алерт + паник-скан
    2. Кластерная паника: ≥3 рынков в категории с дневным изменением ≥10 п.п. → алерт

    Возвращает список рынков, алерченных в ходе паник-сканов.
    """
    large_markets = [
        m for m in all_markets
        if (m.get("volume") or 0) >= config.PANIC_VOLUME_THRESHOLD
    ]

    if not large_markets:
        logger.debug("Нет крупных рынков (> $%d) для паник-мониторинга.",
                     config.PANIC_VOLUME_THRESHOLD)
        return []

    logger.info("Паник-монитор: проверяю %d крупных рынков...", len(large_markets))

    alerted_in_panic = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # --- 1. Одиночная паника ---
    for market in large_markets:
        if not check_single_panic(market):
            continue

        change = abs(market.get("one_hour_price_change") or 0)
        # Вычисляем "до" и "после"
        price_now = market.get("last_trade_price") or 0
        price_old = price_now - (market.get("one_hour_price_change") or 0)
        old_pct = round(price_old * 100, 1)
        new_pct = round(price_now * 100, 1)

        logger.warning(
            "🚨 ПАНИКА: %s | %.1f%% → %.1f%% (+%.1f п.п./час)",
            market.get("question", "")[:45], old_pct, new_pct, change
        )

        # Алерт о панике
        panic_text = format_panic_alert(market, old_pct, new_pct)
        send_telegram_message(panic_text)

        # Паник-скан смежных рынков
        related = find_related_cheap_markets(market, all_markets)

        if related:
            from alerter import format_alert
            from filters import run_filters
            filtered_related = run_filters(related)
            count_sent = 0
            for rel in filtered_related:
                cheap = rel.get("cheap_outcomes", [])
                if not cheap:
                    continue
                outcome, price = min(cheap, key=lambda x: x[1])
                # Принудительно помечаем как panic_scan
                rel["zone"] = "green"
                text = format_alert(rel, outcome, price, alert_type="panic_scan")
                if send_telegram_message(text):
                    db.mark_as_alerted(rel["id"], price, now_iso, "panic_scan")
                    alerted_in_panic.append(rel)
                    count_sent += 1

            logger.info("Паник-скан: отправлено %d алертов.", count_sent)

            if count_sent == 0:
                send_telegram_message(
                    "⚡ <b>Паник-скан завершён</b>\n"
                    "<i>Смежных дешёвых рынков не найдено.</i>"
                )

    # --- 2. Кластерная паника ---
    clusters = check_cluster_panic(all_markets)
    for category, cluster_markets in clusters:
        logger.warning(
            "🚨 КЛАСТЕРНАЯ ПАНИКА [%s]: %d рынков с дневными движениями ≥%d п.п.",
            category, len(cluster_markets), config.CLUSTER_PANIC_DAY_CHANGE
        )
        text = format_cluster_panic_alert(category, len(cluster_markets), cluster_markets)
        send_telegram_message(text)

    return alerted_in_panic


# =============================================================================
# 🦅 Режим Стервятник — check_price_movers
# =============================================================================

def _get_current_price(market: dict) -> float:
    """Возвращает текущую минимальную цену outcome рынка."""
    raw = market.get("outcome_prices", "{}")
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if prices:
            return min(float(p) for p in prices.values())
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return float(market.get("last_trade_price") or 0)


def _passes_l1_l2(market: dict) -> bool:
    """
    Быстрая проверка L1+L2 для стервятника (без L3 keywords и L4 LLM).
    Нужно чтобы отсечь явный мусор и неликвид.
    """
    price = _get_current_price(market)
    if not (config.MIN_PRICE <= price <= config.MAX_PRICE):
        return False
    if (market.get("volume") or 0) < config.MIN_VOLUME:
        return False
    if not market.get("active", 1) or not market.get("enable_order_book", 1):
        return False
    return True


def check_price_movers(all_markets: list[dict]) -> list[dict]:
    """
    Режим Стервятник 🦅: ловим дешёвые рынки с резким ростом цены.

    Срабатывает когда выполняется ЛЮБОЕ из условий:
    A) price_history: цена 24ч назад была ≤ VULTURE_MIN_OLD_PRICE ($0.02)
       И текущая цена ≥ VULTURE_MIN_NEW_PRICE ($0.03)
       И рост ≥ VULTURE_MIN_CHANGE_PCT (50%)
    B) oneDayPriceChange из API ≥ VULTURE_MIN_CHANGE_PP (2 п.п.)
       И рынок дешёвый (цена ≤ MAX_PRICE)

    Плюс: volume_24h ≥ VULTURE_MIN_VOLUME_24H ($300) — чтобы отсечь одиночные сделки.

    Для каждого триггера:
    - Прогоняет через score() (этап 2 — с DuckDuckGo)
    - Отправляет 🦅 алерт
    - Сохраняет в alerted_markets с alert_type='vulture'

    Возвращает список алерченных рынков.
    """
    from alerter import format_vulture_alert
    from ai_analyst import score as ai_score, classify as ai_classify

    now_iso   = datetime.now(timezone.utc).isoformat()
    triggered = []

    # Только рынки прошедшие L1+L2 (ценовой и ликвидность)
    candidates = [m for m in all_markets if _passes_l1_l2(m)]
    logger.info("Стервятник: проверяю %d кандидатов (прошли L1+L2)...", len(candidates))

    for market in candidates:
        market_id   = market["id"]
        current_prc = _get_current_price(market)
        volume_24h  = market.get("volume_24h") or 0

        # Фильтр по объёму — без него любая одна сделка даёт ложный сигнал
        if volume_24h < config.VULTURE_MIN_VOLUME_24H:
            continue

        # Антидупликат: один стервятник-алерт в 24ч на рынок
        if db.get_alerted_vulture(market_id):
            continue

        old_price = None
        triggered_by = None

        # --- Условие A: сравнение с price_history ---
        hist_price = db.get_price_24h_ago(market_id)
        if hist_price is not None:
            if (hist_price <= config.VULTURE_MIN_OLD_PRICE and
                    current_prc >= config.VULTURE_MIN_NEW_PRICE and
                    hist_price > 0):
                change_pct = (current_prc - hist_price) / hist_price * 100
                if change_pct >= config.VULTURE_MIN_CHANGE_PCT:
                    old_price    = hist_price
                    triggered_by = f"history: ${hist_price:.3f}→${current_prc:.3f} (+{change_pct:.0f}%)"

        # --- Условие B: oneDayPriceChange из API ---
        if triggered_by is None:
            day_change = abs(market.get("one_day_price_change") or 0)
            if day_change >= config.VULTURE_MIN_CHANGE_PP:
                old_price    = current_prc - (market.get("one_day_price_change") or 0)
                triggered_by = f"API dayChange: +{day_change:.1f}pp"

        if triggered_by is None:
            continue

        logger.info(
            "🦅 Стервятник сработал: %s | %s | vol24h=$%.0f",
            market.get("question", "")[:50], triggered_by, volume_24h
        )

        # --- AI score (этап 2 с DuckDuckGo) ---
        # Сначала нужна classify-запись для передачи в score()
        cls = db.get_cached_classification(market_id, 48, stage="classify")
        if cls is None:
            # Классификация отсутствует — делаем быструю
            cls = ai_classify(market)
        sc = ai_score(market, cls) if cls else None

        # --- Алерт ---
        text = format_vulture_alert(market, old_price or current_prc,
                                    current_prc, sc)
        if send_telegram_message(text):
            db.mark_as_alerted(market_id, current_prc, now_iso, alert_type="vulture")
            triggered.append(market)
            logger.info("🦅 Алерт отправлен: %s", market.get("question", "")[:50])

    logger.info("Стервятник: %d сработавших алертов.", len(triggered))
    return triggered


# =============================================================================
# Тест: python panic_monitor.py
# =============================================================================

if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(
        level=_log.INFO,
        format="%(asctime)s %(levelname)s [panic] %(message)s",
        datefmt="%H:%M:%S",
    )
    db.init_db()
    markets = db.get_all_active_markets()

    print(f"Рынков в БД: {len(markets)}")
    large = [m for m in markets if (m.get("volume") or 0) >= config.PANIC_VOLUME_THRESHOLD]
    print(f"Крупных (> ${config.PANIC_VOLUME_THRESHOLD:,}): {len(large)}")

    # Статистика price changes
    with_h_change = [m for m in markets if abs(m.get("one_hour_price_change") or 0) > 0]
    with_d_change = [m for m in markets if abs(m.get("one_day_price_change") or 0) > 0]
    print(f"Рынков с hourly change: {len(with_h_change)}")
    print(f"Рынков с daily change: {len(with_d_change)}")

    # Топ по hourly change
    if with_h_change:
        top = sorted(with_h_change, key=lambda x: abs(x.get("one_hour_price_change") or 0),
                     reverse=True)[:5]
        print("\nТоп-5 по hourly change:")
        for m in top:
            print(f"  {m.get('one_hour_price_change',0):+.1f}pp — {m['question'][:55]}")

    # Кластерная паника
    clusters = check_cluster_panic(markets)
    if clusters:
        print(f"\n⚠️  Кластерная паника в {len(clusters)} категориях:")
        for cat, ms in clusters:
            print(f"  [{cat}]: {len(ms)} рынков")
    else:
        print("\n✅ Кластерной паники нет.")
