"""
db.py — Инициализация SQLite v2 и все CRUD-операции.

Таблицы v2:
  markets           — рынки (расширенная схема: slug, bid/ask, price changes, source_tag_id)
  ai_classifications — LLM-классификации рынков (кэш)
  alerted_markets   — антидупликат алертов
  price_history     — история цен
  bets              — журнал ставок (добавлены ai_score, ai_estimated_prob)
  skips             — осознанные пропуски
  deep_analyses     — история /analyze запросов

При смене схемы старый DB файл нужно удалить (в разработке).
"""

import sqlite3
import logging
from config import DB_PATH

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """
    Возвращает соединение с базой данных с поддержкой Row Factory.
    Строки возвращаются как словари (row["field"]).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # Лучше для конкурентного доступа
    return conn


def init_db() -> None:
    """
    Создаёт все таблицы если не существуют. Идемпотентно.
    v2: расширенная схема markets, новая таблица ai_classifications.
    """
    conn = get_connection()
    c = conn.cursor()

    # ------------------------------------------------------------------
    # markets — основной каталог рынков (v2 схема)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            id                    TEXT PRIMARY KEY,  -- condition_id (0x...)
            condition_id          TEXT,              -- то же что id, для явности
            question              TEXT NOT NULL,
            description           TEXT,
            slug                  TEXT,              -- для URL polymarket.com/event/{slug}
            event_slug            TEXT,              -- slug родительского события
            outcomes              TEXT,              -- JSON: ["Yes","No"]
            outcome_prices        TEXT,              -- JSON: {"Yes": 0.03, "No": 0.97}
            best_bid              REAL,
            best_ask              REAL,
            last_trade_price      REAL,
            volume                REAL DEFAULT 0,
            volume_24h            REAL DEFAULT 0,
            liquidity             REAL DEFAULT 0,
            end_date              TEXT,
            tags                  TEXT,              -- JSON: [{"id":100265,"label":"Geopolitics"}]
            one_hour_price_change REAL DEFAULT 0,    -- изменение цены за час (п.п.)
            one_day_price_change  REAL DEFAULT 0,    -- изменение цены за день (п.п.)
            enable_order_book     INTEGER DEFAULT 1, -- 1 = можно торговать
            active                INTEGER DEFAULT 1,
            source_tag_id         INTEGER,           -- из какого тега пришёл рынок
            mode                  TEXT DEFAULT 'sniper', -- 'sniper' | 'conveyor'
            first_seen_at         TEXT,
            last_checked_at       TEXT
        )
    """)

    # ------------------------------------------------------------------
    # ai_classifications — кэш LLM-классификаций (Gemini)
    # Ключевое отличие от v1: хранит is_candidate, confidence, reject_reason
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_classifications (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id           TEXT NOT NULL,
            stage               TEXT NOT NULL DEFAULT 'classify', -- 'classify' | 'score'
            category            TEXT,               -- geopolitics | crypto_risk | macro | ...
            is_candidate        INTEGER DEFAULT 0,  -- 1=true, 0=false (только в classify)
            confidence          INTEGER,            -- 1-10 (только в classify)
            score               INTEGER,            -- 1-10 (только в score)
            estimated_prob_pct  REAL,               -- (только в score)
            reasoning           TEXT,               -- (только в score)
            key_catalyst        TEXT,               -- (только в score)
            reject_reason       TEXT,               -- причина отклонения (только в classify)
            model               TEXT,               -- gemini/claude
            classified_at       TEXT NOT NULL
        )
    """)
    # Миграции для существующих БД
    for migration in [
        "ALTER TABLE ai_classifications ADD COLUMN stage TEXT NOT NULL DEFAULT 'classify'",
        "ALTER TABLE markets ADD COLUMN mode TEXT DEFAULT 'sniper'",
        "ALTER TABLE bets ADD COLUMN mode TEXT DEFAULT 'sniper'",
    ]:
        try:
            c.execute(migration)
            logger.info("Миграция выполнена: %s", migration[:60])
        except Exception:
            pass  # Поле уже существует
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_ai_cls_market_stage
        ON ai_classifications (market_id, stage, classified_at)
    """)

    # ------------------------------------------------------------------
    # price_history — история цен для графиков и анализа трендов
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT NOT NULL,
            price       REAL NOT NULL,
            recorded_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_price_hist_market_time
        ON price_history (market_id, recorded_at)
    """)

    # ------------------------------------------------------------------
    # alerted_markets — антидупликат алертов
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerted_markets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT NOT NULL,
            alert_price REAL NOT NULL,
            alerted_at  TEXT NOT NULL,
            alert_type  TEXT DEFAULT 'new'
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_alerted_unique
        ON alerted_markets (market_id)
    """)

    # ------------------------------------------------------------------
    # bets — журнал ставок (v2: добавлены ai_score, ai_estimated_prob)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id         TEXT NOT NULL,
            question          TEXT,
            entry_price       REAL,
            amount_usd        REAL NOT NULL,
            reason            TEXT,
            ai_score          INTEGER,          -- AI-скор на момент ставки
            ai_estimated_prob REAL,             -- AI-оценка вероятности на момент ставки
            timestamp         TEXT NOT NULL,
            status            TEXT DEFAULT 'active',  -- active / won / lost
            resolved_at       TEXT,
            payout            REAL DEFAULT 0
        )
    """)

    # ------------------------------------------------------------------
    # skips — осознанные пропуски (упрощённая v2 схема)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS skips (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id   TEXT NOT NULL,
            reason      TEXT,
            timestamp   TEXT NOT NULL
        )
    """)

    # ------------------------------------------------------------------
    # deep_analyses — история запросов /analyze (Claude)
    # ------------------------------------------------------------------
    c.execute("""
        CREATE TABLE IF NOT EXISTS deep_analyses (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id     TEXT NOT NULL,
            analysis_text TEXT NOT NULL,
            model         TEXT,
            analyzed_at   TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    logger.info("БД инициализирована: %s", DB_PATH)


# =============================================================================
# CRUD — markets
# =============================================================================

def upsert_market(market: dict) -> None:
    """
    Вставляет новый рынок или обновляет цены/объём существующего.
    first_seen_at не перезаписывается при обновлении.
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO markets (
            id, condition_id, question, description, slug, event_slug,
            outcomes, outcome_prices, best_bid, best_ask, last_trade_price,
            volume, volume_24h, liquidity, end_date, tags,
            one_hour_price_change, one_day_price_change,
            enable_order_book, active, source_tag_id, mode,
            first_seen_at, last_checked_at
        ) VALUES (
            :id, :condition_id, :question, :description, :slug, :event_slug,
            :outcomes, :outcome_prices, :best_bid, :best_ask, :last_trade_price,
            :volume, :volume_24h, :liquidity, :end_date, :tags,
            :one_hour_price_change, :one_day_price_change,
            :enable_order_book, :active, :source_tag_id, :mode,
            :first_seen_at, :last_checked_at
        )
        ON CONFLICT(id) DO UPDATE SET
            outcome_prices        = excluded.outcome_prices,
            best_bid              = excluded.best_bid,
            best_ask              = excluded.best_ask,
            last_trade_price      = excluded.last_trade_price,
            volume                = excluded.volume,
            volume_24h            = excluded.volume_24h,
            liquidity             = excluded.liquidity,
            one_hour_price_change = excluded.one_hour_price_change,
            one_day_price_change  = excluded.one_day_price_change,
            active                = excluded.active,
            mode                  = excluded.mode,
            last_checked_at       = excluded.last_checked_at
    """, market)
    conn.commit()
    conn.close()


def get_market_by_id(market_id: str) -> dict | None:
    """
    Возвращает рынок по id (полному или частичному LIKE-совпадению).
    """
    conn = get_connection()
    row = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM markets WHERE id LIKE ?", (f"{market_id}%",)
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_active_markets() -> list[dict]:
    """
    Возвращает все активные рынки из БД для обработки фильтрами.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM markets WHERE active = 1"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def record_price(market_id: str, price: float, recorded_at: str) -> None:
    """
    Записывает точку цены в историю. Вызывается при каждом скане.
    """
    conn = get_connection()
    conn.execute(
        "INSERT INTO price_history (market_id, price, recorded_at) VALUES (?,?,?)",
        (market_id, price, recorded_at)
    )
    conn.commit()
    conn.close()


def get_price_24h_ago(market_id: str) -> float | None:
    """
    Возвращает цену рынка ~24 часа назад из price_history.

    Берёт ближайшую запись в окне 20-28 часов назад.
    Возвращает None если истории нет (рынок недавно появился).
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT price FROM price_history
        WHERE market_id = ?
          AND recorded_at <= datetime('now', '-20 hours')
          AND recorded_at >= datetime('now', '-28 hours')
        ORDER BY recorded_at DESC
        LIMIT 1
    """, (market_id,)).fetchone()
    conn.close()
    return row["price"] if row else None


