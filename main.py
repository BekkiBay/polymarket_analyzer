"""
main.py — Точка входа Black Swan Hunter v2.

Оркестрирует все модули в едином цикле.
Запуск: python main.py

Архитектура:
  Поток 1 (main): Цикл сканирования и алертов
  Поток 2 (daemon): Telegram long polling (команды)
  Gray zone дайджест: раз в день в DAILY_DIGEST_HOUR
  Weekly AI review: каждое воскресенье в WEEKLY_REVIEW_HOUR
"""

import logging
import threading
import time
from datetime import datetime, timezone

import config
import db
from scanner import run_scan
from filters import run_filters
from alerter import (
    send_telegram_message,
    process_green_alerts,
    process_gray_digest,
)
from panic_monitor import run_panic_check, check_price_movers
from journal import run_bot_polling

logger = logging.getLogger(__name__)


# =============================================================================
# Логирование
# =============================================================================

def setup_logging() -> None:
    """Настраивает логирование в консоль и файл black_swan.log."""
    fmt = "%(asctime)s %(levelname)-8s [%(module)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,   # сбрасываем все ранее добавленные хендлеры (убирает дубли)
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("black_swan.log", encoding="utf-8"),
        ],
    )
    # Меньше шума от http-библиотек
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)


# =============================================================================
# Стартовое сообщение
# =============================================================================

def startup_message() -> None:
    """Отправляет сообщение в Telegram при запуске бота."""
    gemini_ok  = "✅" if config.GEMINI_API_KEY != "YOUR_GEMINI_API_KEY" else "❌ не задан"
    claude_ok  = "✅" if config.ANTHROPIC_API_KEY != "YOUR_ANTHROPIC_API_KEY" else "❌ не задан"
    tg_ok      = config.TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN"

    lines = [
        "🦢 <b>Black Swan Hunter v2 — ЗАПУСК</b>",
        "",
        f"⚙️ <b>Конфигурация:</b>",
        f"  Цена: ${config.MIN_PRICE:.2f}–${config.MAX_PRICE:.2f}",
        f"  Мин. объём: ${config.MIN_VOLUME:,}",
        f"  Горизонт: {config.MIN_DAYS_TO_EXPIRY}–{config.MAX_DAYS_TO_EXPIRY} дней",
        f"  Паника: ≥{config.PANIC_THRESHOLD_PCT}п.п./час",
        f"  Скан: каждые {config.SCAN_INTERVAL_MINUTES} мин",
        f"  Дайджест: {config.DAILY_DIGEST_HOUR}:00 UTC",
        f"  Бюджет: ${config.TOTAL_BUDGET:.0f} (макс ${config.MAX_BET_SINGLE:.2f}/ставку)",
        "",
        f"🎯 <b>Целевые теги:</b> {config.TARGET_TAG_IDS}",
        f"  (politics, geopolitics, finance, crypto)",
        "",
        f"🤖 <b>AI:</b>",
        f"  Gemini (Level 4 фильтр): {gemini_ok}",
        f"  Claude (deep_analyze): {claude_ok}",
        f"  Мин. скор для алерта: {config.AI_MIN_SCORE_TO_ALERT}/10",
        f"  Кэш классификации: {config.AI_CACHE_HOURS}ч",
        "",
        f"✅ Готов к работе. Telegram: {'✅' if tg_ok else '❌'}",
        f"📖 /help — список команд",
    ]
    if tg_ok:
        send_telegram_message("\n".join(lines))
    logger.info("Стартовое сообщение отправлено.")


# =============================================================================
# Цикл сканирования
# =============================================================================

