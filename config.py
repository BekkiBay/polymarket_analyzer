"""
config.py — Центральный файл конфигурации Black Swan Hunter v2.

Два режима работы:
  🦢 SNIPER   — геополитика/макро/крипто. Редкие ставки, крупнее, ждём недели.
  ⚡ CONVEYOR — спорт/киберспорт. Частые мелкие ставки, результат через часы.

ВСЕ параметры системы здесь. Меняй только этот файл.
"""

# =============================================================================
# Telegram
# =============================================================================
TELEGRAM_BOT_TOKEN = "--------"
TELEGRAM_CHAT_ID   = "--------"

# =============================================================================
# AI Providers
# =============================================================================
GEMINI_API_KEY    = "--------"
ANTHROPIC_API_KEY = "--------"



# === Прямой HTTP/SOCKS5 прокси (альтернатива OpenRouter) ===
# Формат: "http://user:pass@host:port" или "socks5://user:pass@host:port"
# Оставь пустым если не нужен.
CLAUDE_PROXY = "http://rYuC3T:pqaQoj@74.205.241.45:8000"
# === AI модели ===
# Проверь актуальные имена: https://docs.anthropic.com/en/docs/about-claude/models
CLAUDE_MODEL  = "claude-sonnet-4-6"
# Проверь актуальные имена: https://ai.google.dev/gemini-api/docs/models
GEMINI_MODEL  = "gemini-3.1-flash-lite-preview"

# === AI настройки ===
AI_CACHE_HOURS            = 24     # Не переклассифицировать рынок чаще N часов
AI_MIN_SCORE_TO_ALERT     = 4      # Зелёная зона: только score >= этого (0 = всё)
AI_CLASSIFICATION_ENABLED = True   # Включить LLM-классификатор (Level 4 фильтрации)
AI_DEEP_ANALYSIS_ENABLED  = True   # Включить команду /analyze
AI_MAX_PER_CYCLE          = 200    # Макс. кол-во новых классификаций за один цикл
                                   # (защита от случайного слива токенов)
                                   # Первый запуск: 810 рынков → 5 циклов по 200 (~$0.13 итого)

# =============================================================================
# Polymarket API
# =============================================================================
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE  = "https://clob.polymarket.com"

# === Целевые теги Polymarket (захардкожены реальные tag_id) ===
# Скачиваем ТОЛЬКО эти категории — убирает тысячи спортивных рынков на входе API
POLYMARKET_TAGS = {
    # ЦЕЛЕВЫЕ — наши "зелёные" категории
    "politics":    2,
    "geopolitics": 100265,
    "finance":     120,
    "crypto":      21,
    # МУСОРНЫЕ — явно отсекаем на уровне API (не скачиваем вообще)
    "sports":      100639,
    "culture":     596,
    "tech":        1401,
}

# =============================================================================
# 🦢 Режим СНАЙПЕР — геополитика / макро / крипто
# =============================================================================
SNIPER_ENABLED          = True
SNIPER_TAG_IDS          = [2, 100265, 120, 21]  # politics, geopolitics, finance, crypto
SNIPER_MIN_PRICE        = 0.01
SNIPER_MAX_PRICE        = 0.10
SNIPER_MIN_VOLUME       = 500
SNIPER_MAX_DAYS_EXPIRY  = 90
SNIPER_MIN_DAYS_EXPIRY  = 1
SNIPER_MIN_AI_SCORE     = 4      # Мягкий порог — человек сам решает
SNIPER_MAX_BET          = 1.00   # Ставки крупнее, но реже

# =============================================================================
# ⚡ Режим КОНВЕЙЕР — спорт / киберспорт
# =============================================================================
CONVEYOR_ENABLED             = True
CONVEYOR_TAG_IDS             = [100639]  # sports (включая esports)
CONVEYOR_MIN_PRICE           = 0.01
CONVEYOR_MAX_PRICE           = 0.15      # Шире диапазон — больше матчей
CONVEYOR_MIN_VOLUME          = 100       # Ниже порог — спортивные рынки мельче
CONVEYOR_MAX_HOURS_TO_EVENT  = 48        # Только матчи в ближайшие 48 часов
CONVEYOR_MIN_AI_SCORE        = 6         # Жёстче — ставим почти вслепую
CONVEYOR_MAX_BET             = 0.50      # Мелкие ставки, берём объёмом

# Обратная совместимость (используются в общих функциях без mode)
TARGET_TAG_IDS    = SNIPER_TAG_IDS
BLACKLIST_TAG_IDS = [596, 1401]      # culture, tech — их не качаем вообще

# Параметры по умолчанию для общих функций (sniper)
MIN_PRICE          = SNIPER_MIN_PRICE
MAX_PRICE          = SNIPER_MAX_PRICE
MIN_VOLUME         = SNIPER_MIN_VOLUME
MAX_DAYS_TO_EXPIRY = SNIPER_MAX_DAYS_EXPIRY
MIN_DAYS_TO_EXPIRY = SNIPER_MIN_DAYS_EXPIRY
MAX_SPREAD_PCT     = 80   # Спред выше этого = предупреждение ⚠️ (не отсечка)

