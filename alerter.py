"""
alerter.py — Модуль 4 v2: Отправка уведомлений в Telegram.

v2 изменения:
  - Ссылка через slug: polymarket.com/event/{slug}
  - Формат включает спред и AI-блок из LLM-классификации
  - Gray Zone дайджест имеет новый формат с AI-скорами
  - alerted_markets: антидупликат теперь по market_id (без outcome)
"""

import logging
import time
from datetime import datetime, timezone

import requests

import config
import db

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
PRICE_CHANGE_THRESHOLD = 0.5  # 50% изменение цены → повторный алерт

CATEGORY_EMOJI = {
    # Sniper
    "geopolitics":    "🌍",
    "crypto_risk":    "🔐",
    "macro":          "📉",
    "health":         "🦠",
    "politics_us":    "🇺🇸",
    "politics_world": "🗺️",
    "tech_risk":      "⚠️",
    "climate":        "🌊",
    "politics":       "🏛️",
    "finance":        "💹",
    "crypto":         "₿",
    "OTHER":          "❓",
    # Conveyor
    "esports":        "🎮",
    "football":       "⚽",
    "basketball":     "🏀",
    "tennis":         "🎾",
    "mma":            "🥊",
    "other_sport":    "🏆",
}


# =============================================================================
# HTTP-слой: отправка в Telegram
# =============================================================================

def send_telegram_message(text: str, parse_mode: str = "HTML") -> bool:
    """
    Отправляет сообщение в Telegram-чат. 3 попытки при ошибках.
    Возвращает True при успехе.
    """
    url = f"{TELEGRAM_API_BASE}/sendMessage"
    payload = {
        "chat_id":                  config.TELEGRAM_CHAT_ID,
        "text":                     text,
        "parse_mode":               parse_mode,
        "disable_web_page_preview": True,
    }
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            resp.raise_for_status()
            if resp.json().get("ok"):
                return True
            logger.warning("Telegram API ошибка: %s", resp.json().get("description"))
            return False
        except requests.exceptions.RequestException as e:
            logger.warning("Telegram [%d/3]: %s", attempt, e)
            if attempt < 3:
                time.sleep(2)
    logger.error("Не удалось отправить сообщение в Telegram.")
    return False


# =============================================================================
# Форматирование алертов
# =============================================================================

def _make_link(market: dict) -> str:
    """
    Формирует ссылку на рынок. Приоритет: event_slug > slug > generic.
    """
    event_slug = market.get("event_slug")
    slug       = market.get("slug")
    if event_slug:
        return f"https://polymarket.com/event/{event_slug}"
    if slug:
        return f"https://polymarket.com/event/{slug}"
    return "https://polymarket.com/markets"


def format_alert(market: dict, outcome: str, price: float,
                 alert_type: str = "new") -> str:
    """
    Форматирует HTML-алерт для GREEN ZONE.

    alert_type: "new" | "price_update" | "panic_scan"
    Включает AI-блок если есть данные классификации.
    """
    question  = market.get("question", "N/A")
    market_id = market.get("id", "")
    category  = market.get("category") or "OTHER"
    days_left = market.get("days_left")
    volume    = market.get("volume", 0)
    end_date  = (market.get("end_date") or "")[:10]
    spread_pct = market.get("spread_pct")
    spread_warning = market.get("spread_warning", False)
    link = _make_link(market)

    prob_pct = round(price * 100, 1)
    cat_emoji = CATEGORY_EMOJI.get(category, "🏷️")
    days_str = f"{days_left:.0f}" if days_left is not None else "?"

    spread_str = ""
    if spread_pct is not None:
        warn = " ⚠️" if spread_warning else ""
        spread_str = f" | Спред: {spread_pct:.0f}%{warn}"

    if alert_type == "price_update":
        header = "🔄 <b>ОБНОВЛЕНИЕ ЦЕНЫ</b>"
    elif alert_type == "panic_scan":
        header = "⚡ <b>PANIC SCAN — BLACK SWAN</b>"
    else:
        header = "🦢 <b>BLACK SWAN ALERT</b>"

    lines = [
        header,
        "",
        f"📌 {question}",
        f"💰 Цена: <b>${price:.3f}</b> ({prob_pct}%)",
        f"🎯 Исход: <b>{outcome}</b>",
        f"📊 Объём: ${volume:,.0f}{spread_str}",
        f"🏷️ Тема: {cat_emoji} {category}",
        f"⏰ Экспирация: {end_date} ({days_str} дн.)",
    ]

    # AI-блок
    ai_score  = market.get("ai_score")
    ai_prob   = market.get("ai_estimated_prob")
    ai_reason = market.get("ai_reasoning")
    ai_cat    = market.get("ai_catalyst")
    score_unavailable = market.get("score_unavailable", False)

    lines.append("")
    if score_unavailable:
        lines.append("🤖 <b>AI ОЦЕНКА: ⚠️ Score unavailable</b>")
        lines.append("<i>Скоринг временно недоступен, классификация прошла</i>")
    elif ai_score is not None:
        score_emoji = "🔥" if ai_score >= 8 else "⚡" if ai_score >= 6 else "🟡"
        lines.append(f"🤖 <b>AI ОЦЕНКА: {score_emoji} {ai_score}/10</b>")
        if ai_prob is not None:
            lines.append(f"📈 ИИ думает: <b>{ai_prob:.1f}%</b> (рынок: {prob_pct}%)")
        if ai_reason:
            lines.append(f"💬 <i>{ai_reason}</i>")
        if ai_cat:
            lines.append(f"🎯 Катализатор: {ai_cat}")

    lines += [
        "",
        f"🔗 <a href=\"{link}\">Открыть на Polymarket</a>",
        "",
        "─────────────────────",
        f"<code>/bet {market_id[:22]} 0.20 причина</code>",
        f"<code>/analyze {market_id[:22]}</code>",
        f"<code>/skip {market_id[:22]} причина</code>",
    ]
    return "\n".join(lines)