def get_alerted_vulture(market_id: str) -> bool:
    """
    Проверяет, был ли уже отправлен стервятник-алерт для этого рынка сегодня.
    Антидупликат: один алерт типа 'vulture' в сутки на рынок.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT 1 FROM alerted_markets
        WHERE market_id = ?
          AND alert_type = 'vulture'
          AND alerted_at >= datetime('now', '-24 hours')
    """, (market_id,)).fetchone()
    conn.close()
    return row is not None


# =============================================================================
# CRUD — ai_classifications
# =============================================================================

def get_cached_classification(market_id: str, max_age_hours: int,
                              stage: str = "classify") -> dict | None:
    """
    Возвращает кэшированный результат конкретного этапа классификации.

    stage="classify" — кэш на 24ч: категория + is_candidate
    stage="score"    — кэш на 12ч: score + новостной reasoning
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM ai_classifications
        WHERE market_id = ? AND stage = ? AND classified_at >= ?
        ORDER BY classified_at DESC LIMIT 1
    """, (market_id, stage, cutoff)).fetchone()
    conn.close()
    return dict(row) if row else None


def save_classification(cls: dict) -> None:
    """
    Сохраняет результат одного этапа LLM-классификации.

    Обязательные поля: market_id, stage, model, classified_at
    classify-поля: category, is_candidate, confidence, reject_reason
    score-поля:    score, estimated_prob_pct, reasoning, key_catalyst
    """
    conn = get_connection()
    conn.execute("""
        INSERT INTO ai_classifications
            (market_id, stage, category, is_candidate, confidence, score,
             estimated_prob_pct, reasoning, key_catalyst, reject_reason, model, classified_at)
        VALUES
            (:market_id, :stage, :category, :is_candidate, :confidence, :score,
             :estimated_prob_pct, :reasoning, :key_catalyst, :reject_reason, :model, :classified_at)
    """, cls)
    conn.commit()
    conn.close()


