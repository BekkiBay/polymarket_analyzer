"""
scanner.py — Модуль 1 v2: Сбор данных с Polymarket через tag-based /events API.

Два режима сканирования:
  🦢 SNIPER   — теги politics/geopolitics/finance/crypto (SNIPER_TAG_IDS)
  ⚡ CONVEYOR — тег sports/esports (CONVEYOR_TAG_IDS)

Каждый рынок получает поле mode='sniper'|'conveyor' для дальнейшей маршрутизации
в filters.py и ai_analyst.py.

Тест: `python scanner.py` — выводит количество рынков по каждому тегу.
"""

import json
import logging
import time
from datetime import datetime, timezone

import requests

import config
import db

logger = logging.getLogger(__name__)

# Обратный словарь tag_id → название для логов
TAG_NAMES = {v: k for k, v in config.POLYMARKET_TAGS.items()}

# Быстрая проверка: в каком наборе тегов находится tag_id
_SNIPER_TAG_SET   = set(config.SNIPER_TAG_IDS)
_CONVEYOR_TAG_SET = set(config.CONVEYOR_TAG_IDS)


def _mode_for_tag(tag_id: int) -> str:
    """Возвращает 'conveyor' для спортивных тегов, иначе 'sniper'."""
    return "conveyor" if tag_id in _CONVEYOR_TAG_SET else "sniper"


# =============================================================================
# HTTP-клиент с retry
# =============================================================================

def fetch_with_retry(url: str, params: dict = None) -> list | dict | None:
    """
    GET-запрос с повторными попытками при сетевых ошибках.
    Возвращает десериализованный JSON или None при неудаче.
    """
    for attempt in range(1, config.REQUEST_RETRY_COUNT + 1):
        try:
            resp = requests.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.warning("HTTP ошибка [%d/%d]: %s — %s",
                           attempt, config.REQUEST_RETRY_COUNT, url, e)
        except requests.exceptions.ConnectionError as e:
            logger.warning("Соединение [%d/%d]: %s — %s",
                           attempt, config.REQUEST_RETRY_COUNT, url, e)
        except requests.exceptions.Timeout:
            logger.warning("Таймаут [%d/%d]: %s", attempt, config.REQUEST_RETRY_COUNT, url)
        except requests.exceptions.RequestException as e:
            logger.error("Критическая ошибка запроса: %s", e)
            return None
        if attempt < config.REQUEST_RETRY_COUNT:
            time.sleep(config.REQUEST_RETRY_DELAY)
    logger.error("Все попытки исчерпаны: %s", url)
    return None


# =============================================================================
# Парсинг рынков из событий
# =============================================================================

def _safe_float(value, default: float = 0.0) -> float:
    """Безопасно конвертирует значение в float, возвращает default при ошибке."""
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def parse_market_from_event(raw: dict, event_slug: str, source_tag_id: int) -> dict | None:
    """
    Парсит рынок из вложенного объекта события Gamma API.

    Извлекает все поля v2 схемы: bid/ask, price changes, volume_24h, slug.
    Возвращает нормализованный словарь или None если нет обязательных полей.
    """
    market_id = raw.get("conditionId") or raw.get("id")
    question  = raw.get("question") or raw.get("title")

    if not market_id or not question:
        return None

    # Парсим outcomePrices: JSON-строка вида '["0.03","0.97"]'
    outcomes_raw     = raw.get("outcomes", "[]")
    out_prices_raw   = raw.get("outcomePrices", "[]")
    try:
        outcomes    = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        out_prices  = json.loads(out_prices_raw) if isinstance(out_prices_raw, str) else out_prices_raw
        prices_dict = {str(o): float(p) for o, p in zip(outcomes, out_prices)}
    except (json.JSONDecodeError, TypeError, ValueError):
        prices_dict = {}

    # Парсим теги события
    tags_raw = raw.get("tags") or []
    try:
        tags_str = json.dumps(tags_raw) if not isinstance(tags_raw, str) else tags_raw
    except Exception:
        tags_str = "[]"

    # Дата экспирации
    end_date_raw = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end")
    if isinstance(end_date_raw, (int, float)):
        end_date = datetime.fromtimestamp(end_date_raw, tz=timezone.utc).isoformat()
    else:
        end_date = end_date_raw

    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        "id":                    market_id,
        "condition_id":          market_id,
        "question":              question,
        "description":           raw.get("description"),
        "slug":                  raw.get("slug"),
        "event_slug":            event_slug,
        "outcomes":              json.dumps(outcomes),
        "outcome_prices":        json.dumps(prices_dict),
        "best_bid":              _safe_float(raw.get("bestBid")),
        "best_ask":              _safe_float(raw.get("bestAsk")),
        "last_trade_price":      _safe_float(raw.get("lastTradePrice")),
        "volume":                _safe_float(raw.get("volumeNum") or raw.get("volume")),
        "volume_24h":            _safe_float(raw.get("volume24hr")),
        "liquidity":             _safe_float(raw.get("liquidityNum") or raw.get("liquidity")),
        "end_date":              end_date,
        "tags":                  tags_str,
        "one_hour_price_change": _safe_float(raw.get("oneHourPriceChange")),
        "one_day_price_change":  _safe_float(raw.get("oneDayPriceChange")),
        "enable_order_book":     1 if raw.get("enableOrderBook") else 0,
        "active":                1 if raw.get("active", True) else 0,
        "source_tag_id":         source_tag_id,
        "mode":                  _mode_for_tag(source_tag_id),
        "first_seen_at":         now_iso,
        "last_checked_at":       now_iso,
    }