def run_scan_cycle() -> list[dict]:
    """
    Один полный цикл: скан → фильтры → паника → алерты.

    Возвращает отфильтрованные рынки (для логики дайджеста).
    """
    logger.info("=" * 60)
    logger.info("Начинаю цикл сканирования...")

    # 1. Сканируем Polymarket по целевым тегам
    markets = run_scan()
    logger.info("Скан завершён: %d рынков.", len(markets))

    if not markets:
        logger.warning("Скан вернул 0 рынков. Проверь соединение с API.")
        return []

    # 2. Паник-мониторинг (до фильтрации — нужны все крупные рынки)
    run_panic_check(markets)

    # 2b. 🦅 Стервятник — ловим дешёвые рынки с резким ростом цены
    # Запускается ДО полной фильтрации (чтобы не пропустить из-за AI-кэша)
    # и после паника (чтобы не дублировать работу с крупными рынками)
    check_price_movers(markets)

    # 3. Фильтрация (4 уровня)
    filtered = run_filters(markets)
    green = [m for m in filtered if m["zone"] == "green"]
    gray  = [m for m in filtered if m["zone"] == "gray"]
    logger.info("После фильтров: %d зелёных + %d серых.", len(green), len(gray))

    # 4. Мгновенные алерты для зелёной зоны
    if green:
        sent = process_green_alerts(green)
        logger.info("Green алертов отправлено: %d.", sent)

    return filtered


def _should_send_digest(last_digest: datetime | None) -> bool:
    """Проверяет наступление часа дайджеста (DAILY_DIGEST_HOUR, не чаще раз в день)."""
    now = datetime.now(timezone.utc)
    if now.hour != config.DAILY_DIGEST_HOUR:
        return False
    if last_digest and last_digest.date() == now.date():
        return False
    return True


def _should_send_weekly_review(last_review: datetime | None) -> bool:
    """Проверяет наступление времени еженедельного AI-ревью (воскресенье, WEEKLY_REVIEW_HOUR)."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:                    # 6 = воскресенье
        return False
    if now.hour != config.WEEKLY_REVIEW_HOUR:
        return False
    if last_review and last_review.date() == now.date():
        return False
    return True


# =============================================================================
# Главная функция
# =============================================================================

def main() -> None:
    """
    Точка входа. Инициализирует систему и запускает главный цикл.

    Поток 1 (main):   scan → filter → alert → sleep → повтор
    Поток 2 (daemon): Telegram long polling (боковой процесс)
    """
    setup_logging()
    logger.info("Black Swan Hunter v2 стартует...")

    db.init_db()
    startup_message()

    # Telegram long polling в фоновом потоке
    bot_thread = threading.Thread(target=run_bot_polling, daemon=True, name="TelegramBot")
    bot_thread.start()
    logger.info("Telegram-бот запущен в фоне.")

    last_digest = None
    last_review = None
    last_gray_markets = []

    try:
        while True:
            cycle_start = datetime.now(timezone.utc)

            # Основной цикл скана
            try:
                filtered = run_scan_cycle()
                last_gray_markets = [m for m in filtered if m["zone"] == "gray"]
            except Exception as e:
                logger.error("Ошибка в scan_cycle: %s", e, exc_info=True)
                filtered = []

            # Gray zone дайджест (раз в день)
            if _should_send_digest(last_digest):
                gray = [m for m in filtered if m["zone"] == "gray"]
                if not gray:
                    gray = last_gray_markets  # Используем предыдущие если нет новых
                if gray:
                    logger.info("Отправляю дневной дайджест (%d рынков)...", len(gray))
                    if process_gray_digest(gray):
                        last_digest = datetime.now(timezone.utc)

            # Еженедельный AI-ревью (Claude)
            if _should_send_weekly_review(last_review):
                logger.info("Отправляю еженедельный AI-ревью...")
                try:
                    from ai_analyst import weekly_portfolio_review
                    review_text = weekly_portfolio_review()
                    send_telegram_message(review_text)
                    last_review = datetime.now(timezone.utc)
                except Exception as e:
                    logger.error("Ошибка weekly review: %s", e)

            # Ждём до следующего скана
            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            wait_sec = max(0, config.SCAN_INTERVAL_MINUTES * 60 - elapsed)
            logger.info(
                "Следующий скан через %.1f мин. (цикл занял %.0f сек.)",
                wait_sec / 60, elapsed
            )
            time.sleep(wait_sec)

    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C...")
        send_telegram_message("🛑 <b>Black Swan Hunter остановлен.</b>")


if __name__ == "__main__":
    main()