def format_digest(markets: list[dict]) -> str:
    """
    Форматирует дневной дайджест для GRAY ZONE.

    Отправляется раз в день в DAILY_DIGEST_HOUR.
    v2 формат включает AI-скор для каждого рынка.
    """
    if not markets:
        return ""

    now_str = datetime.now(timezone.utc).strftime("%d.%m.%Y")
    lines = [
        f"📋 <b>ДНЕВНОЙ ДАЙДЖЕСТ — {now_str}</b>",
        f"Найдено {len(markets)} рынков в серой зоне:",
        "",
    ]
    for i, m in enumerate(markets, 1):
        cheap = m.get("cheap_outcomes", [])
        if not cheap:
            continue
        outcome, price = cheap[0]
        prob_pct  = round(price * 100, 1)
        score_str = f"AI: {m['ai_score']}/10" if m.get("ai_score") else "AI: —"
        mid = m.get("id", "")[:22]
        lines.append(
            f"{i}. {m.get('question','')[:55]}\n"
            f"   ${price:.3f} ({prob_pct}%) — {score_str}"
        )
        lines.append(f"   <code>/analyze {mid}</code>")
        lines.append("")

    lines.append("<i>Ответь /analyze {market_id} для подробного разбора.</i>")
    return "\n".join(lines)


def format_panic_alert(market: dict, old_pct: float, new_pct: float) -> str:
    """Форматирует экстренный алерт о резком движении на крупном рынке."""
    link = _make_link(market)
    return "\n".join([
        "🚨 <b>PANIC DETECTED</b>",
        "",
        f"📌 {market.get('question','')[:65]}",
        f"📈 Прыжок: <b>{old_pct:.1f}% → {new_pct:.1f}%</b> "
        f"(+{new_pct - old_pct:.1f} п.п. за час)",
        f"📊 Объём: ${market.get('volume', 0):,.0f}",
        f"🔗 <a href=\"{link}\">Polymarket</a>",
        "",
        "⚡ <i>Запускаю внеочередной скан смежных рынков...</i>",
    ])