# =============================================================================
# Сканирование по тегу
# =============================================================================

def fetch_events_by_tag(tag_id: int) -> list[dict]:
    """
    Загружает все события по одному tag_id из Gamma API /events.

    Использует пагинацию (offset) до опустошения страниц или достижения MAX_SCAN_PAGES.
    Параметр related_tags=true захватывает родственные категории.
    Возвращает список нормализованных рынков (уже прошедших parse_market_from_event).
    """
    tag_name = TAG_NAMES.get(tag_id, str(tag_id))
    all_markets = []
    seen_ids    = set()
    offset      = 0
    limit       = 100
    page        = 1

    logger.info("Сканирую тег [%s] (tag_id=%d)...", tag_name, tag_id)

    while page <= config.MAX_SCAN_PAGES:
        params = {
            "tag_id":       tag_id,
            "related_tags": "true",
            "closed":       "false",
            "active":       "true",
            "order":        "volume",
            "ascending":    "false",
            "limit":        limit,
            "offset":       offset,
        }
        data = fetch_with_retry(f"{config.GAMMA_API_BASE}/events", params=params)

        if not data:
            break

        events = data if isinstance(data, list) else data.get("events", data.get("results", []))
        if not events:
            break

        page_markets = 0
        for event in events:
            event_slug = event.get("slug", "")
            markets_raw = event.get("markets") or []

            # Если у события нет вложенных рынков — само событие может быть рынком
            if not markets_raw and event.get("conditionId"):
                markets_raw = [event]

            for raw_market in markets_raw:
                market = parse_market_from_event(raw_market, event_slug, tag_id)
                if market and market["id"] not in seen_ids:
                    seen_ids.add(market["id"])
                    all_markets.append(market)
                    page_markets += 1

        logger.info("  Страница %d [%s]: %d событий, %d рынков (всего: %d)",
                    page, tag_name, len(events), page_markets, len(all_markets))

        if len(events) < limit:
            break

        offset += limit
        page   += 1
        time.sleep(config.API_SLEEP_BETWEEN)

    logger.info("Тег [%s]: итого %d уникальных рынков.", tag_name, len(all_markets))
    return all_markets


def fetch_uncategorized_markets(exclude_ids: set, limit_pages: int = 5) -> list[dict]:
    """
    Загружает свежие/популярные рынки без тега — они могут не быть категоризированы.

    Фильтрует по активности и объёму, исключает уже найденные и спортивные теги.
    Возвращает список нормализованных рынков с source_tag_id=0.
    """
    all_markets = []
    offset = 0
    limit  = 100

    logger.info("Сканирую некатегоризированные рынки...")

    for page in range(1, limit_pages + 1):
        params = {
            "closed":    "false",
            "active":    "true",
            "order":     "volume",
            "ascending": "false",
            "limit":     limit,
            "offset":    offset,
        }
        data = fetch_with_retry(f"{config.GAMMA_API_BASE}/markets", params=params)
        if not data:
            break

        markets_raw = data if isinstance(data, list) else data.get("markets", [])
        if not markets_raw:
            break

        new_count = 0
        for raw in markets_raw:
            market_id = raw.get("conditionId") or raw.get("id")
            if not market_id or market_id in exclude_ids:
                continue

            # Пропускаем рынки с мусорными тегами
            raw_tags = raw.get("tags") or []
            try:
                tag_ids = [t.get("id") for t in raw_tags if isinstance(t, dict)]
            except Exception:
                tag_ids = []
            if any(tid in config.BLACKLIST_TAG_IDS for tid in tag_ids):
                continue

            market = parse_market_from_event(raw, raw.get("slug", ""), source_tag_id=0)
            if market:
                all_markets.append(market)
                exclude_ids.add(market_id)
                new_count += 1

        logger.info("  Некатег. страница %d: %d новых рынков.", page, new_count)

        if len(markets_raw) < limit:
            break
        offset += limit
        time.sleep(config.API_SLEEP_BETWEEN)

    logger.info("Некатегоризированных: итого %d рынков.", len(all_markets))
    return all_markets


# =============================================================================
# Сохранение в БД
# =============================================================================

