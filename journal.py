"""
journal.py — Модуль 5 v2: Журнал ставок и Telegram-бот.

v2 изменения:
  - Новая команда /budget — остаток бюджета
  - bets хранят ai_score и ai_estimated_prob
  - /stats включает "Точность AI" — из N алертов скором ≥7 сыграло M (X%)
  - Еженедельный отчёт включает AI-точность
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta

import requests

import config
import db
from alerter import send_telegram_message

logger = logging.getLogger(__name__)
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"


# =============================================================================
# Long Polling
# =============================================================================

def get_updates(offset: int = 0, timeout: int = 10) -> list[dict]:
    """Получает новые обновления Telegram Bot API (long polling)."""
    try:
        resp = requests.get(
            f"{TELEGRAM_API_BASE}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except requests.exceptions.RequestException as e:
        logger.warning("getUpdates ошибка: %s", e)
    return []


# =============================================================================
# Форматирование ответов
# =============================================================================

def _total_spent() -> float:
    """Считает общую сумму всех ставок (активных + завершённых)."""
    return sum(b["amount_usd"] for b in db.get_all_bets())


def _mode_budget(mode: str) -> float:
    """Выделенный бюджет для режима в $."""
    pct = config.SNIPER_BUDGET_PCT if mode == "sniper" else config.CONVEYOR_BUDGET_PCT
    return config.TOTAL_BUDGET * pct / 100


def _mode_stats(bets: list[dict], mode: str) -> dict:
    """Возвращает агрегаты P&L/winrate для конкретного mode."""
    mb = [b for b in bets if b.get("mode", "sniper") == mode]
    won  = [b for b in mb if b["status"] == "won"]
    lost = [b for b in mb if b["status"] == "lost"]
    closed = won + lost
    invested = sum(b["amount_usd"] for b in mb)
    payout   = sum(b["payout"] or 0 for b in won)
    loss_amt = sum(b["amount_usd"] for b in lost)
    pnl      = payout - loss_amt
    win_rate = (len(won) / len(closed) * 100) if closed else 0
    return {
        "total": len(mb), "won": len(won), "lost": len(lost),
        "active": len([b for b in mb if b["status"] == "active"]),
        "invested": invested, "pnl": pnl, "win_rate": win_rate,
    }


def format_portfolio() -> str:
    """
    Формирует ответ на /portfolio.
    Показывает все активные ставки, сумму и остаток бюджета.
    """
    bets    = db.get_active_bets()
    now     = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    total_active = sum(b["amount_usd"] for b in bets)
    week_bets    = [b for b in bets if datetime.fromisoformat(b["timestamp"]) >= week_ago]
    week_invested = sum(b["amount_usd"] for b in week_bets)
    spent        = _total_spent()
    remaining    = config.TOTAL_BUDGET - spent

    lines = ["💼 <b>ПОРТФЕЛЬ</b>", ""]
    if not bets:
        lines.append("📭 <i>Активных ставок нет.</i>")
    else:
        sniper_cnt   = len([b for b in bets if b.get("mode", "sniper") == "sniper"])
        conveyor_cnt = len([b for b in bets if b.get("mode") == "conveyor"])
        lines.append(
            f"Активных: <b>{len(bets)}</b> на <b>${total_active:.2f}</b> "
            f"(🦢 {sniper_cnt} | ⚡ {conveyor_cnt})"
        )
        lines.append(f"За 7 дней: {len(week_bets)} на ${week_invested:.2f}")
        lines.append("")
        lines.append("─────────────────────")
        for b in bets:
            date_str   = b["timestamp"][:10]
            prob_pct   = round((b["entry_price"] or 0) * 100, 1)
            score_str  = f" AI:{b['ai_score']}/10" if b.get("ai_score") else ""
            mode_icon  = "⚡" if b.get("mode") == "conveyor" else "🦢"
            lines.append(
                f"{mode_icon} <b>${b['amount_usd']:.2f}</b> @ ${b['entry_price']:.3f} "
                f"({prob_pct}%){score_str}"
            )
            lines.append(f"  [{date_str}] {b.get('question','')[:50]}")
            if b.get("reason"):
                lines.append(f"  💭 <i>{b['reason'][:60]}</i>")

    lines += [
        "",
        "─────────────────────",
        f"💰 Бюджет: ${config.TOTAL_BUDGET:.0f} | Потрачено: ${spent:.2f} | "
        f"<b>Остаток: ${remaining:.2f}</b>",
        f"⚠️ Лимит/неделя: ${config.WEEKLY_BUDGET_LIMIT:.0f} "
        f"(использовано: ${week_invested:.2f})",
    ]
    return "\n".join(lines)


def format_budget() -> str:
    """
    Формирует ответ на /budget.
    Показывает общий бюджет и разбивку по режимам sniper/conveyor.
    """
    all_bets  = db.get_all_bets()
    spent     = _total_spent()
    remaining = config.TOTAL_BUDGET - spent
    active    = sum(b["amount_usd"] for b in db.get_active_bets())

    now        = datetime.now(timezone.utc)
    week_ago   = now - timedelta(days=7)
    week_spent = sum(
        b["amount_usd"] for b in all_bets
        if datetime.fromisoformat(b["timestamp"]) >= week_ago
    )
    week_pct = (week_spent / config.WEEKLY_BUDGET_LIMIT * 100) if config.WEEKLY_BUDGET_LIMIT else 0

    bar_len   = 20
    used_bars = int(spent / config.TOTAL_BUDGET * bar_len) if config.TOTAL_BUDGET else 0
    bar       = "█" * used_bars + "░" * (bar_len - used_bars)

    # Бюджет по режимам
    sniper_budget   = _mode_budget("sniper")
    conveyor_budget = _mode_budget("conveyor")
    sniper_spent    = sum(b["amount_usd"] for b in all_bets if b.get("mode", "sniper") == "sniper")
    conveyor_spent  = sum(b["amount_usd"] for b in all_bets if b.get("mode") == "conveyor")

    return "\n".join([
        "💵 <b>БЮДЖЕТ</b>",
        "",
        f"[{bar}] {spent / config.TOTAL_BUDGET * 100:.0f}%",
        "",
        f"Начальный:  ${config.TOTAL_BUDGET:.2f}",
        f"Потрачено:  ${spent:.2f}",
        f"В игре:     ${active:.2f}",
        f"<b>Остаток:   ${remaining:.2f}</b>",
        "",
        "─────────────────────",
        f"🦢 Снайпер ({config.SNIPER_BUDGET_PCT}%): "
        f"${sniper_spent:.2f} / ${sniper_budget:.0f} "
        f"(остаток <b>${sniper_budget - sniper_spent:.2f}</b>)",
        f"⚡ Конвейер ({config.CONVEYOR_BUDGET_PCT}%): "
        f"${conveyor_spent:.2f} / ${conveyor_budget:.0f} "
        f"(остаток <b>${conveyor_budget - conveyor_spent:.2f}</b>)",
        "",
        f"Лимит/неделя: ${config.WEEKLY_BUDGET_LIMIT:.0f} "
        f"(${week_spent:.2f} = {week_pct:.0f}%)",
        f"Макс/ставка: 🦢 ${config.SNIPER_MAX_BET:.2f} | ⚡ ${config.CONVEYOR_MAX_BET:.2f}",
    ])


def _mode_stats_lines(bets: list[dict], mode: str, icon: str, label: str) -> list[str]:
    """Блок статистики одного режима."""
    s = _mode_stats(bets, mode)
    if s["total"] == 0:
        return [f"{icon} <b>{label}:</b> ставок нет"]
    closed = s["won"] + s["lost"]
    pnl_str = f"${s['pnl']:+.2f}"
    wr_str  = f"{s['win_rate']:.1f}%" if closed else "—"
    return [
        f"{icon} <b>{label}:</b>",
        f"  Ставок: {s['total']} | Активных: {s['active']}",
        f"  ✅ {s['won']} / ❌ {s['lost']} | Win rate: {wr_str} | P&L: {pnl_str}",
    ]


def format_stats() -> str:
    """
    Формирует ответ на /stats.
    Показывает РАЗДЕЛЬНУЮ статистику по sniper и conveyor + общую.
    """
    bets  = db.get_all_bets()
    skips = db.get_skips_count_since("2000-01-01")

    if not bets:
        return "📊 <b>СТАТИСТИКА</b>\n\n<i>Ставок пока нет.</i>"

    won    = [b for b in bets if b["status"] == "won"]
    lost   = [b for b in bets if b["status"] == "lost"]
    active = [b for b in bets if b["status"] == "active"]
    closed = won + lost

    total_invested  = sum(b["amount_usd"] for b in bets)
    total_payout    = sum(b["payout"] or 0 for b in won)
    total_loss_amt  = sum(b["amount_usd"] for b in lost)
    pnl             = total_payout - total_loss_amt
    win_rate        = (len(won) / len(closed) * 100) if closed else 0
    active_amt      = sum(b["amount_usd"] for b in active)
    closed_invested = total_invested - active_amt
    roi             = (pnl / closed_invested * 100) if closed_invested > 0 else 0

    # AI-точность: из ставок со скором ≥7
    hs     = [b for b in closed if (b.get("ai_score") or 0) >= 7]
    hs_won = [b for b in hs if b["status"] == "won"]
    ai_str = ""
    if hs:
        ai_str = f"🤖 Точность AI (скор ≥7): {len(hs_won)}/{len(hs)} = {len(hs_won)/len(hs)*100:.0f}%"

    lines = [
        "📊 <b>СТАТИСТИКА</b>",
        "",
    ]
    # Раздельная статистика
    lines += _mode_stats_lines(bets, "sniper",   "🦢", "Снайпер (геополитика)")
    lines.append("")
    lines += _mode_stats_lines(bets, "conveyor", "⚡", "Конвейер (спорт)")
    lines += [
        "",
        "─────────────────────",
        f"<b>ИТОГО:</b> {len(bets)} ставок | 🙈 пропусков: {skips}",
        f"💰 Вложено: ${total_invested:.2f}",
        f"📈 P&L: <b>${pnl:+.2f}</b>",
        f"🎯 Win Rate: <b>{win_rate:.1f}%</b> | ROI: <b>{roi:+.1f}%</b>",
    ]
    if ai_str:
        lines.append(ai_str)
    return "\n".join(lines)


def format_review() -> str:
    """
    Формирует ответ на /review.
    Показывает завершённые ставки для ревизии решений.
    """
    bets   = db.get_all_bets()
    closed = [b for b in bets if b["status"] in ("won", "lost")]
    if not closed:
        return "🔍 <b>РЕВИЗИЯ</b>\n\n<i>Завершённых ставок пока нет.</i>"

    lines = [f"🔍 <b>РЕВИЗИЯ</b> ({len(closed)} завершённых)", ""]
    for b in closed:
        icon   = "✅" if b["status"] == "won" else "❌"
        payout = b.get("payout") or 0
        profit = payout - b["amount_usd"]
        pstr   = f"+${profit:.2f}" if profit > 0 else f"-${b['amount_usd']:.2f}"
        score_str = f" [AI:{b['ai_score']}/10]" if b.get("ai_score") else ""
        lines += [
            f"{icon} <b>${b['amount_usd']:.2f}</b> → {b['status'].upper()} ({pstr}){score_str}",
            f"   {b.get('question','')[:55]}",
        ]
        if b.get("reason"):
            lines.append(f"   💭 <i>{b['reason'][:60]}</i>")
        lines.append("")
    return "\n".join(lines)


def format_weekly_report() -> str:
    """
    Формирует еженедельный отчёт (каждое воскресенье).
    v2: включает блок AI-точности.
    """
    now      = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    period   = f"{week_ago.strftime('%d.%m')} — {now.strftime('%d.%m.%Y')}"

    all_bets   = db.get_all_bets()
    week_bets  = [b for b in all_bets if datetime.fromisoformat(b["timestamp"]) >= week_ago]
    week_won   = [b for b in week_bets if b["status"] == "won"]
    week_lost  = [b for b in week_bets if b["status"] == "lost"]
    active_all = [b for b in all_bets if b["status"] == "active"]
    won_all    = [b for b in all_bets if b["status"] == "won"]
    lost_all   = [b for b in all_bets if b["status"] == "lost"]

    week_invested = sum(b["amount_usd"] for b in week_bets)
    week_profit   = sum(b["payout"] or 0 for b in week_won)
    week_loss     = sum(b["amount_usd"] for b in week_lost)
    total_pnl     = sum(b["payout"] or 0 for b in won_all) - sum(b["amount_usd"] for b in lost_all)
    win_rate      = (len(won_all) / (len(won_all) + len(lost_all)) * 100) if (won_all or lost_all) else 0
    remaining     = config.TOTAL_BUDGET - sum(b["amount_usd"] for b in all_bets)

    week_skips = db.get_skips_count_since(week_ago.isoformat())

    # AI-точность
    hs = [b for b in won_all + lost_all if (b.get("ai_score") or 0) >= 7]
    hs_won = [b for b in hs if b["status"] == "won"]
    ai_line = ""
    if hs:
        ai_line = f"\n🤖 Точность AI (скор ≥7): {len(hs_won)}/{len(hs)} ({len(hs_won)/len(hs)*100:.0f}%)"

    lines = [
        f"📊 <b>ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ — {period}</b>",
        "",
        f"Ставок за неделю: <b>{len(week_bets)}</b> на ${week_invested:.2f}",
        f"Пропущено: {week_skips}",
        "",
    ]
    if week_won or week_lost:
        lines += [
            f"Завершённых за неделю: {len(week_won) + len(week_lost)}",
            f"  ✅ Выиграно: {len(week_won)} → +${week_profit:.2f}",
            f"  ❌ Проиграно: {len(week_lost)} → -${week_loss:.2f}",
            "",
        ]

    # Раздельная статистика за всё время
    lines += _mode_stats_lines(all_bets, "sniper",   "🦢", "Снайпер (всё время)")
    lines.append("")
    lines += _mode_stats_lines(all_bets, "conveyor", "⚡", "Конвейер (всё время)")
    lines += [
        "",
        "─────────────────────",
        f"📈 P&L за всё время: <b>${total_pnl:+.2f}</b>",
        f"🎯 Win Rate: <b>{win_rate:.1f}%</b>",
        f"💵 Остаток бюджета: <b>${remaining:.2f}</b>",
    ]
    if ai_line:
        lines.append(ai_line)
    return "\n".join(lines)


# =============================================================================
# Обработка команд
# =============================================================================

def handle_bet(args: list[str]) -> str:
    """
    /bet {market_id} {сумма} {причина}
    Записывает ставку с AI-данными из кэша классификации.
    """
    if len(args) < 2:
        return (
            "⚠️ Формат: <code>/bet {market_id} {сумма} {причина}</code>\n"
            "Пример: <code>/bet 0x123 0.20 суд по Coinbase</code>"
        )
    market_id = args[0]
    try:
        amount = float(args[1])
    except ValueError:
        return f"⚠️ Неверная сумма: <code>{args[1]}</code>"

    reason   = " ".join(args[2:]) if len(args) > 2 else ""
    market   = db.get_market_by_id(market_id)
    question = market["question"] if market else f"Рынок {market_id}"
    mode     = (market.get("mode") or "sniper") if market else "sniper"

    # Лимит зависит от режима
    max_bet = config.CONVEYOR_MAX_BET if mode == "conveyor" else config.SNIPER_MAX_BET
    if amount > max_bet:
        mode_label = "⚡ конвейер" if mode == "conveyor" else "🦢 снайпер"
        return (
            f"⚠️ Сумма ${amount:.2f} превышает лимит для {mode_label}: ${max_bet:.2f}\n"
            f"Придерживайся риск-менеджмента!"
        )

    # Цена входа
    entry_price = 0.0
    if market:
        try:
            prices = json.loads(market.get("outcome_prices", "{}"))
            if prices:
                entry_price = min(float(p) for p in prices.values())
        except Exception:
            pass

    # AI-данные из кэша
    ai_score, ai_prob = None, None
    cls = db.get_cached_classification(market_id, 72) if market else None
    if cls:
        ai_score = cls.get("score")
        ai_prob  = cls.get("estimated_prob_pct")

    now_iso = datetime.now(timezone.utc).isoformat()
    bet_id  = db.insert_bet({
        "market_id":         market_id,
        "question":          question,
        "entry_price":       entry_price,
        "amount_usd":        amount,
        "reason":            reason,
        "ai_score":          ai_score,
        "ai_estimated_prob": ai_prob,
        "mode":              mode,
        "timestamp":         now_iso,
        "status":            "active",
    })

    mode_icon  = "⚡" if mode == "conveyor" else "🦢"
    score_line = f"\n🤖 AI-скор: {ai_score}/10 (оценка: {ai_prob:.1f}%)" if ai_score else ""
    closing    = ("Ловим момент! Матч уже скоро." if mode == "conveyor"
                  else "Один чёрный лебедь окупает сотни потерь.")

    return (
        f"✅ <b>Ставка #{bet_id} записана!</b> {mode_icon}\n\n"
        f"📌 {question[:60]}\n"
        f"💰 ${amount:.2f} @ ${entry_price:.3f}"
        f"{score_line}\n"
        f"💭 <i>{reason or 'причина не указана'}</i>\n\n"
        f"<i>{closing}</i>"
    )


def handle_skip(args: list[str]) -> str:
    """
    /skip {market_id} {причина}
    Записывает осознанный пропуск рынка.
    """
    if not args:
        return "⚠️ Формат: <code>/skip {market_id} {причина}</code>"
    market_id = args[0]
    reason    = " ".join(args[1:]) if len(args) > 1 else "не указана"
    now_iso   = datetime.now(timezone.utc).isoformat()
    db.insert_skip(market_id, reason, now_iso)
    return (
        f"🙈 <b>Пропуск записан.</b>\n"
        f"Рынок: <code>{market_id[:30]}</code>\n"
        f"Причина: <i>{reason}</i>"
    )


def handle_analyze(args: list[str]) -> str:
    """
    /analyze {market_id}
    Запускает deep_analyze через ai_analyst и возвращает первую часть.
    Если анализ длинный — отправляет части напрямую, возвращает последнюю.
    """
    if not args:
        return "⚠️ Формат: <code>/analyze {market_id}</code>"

    market_id = args[0]
    send_telegram_message(
        f"🔍 <i>Анализирую <code>{market_id[:25]}</code>...\n"
        f"Ищу новости и запрашиваю AI. ~10-30 сек.</i>"
    )

    from ai_analyst import deep_analyze
    analysis = deep_analyze(market_id)

    MAX_LEN = 4000
    if len(analysis) <= MAX_LEN:
        return analysis

    # Разбиваем по абзацам
    parts, current = [], ""
    for line in analysis.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            parts.append(current)
            current = line
        else:
            current += ("\n" if current else "") + line
    if current:
        parts.append(current)

    for part in parts[:-1]:
        send_telegram_message(part)
        time.sleep(0.5)
    return parts[-1] if parts else analysis


def handle_ping_ai() -> str:
    """
    /ping_ai — диагностика API ключей Gemini и Claude.
    Делает минимальный реальный запрос к каждому и показывает результат.
    """
    from ai_analyst import call_gemini_text, call_openrouter, call_claude

    lines = ["🔧 <b>Диагностика AI API</b>", ""]

    # 1. Gemini
    try:
        result = call_gemini_text("Ответь одним словом: OK", max_tokens=5)
        if result:
            lines.append(f"✅ Gemini ({config.GEMINI_MODEL}): {result.strip()[:15]}")
        else:
            lines.append(f"❌ Gemini: нет ответа")
    except Exception as e:
        lines.append(f"❌ Gemini: {e}")

    lines.append("")

    # 2. OpenRouter (Claude через прокси — обходит РФ/VPS блокировку)
    or_key = getattr(config, "OPENROUTER_API_KEY", "")
    if or_key:
        try:
            result = call_openrouter("Reply with one word: OK")
            if result:
                model = getattr(config, "CLAUDE_OPENROUTER_MODEL", "claude via openrouter")
                lines.append(f"✅ Claude via OpenRouter ({model}): {result.strip()[:15]}")
                lines.append("   <i>→ /analyze и weekly_review работают через OpenRouter</i>")
            else:
                lines.append("❌ OpenRouter: нет ответа (проверь ключ и баланс на openrouter.ai)")
        except Exception as e:
            lines.append(f"❌ OpenRouter: {e}")
    else:
        lines.append("⚪ OpenRouter: ключ не задан")
        lines.append("   <i>→ Зарегистрируйся на openrouter.ai и вставь ключ в config.py</i>")

    lines.append("")

    # 3. Прямой Claude Anthropic
    try:
        import requests as _req
        r = _req.post(
            "https://api.anthropic.com/v1/messages",
            json={"model": config.CLAUDE_MODEL, "max_tokens": 5,
                  "messages": [{"role": "user", "content": "Reply OK"}]},
            headers={"x-api-key": config.ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            timeout=15,
        )
        if r.ok:
            lines.append(f"✅ Claude прямой ({config.CLAUDE_MODEL}): OK")
        elif r.status_code == 403:
            lines.append("⚠️ Claude прямой: HTTP 403 — Anthropic блокирует этот IP")
            lines.append("   <i>→ Используй OpenRouter как обходной путь</i>")
        else:
            lines.append(f"❌ Claude прямой: HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        lines.append(f"❌ Claude прямой: {e}")

    return "\n".join(lines)


def handle_command(text: str) -> str | None:
    """Разбирает входящую команду и вызывает нужный обработчик."""
    text = text.strip()
    if not text.startswith("/"):
        return None
    parts   = text.split()
    command = parts[0].lower().split("@")[0]
    args    = parts[1:]

    handlers = {
        "/bet":       lambda: handle_bet(args),
        "/skip":      lambda: handle_skip(args),
        "/analyze":   lambda: handle_analyze(args),
        "/portfolio": lambda: format_portfolio(),
        "/budget":    lambda: format_budget(),
        "/stats":     lambda: format_stats(),
        "/review":    lambda: format_review(),
        "/ping_ai":   lambda: handle_ping_ai(),
        "/start":     lambda: _help(),
        "/help":      lambda: _help(),
    }
    handler = handlers.get(command)
    return handler() if handler else None


def _help() -> str:
    return (
        "🦢⚡ <b>Black Swan Hunter v2</b>\n\n"
        "Два режима: 🦢 Снайпер (геополитика) | ⚡ Конвейер (спорт)\n\n"
        "📥 <b>Запись:</b>\n"
        "<code>/bet {id} {$} {причина}</code> — ставка\n"
        "<code>/skip {id} {причина}</code> — пропуск\n\n"
        "🤖 <b>AI:</b>\n"
        "<code>/analyze {id}</code> — глубокий анализ (Claude)\n\n"
        "📊 <b>Аналитика:</b>\n"
        "<code>/portfolio</code> — активные ставки (с разбивкой по режимам)\n"
        "<code>/budget</code> — бюджет: sniper vs conveyor\n"
        "<code>/stats</code> — P&L / win rate / AI-точность по режимам\n"
        "<code>/review</code> — завершённые ставки\n\n"
        "🔧 <b>Диагностика:</b>\n"
        "<code>/ping_ai</code> — проверить Gemini и Claude API\n"
    )


# =============================================================================
# Еженедельная AI-ревизия
# =============================================================================

def check_weekly_report(last_date: datetime | None) -> datetime | None:
    """Отправляет еженедельный отчёт по воскресеньям. Антидубликат по дате."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:
        return last_date
    if last_date and last_date.date() == now.date():
        return last_date
    if send_telegram_message(format_weekly_report()):
        logger.info("Еженедельный отчёт отправлен.")
        return now
    return last_date