def format_vulture_alert(market: dict, old_price: float,
                         new_price: float, sc: dict | None) -> str:
    """
    Форматирует 🦅 алерт Стервятника — рынок тихо вырос на 50%+.

    sc — результат score() этапа 2 (может быть None если AI недоступен).
    """
    question   = market.get("question", "N/A")
    market_id  = market.get("id", "")
    category   = market.get("category") or _get_category_from_cache(market_id)
    volume_24h = market.get("volume_24h") or 0
    link       = _make_link(market)

    if old_price > 0:
        change_pct = (new_price - old_price) / old_price * 100
        price_line = (f"💰 Было: <b>${old_price:.3f}</b> → Стало: <b>${new_price:.3f}</b> "
                      f"(+{change_pct:.0f}%)")
    else:
        price_line = f"💰 Текущая цена: <b>${new_price:.3f}</b>"

    lines = [
        "🦅 <b>СТЕРВЯТНИК — ЦЕНА РАСТЁТ</b>",
        "",
        f"📌 {question}",
        price_line,
        f"📊 Объём за 24ч: ${volume_24h:,.0f}",
    ]
    if category:
        lines.append(f"🏷️ {category}")

    # AI-блок
    lines.append("")
    if sc and sc.get("score") is not None:
        score_val = sc["score"]
        score_emoji = "🔥" if score_val >= 8 else "⚡" if score_val >= 6 else "🟡"
        lines.append(f"🤖 AI: {score_emoji} {score_val}/10")
        if sc.get("estimated_prob_pct") is not None:
            lines.append(f"📈 ИИ думает: <b>{sc['estimated_prob_pct']:.1f}%</b>")
        if sc.get("reasoning"):
            lines.append(f"💬 <i>{sc['reasoning'][:200]}</i>")
        if sc.get("key_catalyst"):
            lines.append(f"🎯 {sc['key_catalyst']}")
    else:
        lines.append("🤖 <i>AI Score: недоступен</i>")

    mid = market_id[:22]
    lines += [
        "",
        f"🔗 <a href=\"{link}\">Открыть на Polymarket</a>",
        "",
        "─────────────────────",
        f"<code>/analyze {mid}</code>",
        f"<code>/bet {mid} 0.20 стервятник</code>",
    ]
    return "\n".join(lines)


def _get_category_from_cache(market_id: str) -> str | None:
    """Достаёт категорию из кэша AI-классификации для алерта."""
    import db as _db
    cls = _db.get_cached_classification(market_id, 48, stage="classify")
    return cls.get("category") if cls else None


def format_conveyor_alert(market: dict, outcome: str, price: float,
                          alert_type: str = "new") -> str:
    """
    Форматирует HTML-алерт для ⚡ CONVEYOR (спорт/киберспорт).

    Акцент: матч, андердог, часы до события, AI-оценка.
    """
    question   = market.get("question", "N/A")
    market_id  = market.get("id", "")
    category   = market.get("category") or "other_sport"
    hours_left = market.get("hours_left")
    volume     = market.get("volume", 0)
    link       = _make_link(market)

    prob_pct   = round(price * 100, 1)
    cat_emoji  = CATEGORY_EMOJI.get(category, "🏆")
    time_str   = f"{hours_left:.0f}ч" if hours_left is not None else "?"

    if alert_type == "price_update":
        header = "🔄 <b>CONVEYOR — ОБНОВЛЕНИЕ</b>"
    else:
        header = "⚡ <b>CONVEYOR BET</b>"

    ai_score  = market.get("ai_score")
    ai_prob   = market.get("ai_estimated_prob")
    ai_reason = market.get("ai_reasoning")
    ai_cat    = market.get("ai_catalyst")

    lines = [
        header,
        "",
        f"🎮 {question}",
        f"💰 Андердог: <b>${price:.3f}</b> ({prob_pct}%)",
        f"📊 Объём: ${volume:,.0f} {cat_emoji} {category}",
        f"⏰ Матч через: <b>{time_str}</b>",
    ]

    lines.append("")
    if ai_score is not None:
        score_emoji = "🔥" if ai_score >= 8 else "⚡" if ai_score >= 6 else "🟡"
        lines.append(f"🤖 AI: {score_emoji} <b>{ai_score}/10</b>")
        if ai_prob is not None:
            lines.append(f"📈 Оценка: <b>{ai_prob:.1f}%</b> (рынок: {prob_pct}%)")
        if ai_reason:
            lines.append(f"💬 <i>{ai_reason}</i>")
        if ai_cat:
            lines.append(f"🎯 Ключевой фактор: {ai_cat}")
    else:
        lines.append("🤖 <i>⚠️ AI Score unavailable</i>")

    mid = market_id[:22]
    key_factor_hint = (ai_cat or "андердог")[:30]
    lines += [
        "",
        f"🔗 <a href=\"{link}\">Открыть на Polymarket</a>",
        "",
        "─────────────────────",
        f"<code>/bet {mid} 0.30 {key_factor_hint}</code>",
        f"<code>/skip {mid}</code>",
    ]
    return "\n".join(lines)


def format_cluster_panic_alert(category: str, count: int,
                                markets: list[dict]) -> str:
    """
    Форматирует алерт о кластерной панике:
    >N рынков в одной категории с дневным изменением > threshold.
    """
    lines = [
        f"🚨 <b>КЛАСТЕРНАЯ ПАНИКА — {category.upper()}</b>",
        f"{count} рынков с резкими движениями за 24ч:",
        "",
    ]
    for m in markets[:5]:
        change = m.get("one_day_price_change", 0)
        lines.append(
            f"• {m.get('question','')[:50]} "
            f"({change:+.1f} п.п./день)"
        )
    lines += ["", "⚡ <i>Внеочередной скан смежных рынков запущен.</i>"]
    return "\n".join(lines)