def save_markets(markets: list[dict]) -> int:
    """
    Сохраняет рынки в БД (upsert). Возвращает количество сохранённых.
    """
    saved = 0
    for m in markets:
        try:
            db.upsert_market(m)
            saved += 1
        except Exception as e:
            logger.error("Ошибка upsert рынка %s: %s", m.get("id", "?")[:20], e)
    return saved


def record_price_snapshots(markets: list[dict]) -> None:
    """
    Записывает снимки цен в price_history для трёх групп рынков:
    1. Крупные рынки (volume > PANIC_VOLUME_THRESHOLD) — для паник-монитора.
    2. Дешёвые снайперские рынки (price <= SNIPER_MAX_PRICE) — для Стервятника.
    3. Дешёвые конвейерные рынки (price <= CONVEYOR_MAX_PRICE) — для Стервятника.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    for m in markets:
        price = m.get("last_trade_price") or 0
        if price <= 0:
            continue
        is_large    = m.get("volume", 0) >= config.PANIC_VOLUME_THRESHOLD
        max_p       = config.CONVEYOR_MAX_PRICE if m.get("mode") == "conveyor" else config.SNIPER_MAX_PRICE
        is_cheap    = price <= max_p
        if is_large or is_cheap:
            try:
                db.record_price(m["id"], price, now_iso)
            except Exception:
                pass


# =============================================================================
# Главная функция модуля
# =============================================================================

def run_scan() -> list[dict]:
    """
    Полный цикл сканирования v2:
    1. Загружает события по каждому целевому тегу через /events
    2. Загружает порцию некатегоризированных рынков (небольшую)
    3. Сохраняет всё в SQLite постранично
    4. Возвращает все найденные рынки для дальнейшей фильтрации

    Логика на входе API уже убирает спорт и культуру — фильтровать остатки
    будет много проще и быстрее чем в v1.
    """
    db.init_db()

    all_markets = []
    seen_ids    = set()
    tag_counts  = {}

    # 1. Режим СНАЙПЕР — politics, geopolitics, finance, crypto
    if config.SNIPER_ENABLED:
        for tag_id in config.SNIPER_TAG_IDS:
            tag_name = TAG_NAMES.get(tag_id, str(tag_id))
            markets  = fetch_events_by_tag(tag_id)
            new = [m for m in markets if m["id"] not in seen_ids]
            for m in new:
                seen_ids.add(m["id"])
            tag_counts[f"sniper/{tag_name}"] = len(new)
            save_markets(new)
            all_markets.extend(new)

    # 2. Режим КОНВЕЙЕР — sports / esports
    if config.CONVEYOR_ENABLED:
        for tag_id in config.CONVEYOR_TAG_IDS:
            tag_name = TAG_NAMES.get(tag_id, str(tag_id))
            markets  = fetch_events_by_tag(tag_id)
            new = [m for m in markets if m["id"] not in seen_ids]
            for m in new:
                seen_ids.add(m["id"])
            tag_counts[f"conveyor/{tag_name}"] = len(new)
            save_markets(new)
            all_markets.extend(new)

    # 3. Некатегоризированные (небольшая выборка — только для снайпера)
    if config.SNIPER_ENABLED:
        uncategorized = fetch_uncategorized_markets(exclude_ids=seen_ids, limit_pages=3)
        save_markets(uncategorized)
        all_markets.extend(uncategorized)
        tag_counts["sniper/uncategorized"] = len(uncategorized)

    # 4. Записываем снимки цен
    record_price_snapshots(all_markets)

    sniper_total   = sum(v for k, v in tag_counts.items() if k.startswith("sniper"))
    conveyor_total = sum(v for k, v in tag_counts.items() if k.startswith("conveyor"))
    logger.info(
        "Скан завершён: %d рынков (🦢 sniper: %d | ⚡ conveyor: %d)",
        len(all_markets), sniper_total, conveyor_total,
    )
    return all_markets


# =============================================================================
# Тест: python scanner.py
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [scanner] %(message)s",
        datefmt="%H:%M:%S",
    )

    print("=" * 60)
    print("Black Swan Hunter v2 — Тест Scanner")
    print("=" * 60)

    markets = run_scan()

    print(f"\n✅ Итого рынков: {len(markets)}")
    print("\n📋 Первые 5:")
    for m in markets[:5]:
        try:
            prices = json.loads(m["outcome_prices"])
            prices_str = " | ".join(f"{k}: ${v:.3f}" for k, v in list(prices.items())[:2])
        except Exception:
            prices_str = "нет цен"
        tag_id = m.get("source_tag_id", 0)
        tag_name = TAG_NAMES.get(tag_id, "uncategorized")
        print(f"  [{tag_name}] {m['question'][:65]}")
        print(f"    {prices_str} | Vol: ${m['volume']:,.0f} | 1h: {m['one_hour_price_change']:+.1f}pp")