# =============================================================================
# CRUD — alerted_markets
# =============================================================================

def was_alerted(market_id: str) -> dict | None:
    """Проверяет, был ли уже алерт для рынка. Возвращает запись или None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM alerted_markets WHERE market_id = ?", (market_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_as_alerted(market_id: str, price: float, alerted_at: str,
                    alert_type: str = "new") -> None:
    """Сохраняет факт отправки алерта. При повторном вызове — обновляет."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO alerted_markets (market_id, alert_price, alerted_at, alert_type)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            alert_price = excluded.alert_price,
            alerted_at  = excluded.alerted_at,
            alert_type  = excluded.alert_type
    """, (market_id, price, alerted_at, alert_type))
    conn.commit()
    conn.close()


# =============================================================================
# CRUD — bets
# =============================================================================

def insert_bet(bet: dict) -> int:
    """Записывает ставку в журнал. Возвращает id новой записи."""
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO bets
            (market_id, question, entry_price, amount_usd, reason,
             ai_score, ai_estimated_prob, mode, timestamp, status)
        VALUES
            (:market_id, :question, :entry_price, :amount_usd, :reason,
             :ai_score, :ai_estimated_prob, :mode, :timestamp, :status)
    """, bet)
    bet_id = c.lastrowid
    conn.commit()
    conn.close()
    return bet_id


def get_active_bets() -> list[dict]:
    """Возвращает все активные ставки (для /portfolio)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM bets WHERE status = 'active' ORDER BY timestamp DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_bets() -> list[dict]:
    """Возвращает все ставки (для /stats и Weekly Review)."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM bets ORDER BY timestamp DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_bet_status(bet_id: int, status: str, resolved_at: str, payout: float) -> None:
    """Обновляет статус ставки после резолюции рынка."""
    conn = get_connection()
    conn.execute(
        "UPDATE bets SET status=?, resolved_at=?, payout=? WHERE id=?",
        (status, resolved_at, payout, bet_id)
    )
    conn.commit()
    conn.close()


# =============================================================================
# CRUD — skips
# =============================================================================

def insert_skip(market_id: str, reason: str, timestamp: str) -> None:
    """Записывает осознанный пропуск рынка."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO skips (market_id, reason, timestamp) VALUES (?,?,?)",
        (market_id, reason, timestamp)
    )
    conn.commit()
    conn.close()


def get_skips_count_since(since_iso: str) -> int:
    """Возвращает количество пропусков начиная с указанной даты."""
    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM skips WHERE timestamp >= ?", (since_iso,)
    ).fetchone()[0]
    conn.close()
    return count


# =============================================================================
# CRUD — deep_analyses
# =============================================================================

def save_deep_analysis(market_id: str, text: str, model: str, analyzed_at: str) -> None:
    """Сохраняет результат /analyze (Claude) в историю."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO deep_analyses (market_id, analysis_text, model, analyzed_at) VALUES (?,?,?,?)",
        (market_id, text, model, analyzed_at)
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    import logging as _log
    _log.basicConfig(level=_log.INFO, format="%(levelname)s: %(message)s")
    init_db()
    conn = get_connection()
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    conn.close()
    print(f"✅ БД создана. Таблицы: {tables}")