# =============================================================================
# Keyword фильтры (Level 3 — только для некатегоризированных рынков)
# =============================================================================
BLACKLIST_KEYWORDS = [
    # Спорт (на случай если пролезло мимо тегов)
    "nba", "nfl", "nhl", "mlb", "premier league", "champions league",
    "world cup", "super bowl", "playoff", "championship", "touchdown",
    "goal scored", "match result", "win series", "mvp", "rushing yards",
    # Поп-культура
    "youtube", "tiktok", "twitch", "streamer", "subscriber count",
    "oscar", "grammy", "emmy", "golden globe", "box office",
    "movie", "album release", "billboard", "netflix", "disney",
    "kardashian", "celebrity", "influencer", "reality tv",
    "bachelor", "love island", "will wear", "hair color",
    # Мемы и фан-рынки
    "meme", "will eat", "will say the word", "tweet about",
    "costume", "hot dog", "eating contest",
    "follower count", "subscribers", "views on",
]

WHITELIST_KEYWORDS = {
    "geopolitics": [
        "war", "invasion", "sanctions", "nato", "military",
        "nuclear", "missile", "coup", "regime", "territory",
        "ceasefire", "escalation", "troops", "iran", "china",
        "taiwan", "korea", "russia", "ukraine", "israel",
        "hamas", "hezbollah", "syria", "north korea",
    ],
    "crypto_risk": [
        "sec", "depeg", "hack", "exploit", "stablecoin",
        "usdt", "usdc", "tether", "binance", "coinbase",
        "withdraw halt", "insolvency", "bankrupt",
        "regulation", "ban crypto", "etf rejected",
    ],
    "macro": [
        "fed", "interest rate", "emergency meeting", "recession",
        "default", "debt ceiling", "bank failure", "fdic",
        "inflation above", "currency crisis", "imf bailout",
        "yield curve",
    ],
    "health": [
        "pandemic", "epidemic", "outbreak", "who emergency",
        "bird flu", "h5n1", "lockdown", "quarantine",
        "vaccine mandate", "mpox", "disease x",
    ],
}

# =============================================================================
# Panic Monitor
# =============================================================================
PANIC_THRESHOLD_PCT    = 15      # Порог для "паники" (п.п. за час из oneHourPriceChange)
PANIC_VOLUME_THRESHOLD = 10000   # Мониторить панику только на рынках с объёмом >
CLUSTER_PANIC_MIN      = 3       # Кластерная паника: >N рынков с dayChange>10 в категории
CLUSTER_PANIC_DAY_CHANGE = 10    # Порог дневного изменения для кластерной паники

# =============================================================================
# 🦅 Режим Стервятник — ловим рынки с резким ростом цены
# =============================================================================
VULTURE_MIN_OLD_PRICE   = 0.02   # Следить за рынками которые были ≤ этой цены ($0.02)
VULTURE_MIN_NEW_PRICE   = 0.03   # Текущая цена должна быть ≥ этого ($0.03)
VULTURE_MIN_CHANGE_PCT  = 50     # Минимальный рост в % (50% = с $0.02 до $0.03+)
VULTURE_MIN_CHANGE_PP   = 2      # ИЛИ: минимальный рост в п.п. (oneDayPriceChange из API)
VULTURE_MIN_VOLUME_24H  = 300    # Мин. объём за 24ч — ниже этого считаем шумом ($)

# =============================================================================
# Расписание
# =============================================================================
SCAN_INTERVAL_MINUTES = 60    # Как часто запускать полный скан
WEEKLY_REVIEW_DAY     = "sunday"  # День еженедельной AI-ревизии
WEEKLY_REVIEW_HOUR    = 20        # Час отправки ревизии (UTC)
DAILY_DIGEST_HOUR     = 21        # Час отправки gray-zone дайджеста (UTC)

# =============================================================================
# HTTP клиент
# =============================================================================
REQUEST_TIMEOUT     = 20    # Таймаут запроса (сек)
REQUEST_RETRY_COUNT = 3     # Количество повторных попыток
REQUEST_RETRY_DELAY = 2.0   # Пауза между попытками (сек)
API_SLEEP_BETWEEN   = 1.0   # Пауза между запросами к Polymarket (сек)
MAX_SCAN_PAGES      = 20    # Максимум страниц на один тег (20 * 100 = 2000 событий)

# =============================================================================
# Бюджет (разделён между режимами)
# =============================================================================
TOTAL_BUDGET         = 80.0   # Начальный бюджет ($)
MAX_BET_SINGLE       = 1.0    # Общий максимум (перекрывается mode-специфичным)
WEEKLY_BUDGET_LIMIT  = 20.0   # Максимум ставок в неделю ($)

SNIPER_BUDGET_PCT    = 50     # % бюджета на снайпер  → $40
CONVEYOR_BUDGET_PCT  = 50     # % бюджета на конвейер → $40

# =============================================================================
# Пути
# =============================================================================
DB_PATH = "black_swan.db"