# =============================================================================
# Логика отправки с антидупликатом
# =============================================================================

def should_send_alert(market_id: str, price: float) -> tuple[bool, str]:
    """
    Определяет нужно ли отправлять алерт.

    Проверяет таблицу alerted_markets:
    - Не алертился → new
    - Алертился, цена изменилась ≥50% → price_update
    - Алертился, цена не изменилась → нет
    """
    prev = db.was_alerted(market_id)
    if prev is None:
        return True, "new"
    prev_price = prev["alert_price"]
    if prev_price > 0 and abs(price - prev_price) / prev_price >= PRICE_CHANGE_THRESHOLD:
        return True, "price_update"
    return False, ""


def process_green_alerts(markets: list[dict]) -> int:
    """
    Обрабатывает GREEN ZONE: мгновенный алерт для каждого рынка.

    Пропускает уже алерченные (антидупликат). Возвращает число отправленных.
    """
    sent = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for market in markets:
        if market.get("zone") != "green":
            continue

        cheap = market.get("cheap_outcomes", [])
        if not cheap:
            continue

        # Берём самый дешёвый outcome
        outcome, price = min(cheap, key=lambda x: x[1])

        need_send, alert_type = should_send_alert(market["id"], price)
        if not need_send:
            logger.debug("Дубликат: %s", market["id"][:20])
            continue

        # Маршрутизируем формат по режиму
        if market.get("mode") == "conveyor":
            text = format_conveyor_alert(market, outcome, price, alert_type)
        else:
            text = format_alert(market, outcome, price, alert_type)

        if send_telegram_message(text):
            db.mark_as_alerted(market["id"], price, now_iso, alert_type)
            sent += 1
            mode_icon = "⚡" if market.get("mode") == "conveyor" else "🦢"
            logger.info("%s Алерт [%s]: %s @ $%.3f",
                        mode_icon, alert_type, market.get("question", "")[:40], price)
            time.sleep(0.5)

    return sent


def process_gray_digest(markets: list[dict]) -> bool:
    """
    Обрабатывает GRAY ZONE: собирает дайджест и отправляет батчем.
    Включает только новые рынки (ещё не алерченные). Возвращает True при успехе.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    new_gray = []
    for market in markets:
        if market.get("zone") != "gray":
            continue
        cheap = market.get("cheap_outcomes", [])
        if not cheap:
            continue
        _, price = min(cheap, key=lambda x: x[1])
        need_send, _ = should_send_alert(market["id"], price)
        if need_send:
            new_gray.append(market)

    if not new_gray:
        logger.info("Нет новых рынков для gray дайджеста.")
        return True

    text = format_digest(new_gray)
    if not text:
        return True

    success = send_telegram_message(text)
    if success:
        for market in new_gray:
            cheap = market.get("cheap_outcomes", [])
            if cheap:
                _, price = min(cheap, key=lambda x: x[1])
                db.mark_as_alerted(market["id"], price, now_iso, "digest")
        logger.info("Gray дайджест отправлен: %d рынков.", len(new_gray))
    return success


# =============================================================================
# Тест: python alerter.py
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [alerter] %(message)s",
        datefmt="%H:%M:%S",
    )

    if config.TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("⚠️  TELEGRAM_BOT_TOKEN не настроен в config.py")
        exit(1)

    gemini_ok  = "✅" if config.GEMINI_API_KEY != "YOUR_GEMINI_API_KEY" else "❌"
    claude_ok  = "✅" if config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY" else "❌"

    test_text = "\n".join([
        "🦢 <b>Black Swan Hunter v2 — Тест</b>",
        "",
        "✅ Telegram подключён!",
        "",
        f"⚙️ Настройки:",
        f"  • Цена: ${config.MIN_PRICE:.2f}–${config.MAX_PRICE:.2f}",
        f"  • Мин. объём: ${config.MIN_VOLUME:,}",
        f"  • Горизонт: {config.MIN_DAYS_TO_EXPIRY}–{config.MAX_DAYS_TO_EXPIRY} дней",
        f"  • Теги: {config.TARGET_TAG_IDS}",
        f"",
        f"🤖 AI: Gemini {gemini_ok} | Claude {claude_ok}",
        f"  • Мин. скор для алерта: {config.AI_MIN_SCORE_TO_ALERT}/10",
    ])

    if send_telegram_message(test_text):
        print("✅ Тестовое сообщение отправлено!")
    else:
        print("❌ Ошибка отправки. Проверь токен и chat_id.")