# =============================================================================
# Главный цикл long polling
# =============================================================================

def run_bot_polling() -> None:
    """
    Бесконечный цикл long polling для обработки команд Telegram.
    Вызывается из main.py в daemon-потоке.
    """
    logger.info("Telegram-бот запущен (long polling)...")
    offset, last_report = 0, None

    while True:
        try:
            updates = get_updates(offset=offset, timeout=10)
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or {}
                text    = message.get("text", "")
                chat_id = message.get("chat", {}).get("id")

                if not text or not chat_id:
                    continue
                if str(chat_id) != str(config.TELEGRAM_CHAT_ID):
                    continue

                response = handle_command(text)
                if response:
                    send_telegram_message(response)

            last_report = check_weekly_report(last_report)

        except KeyboardInterrupt:
            logger.info("Бот остановлен.")
            break
        except Exception as e:
            logger.error("Ошибка в цикле бота: %s", e)
            time.sleep(5)


# =============================================================================
# Тест: python journal.py
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [journal] %(message)s",
        datefmt="%H:%M:%S",
    )
    db.init_db()

    print("=== /portfolio ===")
    print(format_portfolio())
    print()
    print("=== /budget ===")
    print(format_budget())
    print()
    print("=== /stats ===")
    print(format_stats())

    if config.TELEGRAM_BOT_TOKEN != "YOUR_BOT_TOKEN":
        print("\nЗапускаю long polling...")
        run_bot_polling()
    else:
        print("\n⚠️  Telegram не настроен в config.py")
