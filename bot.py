# Astral dry-run smoke.
import logging
import os
import re
import sqlite3
import threading
import time
import asyncio
from functools import partial
from datetime import datetime, timezone
from datetime import timedelta
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update, WebAppInfo
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

# В самом начале файла, где ALLOWED
ALLOWED_BUY_CRYPTO = ("BTC", "ETH", "USDT", "TON", "SOL")
ALLOWED_SELL_CRYPTO = ("BTC", "ETH", "USDT", "TON", "SOL")
CRYPTO_NETWORKS = {
    "USDT": ["TRC20", "ERC20", "BEP20"],
    "BTC": ["Bitcoin"],
    "ETH": ["ERC20"],
    "TON": ["TON"],
    "SOL": ["Solana"],
}

PAYMENT_METHODS = ["СБП", "Карта РФ", "Карта Мир"]

SELL_METHOD_CARD = "Перевод на карту"
SELL_METHOD_QR = "Наличные по QR-коду в банкомате"
SELL_PAYMENT_METHODS = [SELL_METHOD_CARD, SELL_METHOD_QR]

# Заглушки депозитных адресов Astral по сетям (замените на реальные или вынесите в БД/настройки)
ASTRAL_DEPOSIT_ADDRESSES = {
    "TRC20": ("TYourAstralTRC20WalletAddressHere123", None),
    "ERC20": ("0xYourAstralERC20WalletAddressHere123456", None),
    "BEP20": ("0xYourAstralBEP20WalletAddressHere123456", None),
    "Bitcoin": ("bc1qYourAstralBitcoinWalletAddressHere123", None),
    "TON": ("UQYourAstralTONWalletAddressHere123456", "104928"),  # (адрес, Memo/Tag)
    "Solana": ("YourAstralSolanaWalletAddressHere123456", None),
}

# Новые статусы для сценария Продажи (Часть 3 ТЗ)
STATUS_SELL_CHECKING = "Проверка поступления"
STATUS_SELL_PAYOUT_PROCESS = "Выплата обрабатывается"
STATUS_SELL_WAIT_ADMIN_QR = "Ожидает связи администратора"
STATUS_EXPIRED = "Истекла"

# Состояния диалога пользователя
(
    CHOOSING,
    FROM_CURRENCY,
    TO_CURRENCY,
    AMOUNT,
    WALLET,
    ORDER_STATUS_WAIT,
    BLOCKED_USER,
    USER_IN_SUPPORT,
    BUY_CURRENCY,
    BUY_NETWORK,
    BUY_PAYMENT,
    BUY_AMOUNT_MODE,
    BUY_AMOUNT_INPUT,
    BUY_RATE_CONFIRM,
    BUY_WALLET_INPUT,
    BUY_WALLET_CONFIRM,
    BUY_FINAL_CONFIRM,
    BUY_PAYMENT_WAIT,
    BUY_RECEIPT_UPLOAD,
    SELL_CURRENCY,
    SELL_NETWORK,
    SELL_PAYMENT,
    SELL_AMOUNT_MODE,
    SELL_AMOUNT_INPUT,
    SELL_RATE_CONFIRM,
    SELL_CARD_INPUT,
    SELL_CARD_CONFIRM,
    SELL_FINAL_CONFIRM,
    SELL_DEPOSIT_WAIT,
    SELL_CHECKING_WAIT,
    SELL_TXHASH_INPUT,
) = range(31)

# Состояния админского диалога
(
    A_MENU,
    A_CONF_ORDER,
    A_RATE_CUR,
    A_RATE_MULT,
    A_RES_CUR,
    A_RES_AMT,
    A_BLOCK_ID,
    A_UNBLOCK_ID,
    A_REPLY_TO_USER,
    A_SELL_QR_TIME,
) = range(50, 60)

DB_PATH = os.getenv("DB_PATH", "exchange_bot.db")
ROLE_USER = "user"
ROLE_ADMIN = "admin"

STATUS_PENDING = "Ожидает оплаты"
STATUS_REVIEW = "На проверке"
STATUS_DONE = "Выполнена"
STATUS_CANCELLED = "Отменена"
# Новые статусы для сценария Покупки
STATUS_RECEIPT_UPLOADED = "Чек получен, ожидает проверки"
STATUS_PAYMENT_CONFIRMED = "Оплата подтверждена"
STATUS_SENDING = "Отправка криптовалюты"
ADMIN_ALLOWED_STATUSES = (
    STATUS_PENDING, 
    STATUS_REVIEW, 
    STATUS_DONE, 
    STATUS_CANCELLED, 
    STATUS_RECEIPT_UPLOADED, 
    STATUS_PAYMENT_CONFIRMED, 
    STATUS_SENDING,
    STATUS_SELL_CHECKING,
    STATUS_SELL_PAYOUT_PROCESS,
    STATUS_SELL_WAIT_ADMIN_QR,
    STATUS_EXPIRED,
)
ACTIVE_ORDER_STATUSES = (
    STATUS_PENDING,
    STATUS_REVIEW,
    STATUS_RECEIPT_UPLOADED,
    STATUS_PAYMENT_CONFIRMED,
    STATUS_SENDING,
    STATUS_SELL_CHECKING,
    STATUS_SELL_PAYOUT_PROCESS,
    STATUS_SELL_WAIT_ADMIN_QR,
)
_STATUS_CB = {
    "P": STATUS_PENDING,
    "R": STATUS_REVIEW,
    "D": STATUS_DONE,
    "C": STATUS_CANCELLED,
    "RU": STATUS_RECEIPT_UPLOADED,
    "PC": STATUS_PAYMENT_CONFIRMED,
    "S": STATUS_SENDING,
}

ALLOWED = (
    "BTC", "ETH", "USDT", "RUB", "BNB", "SOL",
    "XRP", "DOGE", "ADA", "TRX", "LTC", "TON",
)
# 👇 Новые списки для inline-кнопок
POPULAR_CURRENCIES = ("BTC", "ETH", "USDT", "RUB", "TON")
OTHER_CURRENCIES = tuple(c for c in ALLOWED if c not in POPULAR_CURRENCIES)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

RATES_TTL = float(os.getenv("RATES_TTL", "60"))
_rates_cache: Optional[Tuple[float, Dict[str, float]]] = None
_rates_lock = threading.Lock()

COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "USDT": "tether",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
    "DOGE": "dogecoin",
    "ADA": "cardano",
    "TRX": "tron",
    "LTC": "litecoin",
    "TON": "the-open-network",
}

# --- Основные кнопки меню (сокращенные) ---
BUTTON_BUY = "Купить"
BUTTON_SELL = "Продать"
BUTTON_SWAP = "Swap"
BUTTON_AML = "AML проверка"
BUTTON_WALLET = "Ваш кошелёк"
BUTTON_CABINET = "Личный кабинет"
BUTTON_RATES = "📊 Курсы"
BUTTON_MY_ORDERS = "📂 Мои заявки"
BUTTON_NEWS = "Новости"
BUTTON_REVIEW = "Оставить отзыв"
BUTTON_SUPPORT = "ℹ️ Поддержка"
BUTTON_MORE = "⚙️ Ещё"

# --- Кнопки меню "Ещё" ---
BUTTON_HOW_TO_EXCHANGE = "Как сделать обмен?"
BUTTON_ABOUT = "О сервисе"
BUTTON_FAVORITES = "Избранное"
BUTTON_MY_TICKETS = "Мои обращения"
BUTTON_NOTIFICATION_SETTINGS = "Настройки уведомлений"
BUTTON_LANGUAGE = "Язык"
BUTTON_EXCHANGE_RULES = "Правила обмена"
BUTTON_SHARE = "Поделиться Astral"
BUTTON_BACK_TO_MAIN = "↩️ Назад в меню"

# --- Контекстные кнопки (inline, по условию) ---
BUTTON_CONTINUE_DRAFT = "▶️ Продолжить обмен"
BUTTON_OPEN_ORDER = "📄 Открыть заявку"
BUTTON_ACTIVE_ORDERS = "🗂 Активные заявки"
BUTTON_REPEAT = "🔁 Повторить обмен"

# --- Кнопки навигации ---
BUTTON_BACK = "↩️ Назад"
BUTTON_BACK_TO_MENU = "↩️ В меню"
BUTTON_CANCEL = "❌ Отмена"

# --- Устаревшие кнопки (оставлены для совместимости) ---
BUTTON_CLEAN = "🧹 Чистка"
BUTTON_HISTORY = "📜 История операций"
BUTTON_ORDER_STATUS = "📋 Статус заявки"
BUTTON_MINIAPP = "🧩 Mini App"
BUTTON_CLOSE_TICKET = "✅ Завершить обращение"

BUTTON_ADMIN = "/admin"
BUTTON_ADMIN_SUMMARY = "📋 Сводка заявок"
BUTTON_A_ORDERS = "📋 Все заявки"
BUTTON_A_CONFIRM = "✅ Статус заявки"
BUTTON_A_RATES = "⚙️ Множители курсов"
BUTTON_A_RESERVES = "🏦 Резервы"
BUTTON_A_BLOCK = "🚫 Заблокировать"
BUTTON_A_UNBLOCK = "🔓 Разблокировать"
BUTTON_A_REFRESH_RATES = "🔄 Обновить курсы"
BUTTON_A_TICKETS = "🗂 Активные обращения"
BUTTON_A_EXIT = "↩️ Выход из админки"

CALLBACK_DIALOG_EXIT = "dialog:exit"

_ADMIN_PANEL_REPLY_TEXTS = frozenset(
    (
        BUTTON_A_ORDERS,
        BUTTON_A_CONFIRM,
        BUTTON_A_RATES,
        BUTTON_A_RESERVES,
        BUTTON_A_BLOCK,
        BUTTON_A_UNBLOCK,
        BUTTON_A_REFRESH_RATES,
        BUTTON_A_TICKETS,
        BUTTON_A_EXIT,
    )
)

def get_order_remaining_minutes(order_id: int) -> Optional[int]:
    """Возвращает остаток минут до истечения заявки на продажу или None."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT order_type, status, expires_at FROM orders WHERE id = ?",
            (order_id,),
        )
        row = cur.fetchone()
    if not row or row[0] != "sell" or row[1] != STATUS_PENDING or not row[2]:
        return None
    try:
        exp_dt = datetime.fromisoformat(row[2])
        delta = exp_dt - datetime.now(timezone.utc)
        mins = int(delta.total_seconds() // 60)
        return max(0, mins)
    except ValueError:
        return None


def _admin_id_set() -> Set[int]:
    raw = (os.getenv("ADMIN_TELEGRAM_IDS") or "").strip()
    raw = raw.lstrip("\ufeff")
    if not raw:
        return set()
    out: Set[int] = set()
    for part in re.split(r"[,\s;]+", raw):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
            continue
        except ValueError:
            pass
        for m in re.finditer(r"\d{6,}", part):
            out.add(int(m.group()))
    return out


def parse_admin_ids() -> Set[int]:
    return _admin_id_set()


def _all_admin_ids() -> Set[int]:
    admins = _admin_id_set()
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT telegram_id FROM users WHERE role = ?", (ROLE_ADMIN,))
            admins |= {int(r[0]) for r in cur.fetchall()}
    except sqlite3.Error as exc:
        logging.warning("Не удалось получить админов из БД: %s", exc)
    return admins


def get_miniapp_url() -> str:
    raw = (os.getenv("MINI_APP_URL") or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif not re.match(r"^[Hh][Tt][Tt][Pp][Ss]?://", raw):
        raw = f"https://{raw}"
    low = raw.lower()
    if low.startswith("http://") and "localhost" not in low and "127.0.0.1" not in low:
        raw = "https://" + raw[7:]
    return raw


def input_step_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_BACK, BUTTON_CANCEL]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def status_keyboard_purchase(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(STATUS_PAYMENT_CONFIRMED, callback_data=f"pa:{order_id}:CONFIRM")],
            [InlineKeyboardButton(STATUS_DONE, callback_data=f"pa:{order_id}:DONE")],
            [InlineKeyboardButton(STATUS_CANCELLED, callback_data=f"pa:{order_id}:CANCEL")],
        ]
    )

def status_keyboard_sell(order_id: int, is_qr: bool = False) -> InlineKeyboardMarkup:
    if is_qr:
        rows = [
            [InlineKeyboardButton("✅ Крипта получена (Ждёт админа QR)", callback_data=f"sa:{order_id}:CRYPTO_OK")],
            [InlineKeyboardButton("📅 Согласовать время QR", callback_data=f"sa:{order_id}:QR_TIME")],
            [InlineKeyboardButton("📲 Запросить QR у клиента", callback_data=f"sa:{order_id}:QR_ASK")],
            [InlineKeyboardButton("💵 Наличные выданы (Выполнена)", callback_data=f"sa:{order_id}:DONE")],
            [InlineKeyboardButton("❌ Отменить заявку", callback_data=f"sa:{order_id}:CANCEL")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("✅ Крипта получена (В выплату)", callback_data=f"sa:{order_id}:CRYPTO_OK")],
            [InlineKeyboardButton("💸 Выплата выполнена", callback_data=f"sa:{order_id}:DONE")],
            [InlineKeyboardButton("❌ Отменить заявку", callback_data=f"sa:{order_id}:CANCEL")],
        ]
    return InlineKeyboardMarkup(rows)

def exchange_step_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура для шагов обмена: только Назад и Отмена."""
    return ReplyKeyboardMarkup(
        [[BUTTON_BACK, BUTTON_CANCEL]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def currency_step_keyboard() -> ReplyKeyboardMarkup:
    currency_buttons = [list(ALLOWED[i : i + 3]) for i in range(0, len(ALLOWED), 3)]
    return ReplyKeyboardMarkup(
        currency_buttons + [[BUTTON_BACK, BUTTON_BACK_TO_MENU]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


# 👇 Новая функция для inline-клавиатуры валют
def currency_inline_keyboard(show_others: bool = False) -> InlineKeyboardMarkup:
    """Создает inline-кнопки для выбора валюты."""
    rows = []
    
    if not show_others:
        cur_list = list(POPULAR_CURRENCIES)
        for i in range(0, len(cur_list), 3):
            row = [InlineKeyboardButton(c, callback_data=f"cur_select:{c}") for c in cur_list[i:i+3]]
            rows.append(row)
        rows.append([InlineKeyboardButton("Другие →", callback_data="cur_show_others")])
    else:
        cur_list = list(OTHER_CURRENCIES)
        for i in range(0, len(cur_list), 3):
            row = [InlineKeyboardButton(c, callback_data=f"cur_select:{c}") for c in cur_list[i:i+3]]
            rows.append(row)
        rows.append([InlineKeyboardButton("← Назад", callback_data="cur_show_popular")])
    
    return InlineKeyboardMarkup(rows)


def support_step_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_CLOSE_TICKET], [BUTTON_BACK_TO_MENU]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def exit_dialog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Завершить диалог",
                    callback_data=CALLBACK_DIALOG_EXIT,
                )
            ]
        ]
    )


def allowed_text() -> str:
    return ", ".join(ALLOWED)


EXCHANGE_MODE_BUY = "buy"
EXCHANGE_MODE_SELL = "sell"
EXCHANGE_MODE_SWAP = "swap"


def _exchange_mode_from_context(context: ContextTypes.DEFAULT_TYPE) -> str:
    m = context.user_data.get("exchange_mode", EXCHANGE_MODE_SWAP)
    if m not in (EXCHANGE_MODE_BUY, EXCHANGE_MODE_SELL, EXCHANGE_MODE_SWAP):
        return EXCHANGE_MODE_SWAP
    return m


def _clear_exchange_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("from_currency", "to_currency", "amount", "rate", "total", "exchange_step"):
        context.user_data.pop(k, None)


# 👇 Убрали перечисление валют из промптов
def prompt_buy_want_currency() -> str:
    return "Что хотите купить? Выберите валюту, которую получаете:"


def prompt_buy_pay_currency() -> str:
    return "Чем будете платить? Выберите валюту, которую отдаёте:"


def prompt_exchange_first_currency(mode: str) -> str:
    if mode == EXCHANGE_MODE_SELL:
        return "Что продаёте? Выберите валюту, которую отдаёте:"
    return "Выберите валюту, которую отдаёте:"


def prompt_exchange_second_currency(mode: str) -> str:
    if mode == EXCHANGE_MODE_SELL:
        return "Во что получаете выручку? Выберите валюту, которую получаете:"
    return "Выберите валюту, которую получаете:"


def prompt_exchange_amount(mode: str, from_currency: str) -> str:
    if mode == EXCHANGE_MODE_BUY:
        return f"Введите сумму оплаты в {from_currency} (сколько отдаёте):"
    if mode == EXCHANGE_MODE_SELL:
        return f"Введите объём продажи в {from_currency} (сколько отдаёте):"
    return "Введите сумму в валюте, которую отдаете:"


def prompt_exchange_wallet(mode: str, to_currency: str) -> str:
    if mode == EXCHANGE_MODE_BUY:
        return f"Укажите адрес для получения купленной валюты ({to_currency}):"
    if mode == EXCHANGE_MODE_SELL:
        return f"Укажите адрес для получения {to_currency}:"
    return f"Введите адрес для получения {to_currency}:"


def session_reset_exchange_hint() -> str:
    return "Начните заново: «Купить», «Продать» или /exchange."


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

# --- ОБНОВЛЕННЫЕ ФУНКЦИИ БАЗЫ ДАННЫХ ---

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                blocked INTEGER NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                order_type TEXT NOT NULL DEFAULT 'swap',
                from_currency TEXT NOT NULL,
                to_currency TEXT NOT NULL,
                amount REAL NOT NULL,
                rate REAL NOT NULL,
                total REAL NOT NULL,
                wallet_address TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                network TEXT,
                payment_method TEXT,
                receipt_file_id TEXT
            )
        """)

        # Новые таблицы для тикетов и истории чата
        cur.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                user_id INTEGER PRIMARY KEY,
                admin_id INTEGER,
                created_at TEXT,
                name TEXT,
                username TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                sender TEXT NOT NULL,
                text TEXT,
                created_at TEXT
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS currency_settings (
                currency TEXT PRIMARY KEY,
                rate_multiplier REAL NOT NULL DEFAULT 1.0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reserves (
                currency TEXT PRIMARY KEY,
                amount REAL NOT NULL DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                commission_percent REAL NOT NULL DEFAULT 1.0,
                min_amount_swap REAL NOT NULL DEFAULT 0.001,
                max_amount_swap REAL NOT NULL DEFAULT 10.0,
                min_amount_withdraw_rub REAL NOT NULL DEFAULT 500.0,
                support_contact TEXT NOT NULL DEFAULT '@your_support'
            )
        """)
        # Гарантируем нужные столбцы в orders (на случай обновлений)
        _ensure_column(conn, "orders", "network", "TEXT")
        _ensure_column(conn, "orders", "payment_method", "TEXT")
        _ensure_column(conn, "orders", "receipt_file_id", "TEXT")
        _ensure_column(conn, "orders", "tx_hash", "TEXT")
        _ensure_column(conn, "orders", "expires_at", "TEXT")

        cur.execute("SELECT COUNT(*) FROM settings")
        if cur.fetchone()[0] == 0:
            cur.execute("INSERT INTO settings (id) VALUES (1)")
        conn.commit()

def save_order(
    telegram_id: int,
    from_currency: str,
    to_currency: str,
    amount: float,
    rate: float,
    total: float,
    wallet_address: str,
    order_type: str = "swap",
    network: str = None,
    payment_method: str = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO orders (
                telegram_id, from_currency, to_currency, amount, rate, total,
                wallet_address, status, created_at, updated_at, order_type, network, payment_method
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id, from_currency, to_currency, amount, rate, total,
                wallet_address, STATUS_PENDING, now, now, order_type, network, payment_method,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


# --- Настройки ---


def get_settings() -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT commission_percent, min_amount_swap, max_amount_swap, min_amount_withdraw_rub, support_contact FROM settings WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {
                "commission_percent": 1.0,
                "min_amount_swap": 0.001,
                "max_amount_swap": 10.0,
                "min_amount_withdraw_rub": 500.0,
                "support_contact": "@your_support",
            }
        return {
            "commission_percent": row[0],
            "min_amount_swap": row[1],
            "max_amount_swap": row[2],
            "min_amount_withdraw_rub": row[3],
            "support_contact": row[4],
        }

def update_order_receipt(order_id: int, file_id: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET receipt_file_id = ?, updated_at = ? WHERE id = ?",
            (file_id, now, order_id),
        )
        conn.commit()
        return cur.rowcount > 0

def set_order_expiry(order_id: int, minutes: int = 60) -> str:
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE orders SET expires_at = ? WHERE id = ?", (expires_at, order_id))
        conn.commit()
    return expires_at


def update_order_tx_hash(order_id: int, tx_hash: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET tx_hash = ?, updated_at = ? WHERE id = ?",
            (tx_hash, now, order_id),
        )
        conn.commit()
        return cur.rowcount > 0


def is_sell_order_expired(order_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT expires_at, status FROM orders WHERE id = ?", (order_id,))
        row = cur.fetchone()
    if not row or not row[0]:
        return False
    if row[1] == STATUS_EXPIRED:
        return True
    try:
        exp_dt = datetime.fromisoformat(row[0])
        if datetime.now(timezone.utc) > exp_dt:
            update_order_status(order_id, STATUS_EXPIRED)
            return True
    except ValueError:
        pass
    return False

def expire_overdue_sell_orders() -> List[Tuple[int, int]]:
    """Находит заявки на продажу с истёкшим сроком (60 мин), меняет статус на 'Истекла'
    и возвращает список (order_id, telegram_id) для уведомления клиентов."""
    now_iso = datetime.now(timezone.utc).isoformat()
    now_dt = datetime.now(timezone.utc)
    expired_list: List[Tuple[int, int]] = []

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, telegram_id, expires_at
            FROM orders
            WHERE order_type = 'sell'
              AND status = ?
              AND expires_at IS NOT NULL
            """,
            (STATUS_PENDING,),
        )
        rows = cur.fetchall()

        for oid, tg_id, exp_str in rows:
            try:
                exp_dt = datetime.fromisoformat(exp_str)
                if now_dt > exp_dt:
                    cur.execute(
                        "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                        (STATUS_EXPIRED, now_iso, oid),
                    )
                    expired_list.append((int(oid), int(tg_id)))
            except ValueError:
                continue

        if expired_list:
            conn.commit()

    return expired_list

def mask_card_details(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        return f"{digits[:4]} **** **** {digits[-4:]}"
    return raw[:4] + "****" if len(raw) > 4 else raw

def update_setting(key: str, value) -> bool:
    allowed_keys = ("commission_percent", "min_amount_swap", "max_amount_swap", "min_amount_withdraw_rub", "support_contact")
    if key not in allowed_keys:
        return False
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(f"UPDATE settings SET {key} = ? WHERE id = 1", (value,))
        conn.commit()
        return cur.rowcount > 0


# --- Статистика для админа ---


def get_admin_stats() -> dict:
    with get_conn() as conn:
        cur = conn.cursor()
        
        cur.execute("SELECT COUNT(*) FROM orders")
        total_orders = cur.fetchone()[0]
        
        cur.execute("SELECT order_type, COUNT(*) FROM orders GROUP BY order_type")
        by_type = dict(cur.fetchall())
        
        cur.execute("SELECT SUM(total) FROM orders WHERE to_currency = 'RUB' OR from_currency = 'RUB'")
        total_rub = cur.fetchone()[0] or 0.0
        
        cur.execute("""
            SELECT telegram_id, COUNT(*) as cnt 
            FROM orders 
            GROUP BY telegram_id 
            ORDER BY cnt DESC 
            LIMIT 5
        """)
        top_users = cur.fetchall()
        
        return {
            "total_orders": total_orders,
            "by_type": by_type,
            "total_rub": total_rub,
            "top_users": top_users,
        }


# --- Тикеты поддержки ---


def create_ticket(user_id: int, admin_id: int, name: str = "Пользователь", username: str = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        _ensure_column(conn, "support_tickets", "name", "TEXT")
        _ensure_column(conn, "support_tickets", "username", "TEXT")
        
        cur.execute(
            """
            INSERT INTO support_tickets (user_id, admin_id, created_at, name, username)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
                SET admin_id = excluded.admin_id, created_at = excluded.created_at,
                    name = excluded.name, username = excluded.username
            """,
            (user_id, admin_id, now, name, username),
        )
        conn.commit()


def get_ticket(user_id: int) -> Optional[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, admin_id, created_at, name, username FROM support_tickets WHERE user_id = ?",
            (user_id,),
        )
        return cur.fetchone()


def find_ticket_by_admin(admin_id: int) -> Optional[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, admin_id, created_at, name, username FROM support_tickets "
            "WHERE admin_id = ? ORDER BY created_at DESC LIMIT 1",
            (admin_id,),
        )
        return cur.fetchone()


def list_tickets() -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, admin_id, created_at, name, username FROM support_tickets ORDER BY created_at DESC"
        )
        return [(int(r[0]), int(r[1]), str(r[2]), str(r[3] or ''), str(r[4] or '')) for r in cur.fetchall()]


def close_ticket(user_id: int) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM support_tickets WHERE user_id = ?", (user_id,))
        conn.commit()


def save_chat_message(user_id: int, sender: str, text: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO chat_messages (user_id, sender, text, created_at) VALUES (?, ?, ?, ?)",
            (user_id, sender, text, now),
        )
        conn.commit()


def get_chat_messages(user_id: int, limit: int = 50) -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, sender, text, created_at FROM chat_messages WHERE user_id = ? ORDER BY id ASC LIMIT ?",
            (user_id, limit),
        )
        return cur.fetchall()


# --- Пользователи / роли ---


def sync_admin_roles() -> None:
    admins = _admin_id_set()
    if not admins:
        return
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        for aid in admins:
            cur.execute("SELECT 1 FROM users WHERE telegram_id = ?", (aid,))
            if cur.fetchone():
                cur.execute(
                    "UPDATE users SET role = ? WHERE telegram_id = ?",
                    (ROLE_ADMIN, aid),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO users (telegram_id, created_at, role, blocked)
                    VALUES (?, ?, ?, 0)
                    """,
                    (aid, now, ROLE_ADMIN),
                )
        conn.commit()


def get_user_row(telegram_id: int) -> Optional[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT telegram_id, created_at, role, blocked FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        return cur.fetchone()


def is_user_blocked(telegram_id: int) -> bool:
    row = get_user_row(telegram_id)
    if not row:
        return False
    return int(row[3]) == 1


def is_admin(telegram_id: int) -> bool:
    if telegram_id in _admin_id_set():
        return True
    row = get_user_row(telegram_id)
    if not row:
        return False
    return row[2] == ROLE_ADMIN


def register_user(telegram_id: int) -> bool:
    role = ROLE_ADMIN if telegram_id in _admin_id_set() else ROLE_USER
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        exists = cur.fetchone() is not None
        if not exists:
            cur.execute(
                """
                INSERT INTO users (telegram_id, created_at, role, blocked)
                VALUES (?, ?, ?, 0)
                """,
                (telegram_id, datetime.now(timezone.utc).isoformat(), role),
            )
            conn.commit()
    return not exists


# --- Курсы ---


def _fetch_rates_from_coingecko() -> Dict[str, float]:
    params = {
        "ids": ",".join(COIN_IDS.values()),
        "vs_currencies": "usd,rub",
    }
    resp = requests.get(COINGECKO_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    rates: Dict[str, float] = {}
    usd_per_rub = None
    for ticker, coin_id in COIN_IDS.items():
        if coin_id not in data or "usd" not in data[coin_id] or "rub" not in data[coin_id]:
            raise ValueError(f"Нет курса для {ticker} в ответе CoinGecko")
        usd_value = float(data[coin_id]["usd"])
        rub_value = float(data[coin_id]["rub"])
        rates[ticker] = usd_value
        if rub_value > 0 and usd_per_rub is None:
            usd_per_rub = usd_value / rub_value

    if usd_per_rub is None:
        raise ValueError("Не удалось рассчитать курс RUB в USD")
    rates["RUB"] = usd_per_rub
    return rates


def get_exchange_rates(force_refresh: bool = False) -> Dict[str, float]:
    global _rates_cache
    now = time.monotonic()
    if not force_refresh and _rates_cache is not None:
        ts, cached = _rates_cache
        if now - ts < RATES_TTL:
            return dict(cached)

    with _rates_lock:
        if not force_refresh and _rates_cache is not None:
            ts, cached = _rates_cache
            if now - ts < RATES_TTL:
                return dict(cached)
        fresh = _fetch_rates_from_coingecko()
        _rates_cache = (time.monotonic(), fresh)
        return dict(fresh)


def _load_rate_multipliers() -> Dict[str, float]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT currency, rate_multiplier FROM currency_settings")
        return {str(r[0]).upper(): float(r[1]) for r in cur.fetchall()}


def get_effective_exchange_rates() -> Dict[str, float]:
    base = get_exchange_rates()
    mults = _load_rate_multipliers()
    out: Dict[str, float] = {}
    for c, v in base.items():
        m = mults.get(c, 1.0)
        out[c] = v * m
    return out


def set_currency_multiplier(currency: str, multiplier: float) -> None:
    c = currency.upper()
    if c not in ALLOWED:
        raise ValueError("Неизвестная валюта.")
    if multiplier <= 0:
        raise ValueError("Множитель должен быть положительным.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO currency_settings (currency, rate_multiplier)
            VALUES (?, ?)
            ON CONFLICT(currency) DO UPDATE SET rate_multiplier = excluded.rate_multiplier
            """,
            (c, multiplier),
        )
        conn.commit()


def get_reserve(currency: str) -> float:
    c = currency.upper()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT amount FROM reserves WHERE currency = ?", (c,))
        row = cur.fetchone()
        return float(row[0]) if row else 0.0


def set_reserve_amount(currency: str, amount: float) -> None:
    c = currency.upper()
    if c not in ALLOWED:
        raise ValueError("Неизвестная валюта.")
    if amount < 0:
        raise ValueError("Резерв не может быть отрицательным.")
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO reserves (currency, amount)
            VALUES (?, ?)
            ON CONFLICT(currency) DO UPDATE SET amount = excluded.amount
            """,
            (c, amount),
        )
        conn.commit()


def list_reserves() -> List[Tuple[str, float]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT currency, amount FROM reserves ORDER BY currency")
        return [(str(r[0]), float(r[1])) for r in cur.fetchall()]


def list_rate_multipliers() -> List[Tuple[str, float]]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT currency, rate_multiplier FROM currency_settings ORDER BY currency")
        return [(str(r[0]), float(r[1])) for r in cur.fetchall()]


def set_user_blocked(telegram_id: int, blocked: bool) -> bool:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        exists = cur.fetchone() is not None
        if not exists:
            if not blocked:
                return False
            cur.execute(
                """
                INSERT INTO users (telegram_id, created_at, role, blocked)
                VALUES (?, ?, ?, 1)
                """,
                (telegram_id, datetime.now(timezone.utc).isoformat(), ROLE_USER),
            )
            conn.commit()
            return True
        cur.execute(
            "UPDATE users SET blocked = ? WHERE telegram_id = ?",
            (1 if blocked else 0, telegram_id),
        )
        conn.commit()
    return True


def get_user_orders(telegram_id: int, limit: int = 10) -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, from_currency, to_currency, amount, rate, total, wallet_address, status, created_at
            FROM orders
            WHERE telegram_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (telegram_id, limit),
        )
        return cur.fetchall()


def delete_completed_orders(telegram_id: int) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM orders WHERE telegram_id = ? AND status = ?",
            (telegram_id, STATUS_DONE),
        )
        conn.commit()
        return cur.rowcount


def get_order_by_id(order_id: int) -> Optional[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, telegram_id, from_currency, to_currency, amount, rate, total,
                   wallet_address, status, created_at, updated_at
            FROM orders WHERE id = ?
            """,
            (order_id,),
        )
        return cur.fetchone()


def update_order_status(order_id: int, new_status: str) -> bool:
    if new_status not in ADMIN_ALLOWED_STATUSES:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, order_id),
        )
        conn.commit()
        return cur.rowcount > 0


def list_purchase_orders(limit: int = 20) -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, telegram_id, from_currency, to_currency, amount, total, network, payment_method, status
            FROM orders
            WHERE order_type = 'buy' AND status != 'Выполнена' AND status != 'Отменена'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()

def list_sell_orders(limit: int = 20) -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, telegram_id, from_currency, to_currency, amount, total,
                   network, payment_method, wallet_address, status, tx_hash
            FROM orders
            WHERE order_type = 'sell'
              AND status NOT IN (?, ?, ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (STATUS_DONE, STATUS_CANCELLED, STATUS_EXPIRED, limit),
        )
        return cur.fetchall()

def list_recent_orders_all(limit: int = 20) -> List[Tuple]:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, telegram_id, from_currency, to_currency, amount, status, created_at
            FROM orders
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return cur.fetchall()


def format_recent_orders_broadcast(limit: int = 25) -> str:
    rows = list_recent_orders_all(limit)
    if not rows:
        return "Заявок пока нет."
    lines = ["Последние заявки:"]
    for r in rows:
        oid, tg_id, from_c, to_c, amt, st, crt = r
        lines.append(f"\n#{oid} | user {tg_id} | {st}\n{amt} {from_c} → {to_c}\n{crt}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n…"
    return msg


def format_tickets_broadcast() -> str:
    rows = list_tickets()
    if not rows:
        return "Активных обращений нет."
    lines = ["🗂 Активные обращения:"]
    for user_id, admin_id, created, name, username in rows:
        lines.append(f"\nuser {user_id} ({name}) ↔ admin {admin_id}\nсоздано: {created}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:3900] + "\n…"
    return msg


# --- Клавиатуры ---


def get_user_menu_state(telegram_id: int) -> dict:
    # Перед подсчётом проверяем, не истекли ли просроченные заявки пользователя
    expire_overdue_sell_orders()

    state = {
        "has_draft": False,
        "active_orders_count": 0,
        "has_completed_orders": False,
        "completed_orders_count": 0,
    }

    rows = get_user_orders(telegram_id, limit=50)
    active_count = 0
    completed_count = 0
    for row in rows:
        status = row[7]
        if status in ACTIVE_ORDER_STATUSES:
            active_count += 1
        elif status == STATUS_DONE:
            completed_count += 1

    state["active_orders_count"] = active_count
    state["has_completed_orders"] = completed_count > 0
    state["completed_orders_count"] = completed_count
    return state


def get_context_inline_keyboard(
    telegram_id: int, context: ContextTypes.DEFAULT_TYPE = None
) -> Optional[InlineKeyboardMarkup]:
    state = get_user_menu_state(telegram_id)
    buttons = []

    if context and context.user_data.get("buy_draft"):
        buttons.append(
            [InlineKeyboardButton("▶️ Продолжить покупку", callback_data="ctx:continue_buy")]
        )

    if context and context.user_data.get("sell_draft"):
        buttons.append(
            [InlineKeyboardButton("▶️ Продолжить продажу", callback_data="ctx:continue_sell")]
        )

    if state["has_draft"]:
        buttons.append(
            [InlineKeyboardButton(BUTTON_CONTINUE_DRAFT, callback_data="ctx:continue_draft")]
        )

    if state["active_orders_count"] == 1:
        orders = get_user_orders(telegram_id, limit=10)
        for o in orders:
            if o[7] in ACTIVE_ORDER_STATUSES:
                buttons.append(
                    [InlineKeyboardButton(f"{BUTTON_OPEN_ORDER} #{o[0]}", callback_data=f"ctx:open_order:{o[0]}")]
                )
                break
    elif state["active_orders_count"] > 1:
        buttons.append(
            [InlineKeyboardButton(BUTTON_ACTIVE_ORDERS, callback_data="ctx:active_orders")]
        )

    if state["has_completed_orders"]:
        buttons.append([InlineKeyboardButton(BUTTON_REPEAT, callback_data="ctx:repeat")])

        if state["completed_orders_count"] > 10:
            buttons.append(
                [InlineKeyboardButton("🗑 Удалить выполненные заявки", callback_data="ctx:delete_completed")]
            )

    if buttons:
        return InlineKeyboardMarkup(buttons)
    return None


def main_menu_keyboard(telegram_id: int) -> ReplyKeyboardMarkup:
    rows: List[List[KeyboardButton]] = []
    
    rows.append([KeyboardButton(BUTTON_BUY), KeyboardButton(BUTTON_SELL)])
    rows.append([KeyboardButton(BUTTON_SWAP)])
    rows.append([KeyboardButton(BUTTON_AML)])
    rows.append([KeyboardButton(BUTTON_WALLET), KeyboardButton(BUTTON_CABINET)])
    rows.append([KeyboardButton(BUTTON_RATES), KeyboardButton(BUTTON_SUPPORT)])
    rows.append([KeyboardButton(BUTTON_NEWS), KeyboardButton(BUTTON_REVIEW)])
    rows.append([KeyboardButton(BUTTON_MORE)])

    if is_admin(telegram_id):
        rows.append([KeyboardButton("/admin"), KeyboardButton("📋 Сводка заявок")])
        
    rows.append([KeyboardButton(BUTTON_CANCEL)])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=False)


def more_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BUTTON_HOW_TO_EXCHANGE), KeyboardButton(BUTTON_ABOUT)],
            [KeyboardButton(BUTTON_FAVORITES), KeyboardButton(BUTTON_MY_TICKETS)],
            [KeyboardButton(BUTTON_NOTIFICATION_SETTINGS), KeyboardButton(BUTTON_LANGUAGE)],
            [KeyboardButton(BUTTON_EXCHANGE_RULES), KeyboardButton(BUTTON_SHARE)],
            [KeyboardButton(BUTTON_BACK_TO_MAIN)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def cabinet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📂 Мои заявки", callback_data="cab:orders")],
        [InlineKeyboardButton("✉️ Отправить приглашения", callback_data="cab:invite")],
        [InlineKeyboardButton("💸 Вывод средств", callback_data="cab:withdraw")],
        [InlineKeyboardButton("↩️ Назад в меню", callback_data="cab:back")],
    ])



def blocked_user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BUTTON_SUPPORT)], [KeyboardButton(BUTTON_CLEAN)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [BUTTON_A_ORDERS, BUTTON_A_CONFIRM],
            [BUTTON_A_RATES, BUTTON_A_RESERVES],
            [BUTTON_A_BLOCK, BUTTON_A_UNBLOCK],
            [BUTTON_A_TICKETS, BUTTON_A_REFRESH_RATES],
            ["🛒 Заявки на покупку", "💸 Заявки на продажу"],
            [BUTTON_A_EXIT],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

async def buy_paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "buy_menu":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 Основное меню:",
            reply_markup=main_menu_keyboard(update.effective_user.id)
        )
        return CHOOSING
        
    order_id = int(data.split(":")[1])
    row = get_order_by_id(order_id)
    if not row:
        await query.edit_message_text("Заявка не найдена.")
        return CHOOSING
        
    context.user_data["buy_receipt_order_id"] = order_id
    
    # Создаем inline-кнопку "Загрузить чек"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📎 Как загрузить чек", callback_data=f"buy_upload_receipt:{order_id}")],
        [InlineKeyboardButton("В меню", callback_data="buy_menu")]
    ])
    
    await query.edit_message_text(
        f"Загрузите чек об оплате по заявке {order_id}.\n\n"
        f"👇 Нажмите на кнопку ниже или используйте скрепку 📎 в строке ввода, чтобы прикрепить фото или файл.",
        reply_markup=keyboard
    )
    return BUY_RECEIPT_UPLOAD

async def buy_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING

    # Нажали "В меню" — НЕ удаляем buy_draft, чтобы в меню появилась кнопка "Продолжить обмен"
    if update.message.text == BUTTON_BACK_TO_MENU:
        context.user_data.pop("buy_last_state", None)
        return await show_menu(update, context, preface="Покупка приостановлена. Черновик сохранён.\n")

    current_state = context.user_data.get("buy_last_state")
    draft = context.user_data.get("buy_draft", {})

    # Если мы на первом шаге (выбор валюты) — возвращаемся в меню, сохраняя черновик
    if current_state is None or current_state == BUY_CURRENCY:
        if current_state == BUY_CURRENCY and "currency" in draft:
            context.user_data.pop("buy_last_state", None)
            keyboard = [[InlineKeyboardButton(c, callback_data=f"buy_cur:{c}")] for c in ALLOWED_BUY_CRYPTO]
            await update.message.reply_text(
                "Какую криптовалюту хотите купить? Выберите вариант из списка.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return BUY_CURRENCY

        context.user_data.pop("buy_last_state", None)
        return await show_menu(update, context, preface="Возврат в меню. Черновик сохранён.\n")

    if current_state == BUY_NETWORK:
        context.user_data.pop("buy_last_state", None)
        keyboard = [[InlineKeyboardButton(c, callback_data=f"buy_cur:{c}")] for c in ALLOWED_BUY_CRYPTO]
        await update.message.reply_text(
            "Какую криптовалюту хотите купить? Выберите вариант из списка.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BUY_CURRENCY

    elif current_state == BUY_PAYMENT:
        crypto = draft.get("currency")
        networks = CRYPTO_NETWORKS.get(crypto, [])
        if len(networks) > 1:
            context.user_data["buy_last_state"] = BUY_NETWORK
            keyboard = [[InlineKeyboardButton(net, callback_data=f"buy_net:{net}")] for net in networks]
            await update.message.reply_text(
                f"Вы выбрали: <b>{crypto}</b>\nВыберите сеть для получения:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return BUY_NETWORK
        else:
            # Без рекурсии: сразу отдаём экран выбора валюты
            context.user_data.pop("buy_last_state", None)
            keyboard = [[InlineKeyboardButton(c, callback_data=f"buy_cur:{c}")] for c in ALLOWED_BUY_CRYPTO]
            await update.message.reply_text(
                "Какую криптовалюту хотите купить? Выберите вариант из списка.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return BUY_CURRENCY

    elif current_state == BUY_AMOUNT_MODE:
        context.user_data["buy_last_state"] = BUY_PAYMENT
        keyboard = [[InlineKeyboardButton(method, callback_data=f"buy_pay:{method}")] for method in PAYMENT_METHODS]
        await update.message.reply_text(
            f"Вы покупаете: <b>{draft.get('currency')}</b>\n"
            f"Сеть получения: <b>{draft.get('network')}</b>\n"
            f"Выберите способ оплаты:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BUY_PAYMENT

    elif current_state == BUY_AMOUNT_INPUT:
        context.user_data["buy_last_state"] = BUY_AMOUNT_MODE
        keyboard = [
            [InlineKeyboardButton("Сумму оплаты (в RUB)", callback_data="buy_mode:fiat")],
            [InlineKeyboardButton("Сумму криптовалюты", callback_data="buy_mode:crypto")]
        ]
        await update.message.reply_text(
            f"К покупке: <b>{draft.get('currency')}</b>\n"
            f"Сеть: <b>{draft.get('network')}</b>\n"
            f"Способ оплаты: <b>{draft.get('payment_method')}</b>\n\n"
            f"Какую сумму хотите указать?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BUY_AMOUNT_MODE

    elif current_state == BUY_RATE_CONFIRM:
        context.user_data["buy_last_state"] = BUY_AMOUNT_INPUT
        prompt = (
            "Введите сумму в рублях (RUB), которую хотите оплатить:"
            if draft.get("amount_mode") == "fiat"
            else f"Введите количество {draft.get('currency')}, которое хотите получить:"
        )
        await update.message.reply_text(prompt)
        return BUY_AMOUNT_INPUT

    elif current_state == BUY_WALLET_INPUT:
        context.user_data["buy_last_state"] = BUY_RATE_CONFIRM
        keyboard = [
            [InlineKeyboardButton("Подтвердить расчёт", callback_data="buy_rate:confirm")],
            [InlineKeyboardButton("Изменить сумму", callback_data="buy_rate:change")]
        ]
        await update.message.reply_text(
            f"Оплата: <b>{draft.get('pay_amount', 0):.2f} RUB</b>\n"
            f"Получение: <b>{draft.get('receive_amount', 0):.6f} {draft.get('currency')}</b>\n"
            f"Курс: <b>1 {draft.get('currency')} = {draft.get('rate', 0):.2f} RUB</b>\n\n"
            f"Подтвердите расчёт:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return BUY_RATE_CONFIRM

    elif current_state == BUY_WALLET_CONFIRM:
        context.user_data["buy_last_state"] = BUY_WALLET_INPUT
        await update.message.reply_text(
            f"Укажите адрес кошелька для получения <b>{draft.get('currency')}</b>.\n"
            f"Сеть: <b>{draft.get('network')}</b>.",
            parse_mode="HTML"
        )
        return BUY_WALLET_INPUT

    return await show_menu(update, context)


async def buy_upload_receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    order_id = int(query.data.split(":")[1])
    context.user_data["buy_receipt_order_id"] = order_id
    
    await query.edit_message_text(
        f"Чтобы загрузить чек, нажмите на скрепку 📎 в строке ввода сообщения и выберите фото или файл.\n\n"
        f"Ожидаю ваш чек по заявке #{order_id}."
    )
    return BUY_RECEIPT_UPLOAD

async def admin_purchase_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    if not query or not query.data:
        return None
    await query.answer()
    
    uid = update.effective_user.id
    if not is_admin(uid):
        return None
        
    m = re.match(r"^pa:(\d+):([A-Z]+)$", query.data)
    if not m:
        return None
        
    oid = int(m.group(1))
    action = m.group(2)
    
    new_status = None
    if action == "CONFIRM":
        new_status = STATUS_PAYMENT_CONFIRMED
    elif action == "DONE":
        new_status = STATUS_DONE
    elif action == "CANCEL":
        new_status = STATUS_CANCELLED
        
    if not new_status:
        return None
        
    update_order_status(oid, new_status)
    await query.edit_message_text(f"Заявка #{oid} обновлена. Новый статус: {new_status}")
    
    # Уведомляем клиента
    row = get_order_by_id(oid)
    if row:
        tg_id = row[1]
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=f"Статус вашей заявки #{oid} изменился на: <b>{new_status}</b>",
                parse_mode="HTML"
            )
        except TelegramError:
            pass
            
    return A_MENU

async def admin_sell_status_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    query = update.callback_query
    if not query or not query.data:
        return None
    await query.answer()

    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        return None

    m = re.match(r"^sa:(\d+):([A-Z_]+)$", query.data)
    if not m:
        return None

    oid = int(m.group(1))
    action = m.group(2)

    row = get_order_by_id(oid)
    if not row:
        await query.edit_message_text("Заявка не найдена.")
        return A_MENU

    # row: (0:id, 1:telegram_id, 2:from_currency, 3:to_currency, 4:amount, 5:rate, 6:total, 7:wallet_address, 8:status, ...)
    tg_id = row[1]
    from_c = row[2]
    to_c = row[3]
    amount = float(row[4])
    total = float(row[6])
    wallet = row[7]

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT payment_method FROM orders WHERE id = ?", (oid,))
        pm_row = cur.fetchone()
    method = pm_row[0] if pm_row else ""
    is_qr = (method == SELL_METHOD_QR)

    # 1. Криптовалюта получена и проверена (Экран 11 или Экран 11Б)
    if action == "CRYPTO_OK":
        if is_qr:
            new_status = STATUS_SELL_WAIT_ADMIN_QR
            update_order_status(oid, new_status)
            client_text = (
                f"Криптовалюта по заявке <b>#{oid}</b> поступила и проверена.\n"
                f"К выдаче: <b>{total:.2f} {to_c}</b>.\n"
                f"Администратор свяжется с вами, чтобы согласовать время получения наличных через банкомат по QR-коду. "
                f"Следите за сообщениями в этом чате."
            )
        else:
            new_status = STATUS_SELL_PAYOUT_PROCESS
            update_order_status(oid, new_status)
            masked = mask_card_details(wallet)
            client_text = (
                f"Криптовалюта по заявке <b>#{oid}</b> поступила и проверена.\n"
                f"Готовим выплату: <b>{total:.2f} {to_c}</b>. Карта: <code>{masked}</code>.\n"
                f"Статус: <b>{new_status}</b>. Мы сообщим, когда выплата будет выполнена."
            )

        await query.edit_message_text(
            f"Заявка #{oid} переведена в статус: <b>{new_status}</b>",
            parse_mode="HTML",
            reply_markup=status_keyboard_sell(oid, is_qr=is_qr),
        )
        try:
            await context.bot.send_message(chat_id=tg_id, text=client_text, parse_mode="HTML")
        except TelegramError:
            pass
        return A_MENU

    # 2. Ветка QR — Экран 12Б: запрос даты и времени у админа
    if action == "QR_TIME":
        context.user_data["admin_qr_order_id"] = oid
        await query.edit_message_text(
            f"Введите согласованную дату и время получения наличных по заявке <b>#{oid}</b> "
            f"(например: <code>25.11.2026 в 15:30</code>):",
            parse_mode="HTML",
        )
        return A_SELL_QR_TIME

    # 3. Ветка QR — Экран 13Б: запрос QR-кода у клиента
    if action == "QR_ASK":
        # Привязываем клиента к текущему админу, чтобы фото/сообщения ушли напрямую ему
        create_ticket(user_id=tg_id, admin_id=uid, name=f"Заявка #{oid}")

        await query.edit_message_text(
            f"Инструкция по формированию QR-кода отправлена клиенту по заявке #{oid}.\n"
            f"Чат с клиентом открыт — ожидайте фото QR-кода.",
            reply_markup=status_keyboard_sell(oid, is_qr=True),
        )
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"Сформируйте QR-код у банкомата и отправьте его фото или скриншот прямо в этот чат.\n"
                    f"Администратор обработает QR-код для снятия средств по заявке <b>#{oid}</b>. "
                    f"К выдаче: <b>{total:.2f} {to_c}</b>."
                ),
                parse_mode="HTML",
                reply_markup=support_step_keyboard(),
            )
        except TelegramError:
            pass
        return A_MENU

    # 4. Завершение выплаты (Экран 12 для карты или Экран 14Б для QR)
    if action == "DONE":
        update_order_status(oid, STATUS_DONE)
        if is_qr:
            client_text = (
                f"Заявка <b>#{oid}</b> выполнена.\n"
                f"Наличные выданы: <b>{total:.2f} {to_c}</b>.\n"
                f"Продано: <b>{amount:.6f} {from_c}</b>."
            )
        else:
            masked = mask_card_details(wallet)
            client_text = (
                f"Заявка <b>#{oid}</b> выполнена.\n"
                f"На карту <code>{masked}</code> отправлено <b>{total:.2f} {to_c}</b>.\n"
                f"Продано: <b>{amount:.6f} {from_c}</b>."
            )

        await query.edit_message_text(f"✅ Заявка #{oid} завершена ({STATUS_DONE}).")
        try:
            await context.bot.send_message(chat_id=tg_id, text=client_text, parse_mode="HTML")
        except TelegramError:
            pass
        return A_MENU

    # 5. Отмена заявки
    if action == "CANCEL":
        update_order_status(oid, STATUS_CANCELLED)
        await query.edit_message_text(f"❌ Заявка #{oid} отменена ({STATUS_CANCELLED}).")
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=f"Ваша заявка на продажу <b>#{oid}</b> была отменена администратором.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass
        return A_MENU

    return A_MENU


async def admin_sell_qr_time_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return A_SELL_QR_TIME

    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed

    oid = context.user_data.pop("admin_qr_order_id", None)
    if not oid:
        await update.message.reply_text("Заявка не найдена.", reply_markup=admin_menu_keyboard())
        return A_MENU

    agreed_time = update.message.text.strip()
    row = get_order_by_id(oid)
    if row:
        tg_id = row[1]
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"Получение наличных по заявке <b>#{oid}</b> согласовано на <b>{agreed_time}</b>.\n"
                    f"QR-код будет сформирован у банкомата. Следуйте дальнейшим инструкциям администратора."
                ),
                parse_mode="HTML",
            )
        except TelegramError:
            pass

    await update.message.reply_text(
        f"Время ({agreed_time}) по заявке #{oid} отправлено клиенту.",
        reply_markup=status_keyboard_sell(oid, is_qr=True),
    )
    return A_MENU

async def buy_receipt_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return BUY_RECEIPT_UPLOAD

    order_id = context.user_data.get("buy_receipt_order_id")
    if not order_id:
        await update.message.reply_text("Ошибка: заявка не найдена. Начните заново.")
        return CHOOSING

    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id

    if not file_id:
        await update.message.reply_text("Пожалуйста, отправьте фото или файл с чеком.")
        return BUY_RECEIPT_UPLOAD

    # Сохраняем file_id в БД и меняем статус
    update_order_receipt(order_id, file_id)
    update_order_status(order_id, STATUS_RECEIPT_UPLOADED)

    # Берём дополнительные поля (network, payment_method) для уведомления админов
    network = None
    payment_method = None
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT telegram_id, from_currency, to_currency, amount, rate, total, wallet_address, status, created_at, network, payment_method FROM orders WHERE id = ?", (order_id,))
        orow = cur.fetchone()
        if orow:
            tg_id = orow[0]
            from_c = orow[1]
            to_c = orow[2]
            amount = float(orow[3]) if orow[3] is not None else 0.0
            rate = float(orow[4]) if orow[4] is not None else 0.0
            total = float(orow[5]) if orow[5] is not None else 0.0
            wallet = orow[6]
            status = orow[7]
            created_at = orow[8]
            network = orow[9]
            payment_method = orow[10]
        else:
            tg_id = None

    # Уведомляем админов
    if tg_id is not None:
        admins = _all_admin_ids()
        for aid in admins:
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text=(
                        f"🧾 Новый чек по заявке #{order_id}\n"
                        f"User: {tg_id}\n"
                        f"Покупка: {total:.6f} {to_c}\n"
                        f"Оплата: {amount:.2f} {from_c}\n"
                        f"Сеть: {network or '—'}\n"
                        f"Способ оплаты: {payment_method or '—'}\n"
                        f"Статус: {STATUS_RECEIPT_UPLOADED}"
                    ),
                    reply_markup=status_keyboard_purchase(order_id),
                )
                # Отправляем сам чек
                if update.message.photo:
                    await context.bot.send_photo(chat_id=aid, photo=file_id)
                elif update.message.document:
                    await context.bot.send_document(chat_id=aid, document=file_id)
            except TelegramError:
                pass

    await update.message.reply_text(
        "Чек получен! Ожидайте проверки оплаты оператором.\n"
        "Мы сообщим, когда заявка будет выполнена.",
        reply_markup=main_menu_keyboard(update.effective_user.id)
    )
    context.user_data.pop("buy_receipt_order_id", None)
    return CHOOSING

def buy_navigation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[BUTTON_BACK, BUTTON_BACK_TO_MENU]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING

    # Сохраняем черновик
    context.user_data["buy_draft"] = {"step": "currency"}
    
    # Меняем нижнюю клавиатуру на "Назад" и "В меню"
    await update.message.reply_text(
        "Начинаем покупку...",
        reply_markup=buy_navigation_keyboard()
    )
    
    keyboard = []
    for crypto in ALLOWED_BUY_CRYPTO:
        keyboard.append([InlineKeyboardButton(crypto, callback_data=f"buy_cur:{crypto}")])
    
    # Убираем "В меню" из inline-кнопок, так как теперь есть нижняя кнопка
    await update.message.reply_text(
        "Какую криптовалюту хотите купить? Выберите вариант из списка.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BUY_CURRENCY

async def buy_currency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    crypto = query.data.split(":")[1]
    context.user_data["buy_draft"]["currency"] = crypto
    context.user_data["buy_draft"]["step"] = "network"
        
    networks = CRYPTO_NETWORKS.get(crypto, [])
    if len(networks) == 1:
        context.user_data["buy_draft"]["network"] = networks[0]
        context.user_data["buy_last_state"] = BUY_CURRENCY
        return await buy_choose_payment(update, context, query)
        
    keyboard = []
    for net in networks:
        keyboard.append([InlineKeyboardButton(net, callback_data=f"buy_net:{net}")])
    keyboard.append([InlineKeyboardButton("В меню", callback_data="buy_menu")])
    
    await query.edit_message_text(
        f"Вы выбрали: <b>{crypto}</b>\nВыберите сеть для получения. Убедитесь, что она совпадает с сетью вашего кошелька.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["buy_last_state"] = BUY_CURRENCY
    return BUY_NETWORK

async def buy_network_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    network = query.data.split(":")[1]
    context.user_data["buy_draft"]["network"] = network
    context.user_data["buy_last_state"] = BUY_NETWORK
    return await buy_choose_payment(update, context, query)

async def buy_choose_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None) -> int:
    draft = context.user_data.get("buy_draft", {})
    crypto = draft.get("currency")
    network = draft.get("network")
    
    keyboard = []
    for method in PAYMENT_METHODS:
        keyboard.append([InlineKeyboardButton(method, callback_data=f"buy_pay:{method}")])
    keyboard.append([InlineKeyboardButton("В меню", callback_data="buy_menu")])
    
    text = (
        f"Вы покупаете: <b>{crypto}</b>\n"
        f"Сеть получения: <b>{network}</b>\n"
        f"Выберите способ оплаты."
    )
    
    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return BUY_PAYMENT

async def buy_payment_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    method = query.data.split(":")[1]
    context.user_data["buy_draft"]["payment_method"] = method
    
    # Экран 4: Выбор режима ввода
    keyboard = [
        [InlineKeyboardButton("Сумму оплаты (в RUB)", callback_data="buy_mode:fiat")],
        [InlineKeyboardButton("Сумму криптовалюты", callback_data="buy_mode:crypto")],
        [InlineKeyboardButton("В меню", callback_data="buy_menu")]
    ]
    await query.edit_message_text(
        f"К покупке: <b>{context.user_data['buy_draft']['currency']}</b>\n"
        f"Сеть: <b>{context.user_data['buy_draft']['network']}</b>\n"
        f"Способ оплаты: <b>{context.user_data['buy_draft']['payment_method']}</b>\n\n"
        f"Какую сумму хотите указать?",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data["buy_last_state"] = BUY_PAYMENT
    return BUY_AMOUNT_MODE

async def buy_amount_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    mode = query.data.split(":")[1]
    context.user_data["buy_draft"]["amount_mode"] = mode
    
    if mode == "fiat":
        prompt = "Введите сумму в рублях (RUB), которую хотите оплатить:"
    else:
        prompt = f"Введите количество {context.user_data['buy_draft']['currency']}, которое хотите получить:"
        
    await query.edit_message_text(prompt)
    context.user_data["buy_last_state"] = BUY_AMOUNT_MODE
    return BUY_AMOUNT_INPUT

async def buy_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return BUY_AMOUNT_INPUT
        
    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("Введите корректную положительную сумму (например, 5000 или 10.5).")
        return BUY_AMOUNT_INPUT
        
    draft = context.user_data["buy_draft"]
    crypto = draft["currency"]
    mode = draft["amount_mode"]
    
    # Получаем курсы
    try:
        rates = get_effective_exchange_rates()
        # rate = сколько RUB за 1 единицу крипты
        rub_per_crypto = rates[crypto] / rates["RUB"] 
    except Exception:
        await update.message.reply_text("Не удалось получить курс. Попробуйте позже.")
        context.user_data["buy_last_state"] = BUY_AMOUNT_INPUT
        return BUY_AMOUNT_INPUT
        
    if mode == "fiat":
        pay_amount = amount
        receive_amount = pay_amount / rub_per_crypto
    else:
        receive_amount = amount
        pay_amount = receive_amount * rub_per_crypto
        
    draft["pay_amount"] = pay_amount
    draft["receive_amount"] = receive_amount
    draft["rate"] = rub_per_crypto
    
    # Экран 5: Проверка расчёта
    keyboard = [
        [InlineKeyboardButton("Подтвердить расчёт", callback_data="buy_rate:confirm")],
        [InlineKeyboardButton("Изменить сумму", callback_data="buy_rate:change")],
        [InlineKeyboardButton("В меню", callback_data="buy_menu")]
    ]
    
    text = (
        f"Проверьте условия покупки:\n\n"
        f"Оплата: <b>{pay_amount:.2f} RUB</b>\n"
        f"Способ оплаты: <b>{draft['payment_method']}</b>\n"
        f"Получение: <b>{receive_amount:.6f} {crypto}</b>\n"
        f"Сеть получения: <b>{draft['network']}</b>\n"
        f"Курс: <b>1 {crypto} = {rub_per_crypto:.2f} RUB</b>\n\n"
        f"Подтвердите расчёт, чтобы продолжить."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["buy_last_state"] = BUY_AMOUNT_INPUT
    return BUY_RATE_CONFIRM

async def buy_rate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    action = query.data.split(":")[1]
    
    if action == "change":
        # Возвращаемся к вводу суммы
        draft = context.user_data["buy_draft"]
        if draft["amount_mode"] == "fiat":
            prompt = "Введите сумму в рублях (RUB), которую хотите оплатить:"
        else:
            prompt = f"Введите количество {draft['currency']}, которое хотите получить:"
        await query.edit_message_text(prompt)
        return BUY_AMOUNT_INPUT
        
    if action == "confirm":
        # Экран 6: Ввод адреса
        draft = context.user_data["buy_draft"]
        await query.edit_message_text(
            f"Укажите адрес кошелька для получения <b>{draft['currency']}</b>.\n"
            f"Сеть: <b>{draft['network']}</b>.\n"
            f"Проверьте, что адрес поддерживает эту сеть. Неверный адрес или сеть могут привести к непоступлению средств.",
            parse_mode="HTML"
        )
        context.user_data["buy_last_state"] = BUY_RATE_CONFIRM
        return BUY_WALLET_INPUT

async def buy_wallet_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return BUY_WALLET_INPUT
        
    wallet = update.message.text.strip()
    if len(wallet) < 8:
        await update.message.reply_text("Похоже, адрес слишком короткий. Введите корректный адрес.")
        return BUY_WALLET_INPUT
        
    context.user_data["buy_draft"]["wallet"] = wallet
    
    # Экран 7: Подтверждение адреса
    draft = context.user_data["buy_draft"]
    keyboard = [
        [InlineKeyboardButton("Подтвердить адрес", callback_data="buy_wallet:confirm")],
        [InlineKeyboardButton("Изменить адрес", callback_data="buy_wallet:change")],
        [InlineKeyboardButton("В меню", callback_data="buy_menu")]
    ]
    
    text = (
        f"Проверьте адрес получения:\n\n"
        f"Криптовалюта: <b>{draft['currency']}</b>\n"
        f"Сеть: <b>{draft['network']}</b>\n"
        f"Адрес: <code>{wallet}</code>\n\n"
        f"Убедитесь, что адрес верен и принимает переводы в выбранной сети."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data["buy_last_state"] = BUY_WALLET_INPUT
    return BUY_WALLET_CONFIRM

async def buy_wallet_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    action = query.data.split(":")[1]
    
    if action == "change":
        draft = context.user_data["buy_draft"]
        await query.edit_message_text(
            f"Укажите адрес кошелька для получения <b>{draft['currency']}</b>.\n"
            f"Сеть: <b>{draft['network']}</b>.",
            parse_mode="HTML"
        )
        return BUY_WALLET_INPUT
        
    if action == "confirm":
        # Экран 8: Итоговая проверка и создание заявки
        draft = context.user_data["buy_draft"]
        keyboard = [
            [InlineKeyboardButton("Подтвердить и создать заявку", callback_data="buy_final:create")],
            [InlineKeyboardButton("В меню", callback_data="buy_menu")]
        ]
        
        text = (
            f"Проверьте данные перед созданием заявки:\n\n"
            f"Оплата: <b>{draft['pay_amount']:.2f} RUB</b>\n"
            f"Способ оплаты: <b>{draft['payment_method']}</b>\n"
            f"Получение: <b>{draft['receive_amount']:.6f} {draft['currency']}</b>\n"
            f"Сеть получения: <b>{draft['network']}</b>\n"
            f"Адрес получения: <code>{draft['wallet']}</code>\n\n"
            f"Подтвердите условия и создание заявки."
        )
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["buy_last_state"] = BUY_WALLET_CONFIRM
        return BUY_FINAL_CONFIRM

async def buy_final_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == "buy_menu":
        return await show_menu(update, context)
        
    draft = context.user_data["buy_draft"]
    user_id = update.effective_user.id
    
    # Создаем заявку в БД
    order_id = save_order(
        telegram_id=user_id,
        from_currency="RUB",
        to_currency=draft["currency"],
        amount=draft["pay_amount"],
        rate=draft["rate"],
        total=draft["receive_amount"],
        wallet_address=draft["wallet"],
        order_type="buy",
        network=draft["network"],
        payment_method=draft["payment_method"]
    )
    
    # Экран 9: Оплата заявки
    keyboard = [
        [InlineKeyboardButton("Я оплатил", callback_data=f"buy_paid:{order_id}")],
        [InlineKeyboardButton("В меню", callback_data="buy_menu")]
    ]
    
    await query.edit_message_text(
        f"Заявка <b>#{order_id}</b> создана. Статус: <b>{STATUS_PENDING}</b>.\n\n"
        f"К оплате: <b>{draft['pay_amount']:.2f} RUB</b>\n"
        f"Способ: {draft['payment_method']}\n"
        f"Реквизиты: <i>(здесь будут реквизиты, которые выдаст админ)</i>\n\n"
        f"После оплаты нажмите «Я оплатил».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    context.user_data.pop("buy_draft", None) # Очищаем черновик
    return BUY_PAYMENT_WAIT

def _sell_calculate(crypto: str, mode: str, amount: float) -> Tuple[float, float, float, float]:
    """Возвращает (crypto_amount, payout_rub, rate_rub, commission_percent)."""
    rates = get_effective_exchange_rates()
    settings = get_settings()
    commission = float(settings.get("commission_percent", 1.0))
    raw_rate = rates[crypto] / rates["RUB"]
    effective_rate = raw_rate * (1.0 - commission / 100.0)

    if mode == "crypto":
        crypto_amount = amount
        payout_rub = crypto_amount * effective_rate
    else:  # fiat (сумма к получению в RUB)
        payout_rub = amount
        crypto_amount = payout_rub / effective_rate

    return crypto_amount, payout_rub, effective_rate, commission


# --- ЭКРАН 1: Выбор криптовалюты ---
async def sell_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING

    if not ALLOWED_SELL_CRYPTO:
        await update.message.reply_text(
            "Сейчас нет доступных криптовалют для продажи.",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
        return CHOOSING

    context.user_data["sell_draft"] = {"step": "currency"}
    context.user_data.pop("sell_last_state", None)

    await update.message.reply_text(
        "Начинаем продажу криптовалюты...",
        reply_markup=buy_navigation_keyboard(),
    )

    keyboard = [
        [InlineKeyboardButton(c, callback_data=f"sell_cur:{c}")]
        for c in ALLOWED_SELL_CRYPTO
    ]
    keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])

    await update.message.reply_text(
        "Какую криптовалюту хотите продать? Выберите вариант из списка.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELL_CURRENCY


# --- ЭКРАН 2: Выбор сети отправки ---
async def sell_currency_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_CURRENCY
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    crypto = query.data.split(":", 1)[1]
    draft = context.user_data.setdefault("sell_draft", {})
    draft["currency"] = crypto

    networks = CRYPTO_NETWORKS.get(crypto, [])
    if not networks:
        await query.edit_message_text(
            f"Сетей для продажи <b>{crypto}</b> сейчас нет. Выберите другую криптовалюту.",
            parse_mode="HTML",
        )
        return SELL_CURRENCY

    if len(networks) == 1:
        draft["network"] = networks[0]
        context.user_data["sell_last_state"] = SELL_CURRENCY
        return await sell_show_payment_methods(update, context, query)

    keyboard = [
        [InlineKeyboardButton(net, callback_data=f"sell_net:{net}")]
        for net in networks
    ]
    keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])

    await query.edit_message_text(
        f"Вы выбрали: <b>{crypto}</b>.\n"
        f"Выберите сеть, по которой будете отправлять криптовалюту. Она должна совпадать с сетью кошелька.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data["sell_last_state"] = SELL_CURRENCY
    return SELL_NETWORK


async def sell_network_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_NETWORK
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    network = query.data.split(":", 1)[1]
    context.user_data.setdefault("sell_draft", {})["network"] = network
    context.user_data["sell_last_state"] = SELL_NETWORK
    return await sell_show_payment_methods(update, context, query)


# --- ЭКРАН 3: Выбор способа получения денег ---
async def sell_show_payment_methods(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query=None
) -> int:
    draft = context.user_data.get("sell_draft", {})
    keyboard = [
        [InlineKeyboardButton(SELL_METHOD_CARD, callback_data="sell_pay:card")],
        [InlineKeyboardButton(SELL_METHOD_QR, callback_data="sell_pay:qr")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]
    text = (
        f"Вы продаёте: <b>{draft.get('currency')}</b>.\n"
        f"Сеть отправки: <b>{draft.get('network')}</b>.\n"
        f"Выберите, как получить деньги."
    )
    if query:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELL_PAYMENT


async def sell_payment_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_PAYMENT
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    code = query.data.split(":", 1)[1]
    method = SELL_METHOD_CARD if code == "card" else SELL_METHOD_QR
    draft = context.user_data.setdefault("sell_draft", {})
    draft["payment_code"] = code
    draft["payment_method"] = method

    # Если способ изменили после расчёта — пересчитываем с сохранением суммы
    if "raw_input_amount" in draft and "amount_mode" in draft:
        try:
            c_amt, p_rub, rate, comm = _sell_calculate(
                draft["currency"], draft["amount_mode"], draft["raw_input_amount"]
            )
            draft.update(
                crypto_amount=c_amt,
                payout_amount=p_rub,
                rate=rate,
                commission=comm,
            )
            return await sell_show_rate_confirm(update, context, query)
        except Exception:
            pass

    return await sell_show_amount_mode(update, context, query)


# --- ЭКРАН 4: Ввод суммы продажи ---
async def sell_show_amount_mode(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query=None
) -> int:
    draft = context.user_data.get("sell_draft", {})
    keyboard = [
        [InlineKeyboardButton("Сумму криптовалюты", callback_data="sell_mode:crypto")],
        [InlineKeyboardButton("Сумму к получению", callback_data="sell_mode:fiat")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]
    text = (
        f"Вы продаёте: <b>{draft.get('currency')}</b>. "
        f"Сеть: <b>{draft.get('network')}</b>. "
        f"Получение: <b>{draft.get('payment_method')}</b>.\n\n"
        f"Какую сумму хотите указать?"
    )
    context.user_data["sell_last_state"] = SELL_PAYMENT
    if query:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELL_AMOUNT_MODE


async def sell_amount_mode_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_AMOUNT_MODE
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    mode = query.data.split(":", 1)[1]
    draft = context.user_data.setdefault("sell_draft", {})
    draft["amount_mode"] = mode

    prompt = (
        f"Введите сумму {draft['currency']}, которую хотите продать:"
        if mode == "crypto"
        else "Введите желаемую сумму выплаты в рублях (RUB):"
    )
    await query.edit_message_text(prompt)
    context.user_data["sell_last_state"] = SELL_AMOUNT_MODE
    return SELL_AMOUNT_INPUT


async def sell_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return SELL_AMOUNT_INPUT

    text = update.message.text.strip().replace(",", ".")
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Некорректная сумма. Введите положительное число (например, 0.05 или 15000)."
        )
        return SELL_AMOUNT_INPUT

    draft = context.user_data.get("sell_draft", {})
    crypto = draft.get("currency")
    mode = draft.get("amount_mode", "crypto")

    try:
        crypto_amount, payout_rub, rate, comm = _sell_calculate(crypto, mode, amount)
    except Exception:
        await update.message.reply_text(
            "Не удалось выполнить расчёт по текущему курсу. Попробуйте позже."
        )
        return SELL_AMOUNT_INPUT

    settings = get_settings()
    if payout_rub < float(settings.get("min_amount_withdraw_rub", 500.0)):
        await update.message.reply_text(
            f"Сумма выплаты ({payout_rub:.2f} RUB) меньше минимальной "
            f"({settings.get('min_amount_withdraw_rub', 500.0)} RUB). Укажите большую сумму."
        )
        return SELL_AMOUNT_INPUT

    draft["raw_input_amount"] = amount
    draft["crypto_amount"] = crypto_amount
    draft["payout_amount"] = payout_rub
    draft["rate"] = rate
    draft["commission"] = comm

    context.user_data["sell_last_state"] = SELL_AMOUNT_INPUT
    return await sell_show_rate_confirm(update, context)

async def sell_show_rate_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query=None
) -> int:
    draft = context.user_data.get("sell_draft", {})
    crypto = draft.get("currency", "")
    network = draft.get("network", "")
    method = draft.get("payment_method", "")
    crypto_amount = float(draft.get("crypto_amount", 0.0))
    payout_amount = float(draft.get("payout_amount", 0.0))
    rate = float(draft.get("rate", 0.0))
    commission = float(draft.get("commission", 0.0))

    keyboard = [
        [InlineKeyboardButton("Подтвердить расчёт", callback_data="sell_rate:confirm")],
        [InlineKeyboardButton("Изменить сумму", callback_data="sell_rate:change_amt")],
        [InlineKeyboardButton("Изменить способ получения", callback_data="sell_rate:change_pay")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]
    text = (
        f"Проверьте условия продажи:\n\n"
        f"Отправляете: <b>{crypto_amount:.6f} {crypto}</b>\n"
        f"Сеть отправки: <b>{network}</b>\n"
        f"Получение: <b>{method}</b>\n"
        f"Курс и комиссии: <b>1 {crypto} = {rate:.2f} RUB</b> (комиссия {commission:g}%)\n"
        f"К выплате: <b>{payout_amount:.2f} RUB</b>\n\n"
        f"Подтвердите расчёт, чтобы продолжить."
    )
    if query:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELL_RATE_CONFIRM

async def sell_rate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_RATE_CONFIRM
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    action = query.data.split(":", 1)[1]
    draft = context.user_data.get("sell_draft", {})

    if action == "change_amt":
        return await sell_show_amount_mode(update, context, query)

    if action == "change_pay":
        return await sell_show_payment_methods(update, context, query)

    if action == "confirm":
        # По ТЗ: если выбраны наличные по QR-коду, ввод реквизитов карты пропускается -> Экран 8
        if draft.get("payment_code") == "qr":
            context.user_data["sell_last_state"] = SELL_RATE_CONFIRM
            return await sell_show_final_confirm(update, context, query)

        # Иначе Экран 6: ввод реквизитов карты
        await query.edit_message_text(
            "Укажите реквизиты карты, на которую хотите получить выплату (номер карты или номер телефона СБП и банк):"
        )
        context.user_data["sell_last_state"] = SELL_RATE_CONFIRM
        return SELL_CARD_INPUT

    return SELL_RATE_CONFIRM

async def sell_card_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return SELL_CARD_INPUT

    card_details = update.message.text.strip()
    digits = re.sub(r"\D", "", card_details)
    if len(digits) < 10 or len(card_details) > 120:
        await update.message.reply_text(
            "Формат реквизитов не распознан. Укажите корректный номер карты (16 цифр) или телефон СБП и банк:"
        )
        return SELL_CARD_INPUT

    context.user_data.setdefault("sell_draft", {})["card_details"] = card_details
    context.user_data["sell_last_state"] = SELL_CARD_INPUT

    # --- ЭКРАН 7: Подтверждение реквизитов ---
    keyboard = [
        [InlineKeyboardButton("Подтвердить реквизиты", callback_data="sell_card:confirm")],
        [InlineKeyboardButton("Изменить реквизиты", callback_data="sell_card:change")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]
    await update.message.reply_text(
        f"Проверьте реквизиты для выплаты:\n"
        f"<code>{card_details}</code>\n\n"
        f"Подтвердите, что данные указаны верно.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELL_CARD_CONFIRM


# --- ЭКРАН 7: Подтверждение реквизитов ---
async def sell_card_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_CARD_CONFIRM
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    action = query.data.split(":", 1)[1]
    if action == "change":
        await query.edit_message_text(
            "Укажите реквизиты карты, на которую хотите получить выплату:"
        )
        return SELL_CARD_INPUT

    if action == "confirm":
        context.user_data["sell_last_state"] = SELL_CARD_CONFIRM
        return await sell_show_final_confirm(update, context, query)

    return SELL_CARD_CONFIRM


# --- ЭКРАН 8: Итоговая проверка и создание заявки ---
async def sell_show_final_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query=None
) -> int:
    draft = context.user_data.get("sell_draft", {})
    crypto = draft["currency"]
    is_card = draft.get("payment_code") == "card"

    keyboard = [
        [InlineKeyboardButton("Подтвердить условия и создать заявку", callback_data="sell_final:create")],
        [InlineKeyboardButton("Изменить сумму", callback_data="sell_final:change_amt")],
        [InlineKeyboardButton("Изменить способ получения", callback_data="sell_final:change_pay")],
    ]
    if is_card:
        keyboard.append(
            [InlineKeyboardButton("Изменить реквизиты карты", callback_data="sell_final:change_card")]
        )
    keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])

    card_line = (
        f"Реквизиты карты: <code>{draft.get('card_details')}</code>\n"
        if is_card
        else ""
    )

    text = (
        f"Проверьте данные перед созданием заявки:\n\n"
        f"Отправляете: <b>{draft['crypto_amount']:.6f} {crypto}</b>. Сеть: <b>{draft['network']}</b>.\n"
        f"Получение: <b>{draft['payment_method']}</b>.\n"
        f"Курс и комиссии: <b>1 {crypto} = {draft['rate']:.2f} RUB</b> (комиссия {draft['commission']:g}%).\n"
        f"К выплате: <b>{draft['payout_amount']:.2f} RUB</b>.\n"
        f"{card_line}\n"
        f"Подтвердите условия и создание заявки."
    )
    if query:
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return SELL_FINAL_CONFIRM


async def sell_final_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_FINAL_CONFIRM
    await query.answer()

    if query.data == "sell_menu":
        context.user_data.pop("sell_draft", None)
        return await show_menu(update, context)

    action = query.data.split(":", 1)[1]
    if action == "change_amt":
        return await sell_show_amount_mode(update, context, query)
    if action == "change_pay":
        return await sell_show_payment_methods(update, context, query)
    if action == "change_card":
        await query.edit_message_text("Укажите новые реквизиты карты для выплаты:")
        return SELL_CARD_INPUT

    if action != "create":
        return SELL_FINAL_CONFIRM

    draft = context.user_data.get("sell_draft")
    if not draft or not update.effective_user:
        await query.edit_message_text("Сессия истекла. Начните продажу заново.")
        return CHOOSING

    network = draft["network"]
    deposit_info = ASTRAL_DEPOSIT_ADDRESSES.get(network)
    if not deposit_info or not deposit_info[0]:
        await query.edit_message_text(
            "Ошибка: адрес приёма для выбранной сети временно не выдан. Заявка не создана."
        )
        return SELL_FINAL_CONFIRM

    deposit_address, deposit_memo = deposit_info
    user_id = update.effective_user.id
    wallet_field = (
        draft["card_details"]
        if draft.get("payment_code") == "card"
        else "QR-код в банкомате"
    )

    order_id = save_order(
        telegram_id=user_id,
        from_currency=draft["currency"],
        to_currency="RUB",
        amount=draft["crypto_amount"],
        rate=draft["rate"],
        total=draft["payout_amount"],
        wallet_address=wallet_field,
        order_type="sell",
        network=network,
        payment_method=draft["payment_method"],
    )
    set_order_expiry(order_id, minutes=60)

    # --- ЭКРАН 9: Реквизиты для отправки криптовалюты ---
    memo_line = f"Тег или Memo: <code>{deposit_memo}</code>\n" if deposit_memo else ""
    keyboard = [
        [InlineKeyboardButton("Я отправил криптовалюту", callback_data=f"sell_sent:{order_id}")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]

    await query.edit_message_text(
        f"Заявка <b>#{order_id}</b> создана. Отправьте криптовалюту по указанным реквизитам.\n\n"
        f"К отправке: <b>{draft['crypto_amount']:.6f} {draft['currency']}</b>. Сеть: <b>{network}</b>.\n"
        f"Адрес: <code>{deposit_address}</code>\n"
        f"{memo_line}"
        f"Срок отправки: <b>60 минут</b> после выдачи адреса.\n"
        f"Перевод, отправленный в срок, принимается, даже если подтверждение сети завершится позже.\n\n"
        f"После перевода нажмите «Я отправил криптовалюту».",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

    context.user_data.pop("sell_draft", None)
    context.user_data["sell_active_order_id"] = order_id
    return SELL_DEPOSIT_WAIT


# --- ЭКРАН 10: Проверка поступления криптовалюты и TX Hash ---
async def sell_sent_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_DEPOSIT_WAIT
    await query.answer()

    if query.data == "sell_menu":
        return await show_menu(update, context)

    order_id = int(query.data.split(":", 1)[1])
    if is_sell_order_expired(order_id):
        await query.edit_message_text(
            f"Срок отправки (60 минут) по заявке <b>#{order_id}</b> истёк. Статус заявки: <b>{STATUS_EXPIRED}</b>.",
            parse_mode="HTML",
        )
        return CHOOSING

    row = get_order_by_id(order_id)
    if not row:
        await query.edit_message_text("Заявка не найдена.")
        return CHOOSING

    # row: (0:id, 1:telegram_id, 2:from_currency, 3:to_currency, 4:amount, 5:rate, 6:total, 7:wallet_address, ...)
    update_order_status(order_id, STATUS_SELL_CHECKING)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT network, payment_method FROM orders WHERE id = ?", (order_id,))
        extra = cur.fetchone() or ("—", "—")

    is_qr = (extra[1] == SELL_METHOD_QR)
    for aid in _all_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=(
                    f"📥 Клиент нажал «Я отправил криптовалюту» (Продажа #{order_id})\n"
                    f"User: {row[1]}\n"
                    f"Отправляет: {row[4]:.6f} {row[2]} (Сеть: {extra[0]})\n"
                    f"К выплате: {row[6]:.2f} RUB ({extra[1]})\n"
                    f"Реквизиты: {row[7]}\n"
                    f"Статус: {STATUS_SELL_CHECKING}"
                ),
                reply_markup=status_keyboard_sell(order_id, is_qr=is_qr),
            )
        except TelegramError:
            pass

    keyboard = [
        [InlineKeyboardButton("Отправить TX Hash", callback_data=f"sell_txhash:{order_id}")],
        [InlineKeyboardButton("Мои заявки", callback_data="sell_my_orders")],
        [InlineKeyboardButton("В меню", callback_data="sell_menu")],
    ]

    await query.edit_message_text(
        f"Информация об отправке по заявке <b>#{order_id}</b> получена.\n"
        f"Проверяем поступление <b>{row[4]:.6f} {row[2]}</b> в сети <b>{extra[0]}</b>.\n"
        f"После подтверждения начнётся выплата выбранным способом.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return SELL_CHECKING_WAIT

async def sell_checking_actions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not query.data:
        return SELL_CHECKING_WAIT
    await query.answer()

    if query.data == "sell_menu":
        return await show_menu(update, context)

    if query.data == "sell_my_orders":
        uid = update.effective_user.id if update.effective_user else 0
        rows = get_user_orders(uid, limit=10)
        text = (
            "Здесь все ваши обмены:\n" + "\n".join(format_order_line_short(r) for r in rows)
            if rows
            else "Пока нет заявок."
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=main_menu_keyboard(uid),
        )
        return CHOOSING

    if query.data.startswith("sell_txhash:"):
        order_id = int(query.data.split(":", 1)[1])
        context.user_data["sell_txhash_order_id"] = order_id
        await query.edit_message_text(
            f"Отправьте хеш транзакции (TX Hash) по заявке <b>#{order_id}</b> одним сообщением:",
            parse_mode="HTML",
        )
        return SELL_TXHASH_INPUT

    return SELL_CHECKING_WAIT


async def sell_txhash_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return SELL_TXHASH_INPUT

    tx_hash = update.message.text.strip()
    if len(tx_hash) < 16 or " " in tx_hash:
        await update.message.reply_text(
            "Некорректный формат TX Hash. Вставьте хеш транзакции без пробелов:"
        )
        return SELL_TXHASH_INPUT

    order_id = context.user_data.pop("sell_txhash_order_id", None)
    if not order_id:
        await update.message.reply_text("Заявка не найдена. Вернитесь в меню.")
        return CHOOSING

    update_order_tx_hash(order_id, tx_hash)

    for aid in _all_admin_ids():
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=f"🔗 По заявке на продажу #{order_id} клиент прислал TX Hash:\n<code>{tx_hash}</code>",
                parse_mode="HTML",
            )
        except TelegramError:
            pass

    await update.message.reply_text(
        f"TX Hash по заявке <b>#{order_id}</b> принят и передан на проверку.\n"
        f"Мы уведомим вас сразу после подтверждения поступления.",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(update.effective_user.id),
    )
    return CHOOSING


async def sell_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING

    if update.message.text == BUTTON_BACK_TO_MENU:
        context.user_data.pop("sell_last_state", None)
        return await show_menu(update, context, preface="Продажа приостановлена.\n")

    last_state = context.user_data.get("sell_last_state")
    draft = context.user_data.get("sell_draft", {})

    if last_state is None:
        return await show_menu(update, context)

    if last_state == SELL_CURRENCY:
        return await sell_start(update, context)

    if last_state == SELL_NETWORK:
        crypto = draft.get("currency")
        networks = CRYPTO_NETWORKS.get(crypto, [])
        if len(networks) <= 1:
            return await sell_start(update, context)
        keyboard = [[InlineKeyboardButton(net, callback_data=f"sell_net:{net}")] for net in networks]
        keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])
        await update.message.reply_text(
            f"Вы выбрали: <b>{crypto}</b>.\nВыберите сеть отправки:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        context.user_data["sell_last_state"] = SELL_CURRENCY
        return SELL_NETWORK

    if last_state == SELL_PAYMENT:
        context.user_data["sell_last_state"] = SELL_NETWORK
        return await sell_show_payment_methods(update, context)

    if last_state == SELL_AMOUNT_MODE:
        return await sell_show_amount_mode(update, context)

    if last_state == SELL_AMOUNT_INPUT:
        prompt = (
            f"Введите сумму {draft.get('currency')}, которую хотите продать:"
            if draft.get("amount_mode") == "crypto"
            else "Введите желаемую сумму выплаты в рублях (RUB):"
        )
        await update.message.reply_text(prompt)
        context.user_data["sell_last_state"] = SELL_AMOUNT_MODE
        return SELL_AMOUNT_INPUT

    if last_state == SELL_RATE_CONFIRM:
        context.user_data["sell_last_state"] = SELL_AMOUNT_INPUT
        return await sell_show_rate_confirm(update, context)

    if last_state == SELL_CARD_INPUT:
        await update.message.reply_text("Укажите реквизиты карты для выплаты:")
        context.user_data["sell_last_state"] = SELL_RATE_CONFIRM
        return SELL_CARD_INPUT

    return await show_menu(update, context)

async def reply_blocked(update: Update) -> None:
    if update.message:
        await update.message.reply_text(
            "Ваш аккаунт заблокирован. Для уточнений свяжитесь с поддержкой.",
            reply_markup=blocked_user_keyboard(),
        )


async def ensure_not_blocked(update: Update) -> bool:
    if not update.effective_user:
        return False
    if is_user_blocked(update.effective_user.id):
        await reply_blocked(update)
        return False
    return True


def format_order_line_short(row: Tuple) -> str:
    oid, from_c, to_c, amount, rate, total, _w, status, created_at = row
    return (
        f"\n#{oid} | {status}\n"
        f"Отдаю: {amount} {from_c} → Получаю: {total:.8f} {to_c}\n"
        f"Курс: {rate:.8f}\n"
        f"Создана: {created_at}"
    )


def status_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(STATUS_PENDING, callback_data=f"a:{order_id}:P"),
                InlineKeyboardButton(STATUS_REVIEW, callback_data=f"a:{order_id}:R"),
            ],
            [
                InlineKeyboardButton(STATUS_DONE, callback_data=f"a:{order_id}:D"),
                InlineKeyboardButton(STATUS_CANCELLED, callback_data=f"a:{order_id}:C"),
            ],
        ]
    )


def ticket_invite_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✍️ Написать пользователю", callback_data=f"ticket:{user_id}")]]
    )


# --- Тикеты: уведомления ---


async def notify_admins_about_clean_request(
    context: ContextTypes.DEFAULT_TYPE, user, source: str = "через бота."
) -> bool:
    admins = _all_admin_ids()
    if not admins:
        logging.warning("Нет админов для уведомления о Чистке")
        return False

    name = (getattr(user, "full_name", None) or "").strip() or "—"
    username = f"@{user.username}" if getattr(user, "username", None) else "—"
    text = (
        f"🧹 Запрос на «Чистку» {source}\n"
        f"Пользователь: {name} ({username})\n"
        f"ID: {user.id}\n\n"
        "Нажмите кнопку ниже, чтобы начать переписку."
    )
    kb = ticket_invite_keyboard(user.id)
    sent = 0
    for aid in admins:
        try:
            await context.bot.send_message(chat_id=aid, text=text, reply_markup=kb)
            sent += 1
        except TelegramError as exc:
            logging.warning("Не удалось уведомить админа %s: %s", aid, exc)
    logging.info("notify_admins_about_clean_request: доставлено %s/%s", sent, len(admins))
    return sent > 0


async def notify_admins_user_message(
    context: ContextTypes.DEFAULT_TYPE, user, text: str
) -> bool:
    ticket = get_ticket(user.id)
    if not ticket:
        return False
    admin_id = ticket[1]
    name = (getattr(user, "full_name", None) or "").strip() or "—"
    username = f"@{user.username}" if getattr(user, "username", None) else "—"
    body = (
        f"📨 Ответ от пользователя\n"
        f"Имя: {name} ({username})\n"
        f"ID: {user.id}\n\n{text}"
    )
    try:
        await context.bot.send_message(
            chat_id=admin_id,
            text=body,
            reply_markup=exit_dialog_keyboard(),
        )
        return True
    except TelegramError as exc:
        logging.warning("Не удалось доставить админу %s: %s", admin_id, exc)
        return False

async def notify_admins_user_media(
    context: ContextTypes.DEFAULT_TYPE, update: Update
) -> bool:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return False

    ticket = get_ticket(user.id)
    if not ticket:
        return False

    admin_id = ticket[1]
    name = (getattr(user, "full_name", None) or "").strip() or "—"
    username = f"@{user.username}" if getattr(user, "username", None) else "—"
    caption_extra = f"\nКомментарий: {msg.caption}" if msg.caption else ""
    header = (
        f"📲 Медиафайл (QR-код / скриншот) от пользователя\n"
        f"Имя: {name} ({username})\n"
        f"ID: {user.id}{caption_extra}"
    )

    try:
        if msg.photo:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=msg.photo[-1].file_id,
                caption=header,
                reply_markup=exit_dialog_keyboard(),
            )
            save_chat_message(user.id, "user", f"[Фото QR-кода] {msg.caption or ''}".strip())
            return True
        if msg.document:
            await context.bot.send_document(
                chat_id=admin_id,
                document=msg.document.file_id,
                caption=header,
                reply_markup=exit_dialog_keyboard(),
            )
            save_chat_message(user.id, "user", f"[Документ QR-кода] {msg.caption or ''}".strip())
            return True
    except TelegramError as exc:
        logging.warning("Не удалось переслать медиафайл админу %s: %s", admin_id, exc)
    return False

# --- Пользовательские хендлеры ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END

    sync_admin_roles()
    user_id = update.effective_user.id
    is_new = register_user(user_id)

    if is_user_blocked(user_id):
        await update.message.reply_text(
            "Ваш аккаунт заблокирован."
            if not is_new
            else "Регистрация невозможна: аккаунт заблокирован.",
            reply_markup=blocked_user_keyboard(),
        )
        return BLOCKED_USER

    miniapp_url = get_miniapp_url()
    welcome_text = (
        "Привет! Это <b>Astral</b>.\n"
        "Здесь можно рассчитать обмен, посмотреть свои заявки или написать нам."
    )

    if miniapp_url:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Открыть Mini App", web_app=WebAppInfo(url=miniapp_url))]
        ])
    else:
        keyboard = None

    await update.message.reply_text(
        welcome_text,
        parse_mode="HTML",
        reply_markup=keyboard
    )
    
    inline_kb = get_context_inline_keyboard(user_id, context)
    menu_text = "👇 Выберите действие:"
    
    if inline_kb:
        menu_msg = await update.message.reply_text(
            menu_text,
            reply_markup=inline_kb
        )
        await update.message.reply_text(
            "Основное меню:",
            reply_markup=main_menu_keyboard(user_id)
        )
    else:
        menu_msg = await update.message.reply_text(
            menu_text,
            reply_markup=main_menu_keyboard(user_id)
        )
    
    context.user_data["last_menu_msg_id"] = menu_msg.message_id
    return CHOOSING

async def show_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    preface: str = "",
) -> int:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return CHOOSING

    if is_user_blocked(user.id):
        if update.message:
            await reply_blocked(update)
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text="Ваш аккаунт заблокирован. Для уточнений свяжитесь с поддержкой.",
                reply_markup=blocked_user_keyboard(),
            )
        return BLOCKED_USER

    uid = user.id
    menu_text = f"{preface}👇 Выберите действие:"
    inline_kb = get_context_inline_keyboard(uid, context)

    # Если вызов пришёл с inline-кнопки (callback_query), закрываем "часики" и убираем старую клавиатуру
    if update.callback_query:
        try:
            await update.callback_query.answer()
            await update.callback_query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass

    if inline_kb:
        menu_msg = await context.bot.send_message(
            chat_id=chat.id,
            text=menu_text,
            reply_markup=inline_kb,
        )
        await context.bot.send_message(
            chat_id=chat.id,
            text="Основное меню:",
            reply_markup=main_menu_keyboard(uid),
        )
    else:
        menu_msg = await context.bot.send_message(
            chat_id=chat.id,
            text=menu_text,
            reply_markup=main_menu_keyboard(uid),
        )

    context.user_data["last_menu_msg_id"] = menu_msg.message_id
    return CHOOSING


async def rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.message:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    try:
        eff = get_effective_exchange_rates()
        raw = get_exchange_rates()
        uid = update.effective_user.id if update.effective_user else 0
        if is_admin(uid):
            lines = ["Курсы (USD за 1 ед.) с учётом множителей администратора:"]
        else:
            lines = ["Актуальные курсы обмена (USD за 1 ед.):"]
        mults = {c: eff[c] / raw[c] if raw[c] else 1.0 for c in eff}
        for c, v in sorted(eff.items()):
            m = mults.get(c, 1.0)
            if is_admin(uid):
                extra = f" (×{m:.4g} к бирже)" if abs(m - 1.0) > 1e-9 else ""
            else:
                extra = ""
            lines.append(f"{c}: ${v:,.6f}{extra}")
    except (requests.RequestException, ValueError) as exc:
        logging.exception("Ошибка получения курсов: %s", exc)
        await update.message.reply_text("Курс сейчас недоступен. Попробуйте позже.")
        return CHOOSING

    await update.message.reply_text("\n".join(lines))
    return CHOOSING


async def exchange_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_exchange_draft(context)
    context.user_data["exchange_mode"] = EXCHANGE_MODE_SWAP
    return await exchange(update, context)


async def exchange(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    context.user_data.setdefault("exchange_mode", EXCHANGE_MODE_SWAP)
    mode = _exchange_mode_from_context(context)
    first = (
        prompt_buy_want_currency()
        if mode == EXCHANGE_MODE_BUY
        else prompt_exchange_first_currency(mode)
    )
    
    context.user_data["exchange_step"] = "first"
    
    # 👇 Сначала убираем основные кнопки, показывая только "Назад" и "Отмена"
    await update.message.reply_text(
        "Начинаем обмен. Выберите валюту:",
        reply_markup=exchange_step_keyboard()
    )
    
    # 👇 Затем отправляем сообщение с inline-клавиатурой
    await update.message.reply_text(
        first,
        reply_markup=currency_inline_keyboard(show_others=False)
    )
    return FROM_CURRENCY


async def handle_clean_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_exchange_draft(context)
    if not update.effective_user or not update.message:
        return CHOOSING
    ok = await notify_admins_about_clean_request(context, update.effective_user)
    if ok:
        await update.message.reply_text(
            "Запрос в поддержку отправлен — с вами свяжется администратор."
        )
    else:
        await update.message.reply_text(
            "К сожалению, операторы сейчас недоступны, попробуйте позже."
        )
    return CHOOSING


async def blocked_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return BLOCKED_USER
    text = (update.message.text or "").strip()
    if text == BUTTON_SUPPORT:
        return await support(update, context)
    if text == BUTTON_CLEAN:
        await handle_clean_request(update, context)
        return BLOCKED_USER
    if update.effective_user and get_ticket(update.effective_user.id):
        sent = await notify_admins_user_message(context, update.effective_user, text)
        if sent:
            await update.message.reply_text("Сообщение передано в поддержку.")
            return BLOCKED_USER
    await reply_blocked(update)
    return BLOCKED_USER

async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return CHOOSING
    if update.effective_user and is_user_blocked(update.effective_user.id):
        await reply_blocked(update)
        return BLOCKED_USER

    text = update.message.text.strip()

    if text == BUTTON_BACK_TO_MAIN:
        # Принудительно отправляем главное меню
        await update.message.reply_text(
            "👇 Основное меню:",
            reply_markup=main_menu_keyboard(update.effective_user.id)
        )
        return CHOOSING
    
    if (context.user_data.get("from_currency") and 
        context.user_data.get("to_currency") and 
        "amount" not in context.user_data and
        text not in (BUTTON_BACK, BUTTON_BACK_TO_MENU, BUTTON_CANCEL)):
        try:
            amount = float(text.replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Введите корректную положительную сумму (например, 0.5).")
            return CHOOSING
            
        from_currency = context.user_data["from_currency"]
        to_currency = context.user_data["to_currency"]
        
        try:
            rates_ref = get_effective_exchange_rates()
            rate = rates_ref[from_currency] / rates_ref[to_currency]
            total = amount * rate
        except Exception as exc:
            await update.message.reply_text("Не удалось получить курс. Попробуйте позже.")
            return CHOOSING
            
        context.user_data["amount"] = amount
        context.user_data["rate"] = rate
        context.user_data["total"] = total
        
        mode = _exchange_mode_from_context(context)
        context.user_data["exchange_step"] = "wallet"
        await update.message.reply_text(
            f"Курс: 1 {from_currency} = {rate:.8f} {to_currency}\n"
            f"Итоговая сумма: {total:.8f} {to_currency}\n"
            f"{prompt_exchange_wallet(mode, to_currency)}",
            reply_markup=exchange_step_keyboard(),
        )
        return WALLET

    if text in (BUTTON_BACK, BUTTON_BACK_TO_MENU) or text == "🏠 Главное меню":
        return await show_menu(update, context)
    
    # 👇 Реальные сценарии для кнопок
        # --- Главное меню по ТЗ Часть 1 ---

    if text == BUTTON_BUY:
        return await buy_start(update, context)

    if text == BUTTON_SELL:
        return await sell_start(update, context)

    if text == BUTTON_SWAP:
        _clear_exchange_draft(context)
        context.user_data["exchange_mode"] = EXCHANGE_MODE_SWAP
        return await handle_clean_request(update, context)

    if text == BUTTON_AML:
        await update.message.reply_text("🛡 Сценарий проверки AML находится в разработке (согласно ТЗ).")
        return CHOOSING

    if text == BUTTON_WALLET:
        return await my_wallet(update, context)

    if text == BUTTON_CABINET:
        return await my_cabinet(update, context)

    if text == BUTTON_RATES:
        return await rates(update, context)

    if text == BUTTON_SUPPORT:
        return await support(update, context)

    if text == BUTTON_NEWS:
        return await news(update, context)

    if text == BUTTON_REVIEW:
        return await leave_review(update, context)

    if text == BUTTON_MORE:
        return await more_menu(update, context)

    # --- Меню "Ещё" ---

    if text == BUTTON_HOW_TO_EXCHANGE:
        await update.message.reply_text("📖 Инструкция по обмену находится в разработке.")
        return CHOOSING
    if text == BUTTON_ABOUT:
        await update.message.reply_text("ℹ️ Информация об Astral находится в разработке.")
        return CHOOSING
    if text == BUTTON_FAVORITES:
        await update.message.reply_text("⭐ Раздел «Избранное» находится в разработке.")
        return CHOOSING
    if text == BUTTON_MY_TICKETS:
        await update.message.reply_text("📨 Раздел «Мои обращения» находится в разработке.")
        return CHOOSING
    if text == BUTTON_NOTIFICATION_SETTINGS:
        await update.message.reply_text("🔔 Настройки уведомлений находятся в разработке.")
        return CHOOSING
    if text == BUTTON_LANGUAGE:
        await update.message.reply_text("🌐 Выбор языка находится в разработке.")
        return CHOOSING
    if text == BUTTON_EXCHANGE_RULES:
        await update.message.reply_text("📜 Правила обмена находятся в разработке.")
        return CHOOSING
    if text == BUTTON_SHARE:
        share_url = "https://t.me/domyfavorandesarvarov_bot"
        await update.message.reply_text(f"Поделитесь ботом с друзьями:\n{share_url}")
        return CHOOSING
    if text == BUTTON_ADMIN_SUMMARY:
        if not update.effective_user or not is_admin(update.effective_user.id):
            await update.message.reply_text("Нет доступа.")
            return CHOOSING
        await update.message.reply_text(format_recent_orders_broadcast(20))
        return CHOOSING
    if text == BUTTON_CANCEL:
        return await cancel(update, context)
    if text == BUTTON_CLOSE_TICKET:
        return await close_ticket_cmd(update, context)

    if (
        update.effective_user
        and is_admin(update.effective_user.id)
        and text in _ADMIN_PANEL_REPLY_TEXTS
    ):
        return CHOOSING

    if update.effective_user:
        ticket = get_ticket(update.effective_user.id)
        if ticket:
            sent = await notify_admins_user_message(context, update.effective_user, text)
            if sent:
                await update.message.reply_text("Сообщение передано в поддержку.")
            else:
                await update.message.reply_text(
                    "Не удалось передать сообщение администратору. Попробуйте позже."
                )
            return USER_IN_SUPPORT

    await update.message.reply_text("Выберите действие из меню ниже или используйте команду.")
    return CHOOSING

async def handle_user_media_in_choosing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING
    if get_ticket(update.effective_user.id):
        sent = await notify_admins_user_media(context, update)
        if sent:
            await update.message.reply_text("QR-код / файл передан администратору.")
            return USER_IN_SUPPORT
    await update.message.reply_text("Выберите действие из меню ниже.")
    return CHOOSING

async def user_in_support_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return USER_IN_SUPPORT

    sent = await notify_admins_user_media(context, update)
    if sent:
        await update.message.reply_text("QR-код / файл передан администратору.")
    else:
        await update.message.reply_text(
            "Не удалось передать файл: обращение закрыто или оператор недоступен."
        )
    return USER_IN_SUPPORT

async def user_in_support_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return USER_IN_SUPPORT
    text = update.message.text.strip()

    if text == BUTTON_CLOSE_TICKET:
        return await close_ticket_cmd(update, context)
    if text == BUTTON_BACK_TO_MENU:
        return await show_menu(update, context)
    if text == BUTTON_CLEAN:
        return await handle_clean_request(update, context)
    if text == BUTTON_SUPPORT:
        return await support(update, context)

    sent = await notify_admins_user_message(context, update.effective_user, text)
    if sent:
        await update.message.reply_text("Сообщение передано в поддержку.")
    else:
        await update.message.reply_text(
            "Администратор закрыл обращение или недоступен. Нажмите «Чистка» для нового запроса."
        )
    return USER_IN_SUPPORT


async def close_ticket_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.message or not update.effective_user:
        return CHOOSING
    ticket = get_ticket(update.effective_user.id)
    if ticket:
        admin_id = ticket[1]
        close_ticket(update.effective_user.id)
        save_chat_message(update.effective_user.id, "system", "Клиент завершил диалог. Спасибо!")
        try:
            await update.get_bot().send_message(
                chat_id=admin_id,
                text=f"Пользователь {update.effective_user.id} завершил обращение.",
            )
        except TelegramError as exc:
            logging.warning("Не удалось уведомить админа %s о закрытии: %s", admin_id, exc)
        await update.message.reply_text(
            "Обращение закрыто. Спасибо!",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
    else:
        await update.message.reply_text(
            "У вас нет активного обращения.",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
    return CHOOSING


async def handle_exchange_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return FROM_CURRENCY
    if not await ensure_not_blocked(update):
        return CHOOSING
    text = update.message.text.strip()
    
    if text == BUTTON_CANCEL:
        context.user_data.clear()
        return await cancel(update, context)
    
    if text in (BUTTON_BACK, BUTTON_BACK_TO_MENU):
        context.user_data.clear()
        return await show_menu(update, context)

    ticker = text.upper()
    if ticker not in ALLOWED:
        await update.message.reply_text(f"Неверная валюта. Доступно: {allowed_text()}")
        return FROM_CURRENCY

    mode = _exchange_mode_from_context(context)
    context.user_data["exchange_step"] = "second"
    if mode == EXCHANGE_MODE_BUY:
        context.user_data["to_currency"] = ticker
        await update.message.reply_text(
            prompt_buy_pay_currency(), reply_markup=currency_inline_keyboard(show_others=False)
        )
        return TO_CURRENCY
    context.user_data["from_currency"] = ticker
    await update.message.reply_text(
        prompt_exchange_second_currency(mode), reply_markup=currency_inline_keyboard(show_others=False)
    )
    return TO_CURRENCY


async def handle_exchange_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return TO_CURRENCY
    if not await ensure_not_blocked(update):
        return CHOOSING
    text = update.message.text.strip()
    
    if text == BUTTON_CANCEL:
        context.user_data.clear()
        return await cancel(update, context)
    
    if text == BUTTON_BACK:
        context.user_data.pop("to_currency", None)
        context.user_data["exchange_step"] = "first"
        mode = _exchange_mode_from_context(context)
        back_prompt = (
            prompt_buy_want_currency()
            if mode == EXCHANGE_MODE_BUY
            else prompt_exchange_first_currency(mode)
        )
        await update.message.reply_text(back_prompt, reply_markup=currency_inline_keyboard(show_others=False))
        return FROM_CURRENCY
    if text == BUTTON_BACK_TO_MENU:
        context.user_data.clear()
        return await show_menu(update, context)

    ticker = text.upper()
    if ticker not in ALLOWED:
        await update.message.reply_text(f"Неверная валюта. Доступно: {allowed_text()}")
        return TO_CURRENCY

    mode = _exchange_mode_from_context(context)
    if mode == EXCHANGE_MODE_BUY:
        to_currency = context.user_data.get("to_currency")
        if not to_currency:
            await update.message.reply_text(
                f"Сессия сброшена. {session_reset_exchange_hint()}"
            )
            return CHOOSING
        if ticker == to_currency:
            await update.message.reply_text(
                "Валюта оплаты должна отличаться от покупаемой. Выберите другую валюту."
            )
            return TO_CURRENCY
        context.user_data["from_currency"] = ticker
        from_currency = ticker
    else:
        from_currency = context.user_data.get("from_currency")
        if not from_currency:
            await update.message.reply_text(
                f"Сессия сброшена. {session_reset_exchange_hint()}"
            )
            return CHOOSING
        to_currency = ticker
        if to_currency == from_currency:
            await update.message.reply_text("Валюты должны отличаться. Введите другую валюту.")
            return TO_CURRENCY
        context.user_data["to_currency"] = to_currency

    context.user_data["exchange_step"] = "amount"
    await update.message.reply_text(
        prompt_exchange_amount(mode, from_currency), reply_markup=exchange_step_keyboard()
    )
    return AMOUNT


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return AMOUNT
    if not await ensure_not_blocked(update):
        return CHOOSING
    text = update.message.text.strip()
    
    if text == BUTTON_CANCEL:
        context.user_data.clear()
        return await cancel(update, context)
    
    if text == BUTTON_BACK:
        context.user_data.pop("amount", None)
        context.user_data.pop("rate", None)
        context.user_data.pop("total", None)
        context.user_data["exchange_step"] = "second"
        mode = _exchange_mode_from_context(context)
        if mode == EXCHANGE_MODE_BUY:
            context.user_data.pop("from_currency", None)
            await update.message.reply_text(
                prompt_buy_pay_currency(), reply_markup=currency_inline_keyboard(show_others=False)
            )
        else:
            await update.message.reply_text(
                prompt_exchange_second_currency(mode), reply_markup=currency_inline_keyboard(show_others=False)
            )
        return TO_CURRENCY
    if text == BUTTON_BACK_TO_MENU:
        context.user_data.clear()
        return await show_menu(update, context)
    try:
        amount = float(text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Введите корректную положительную сумму (например, 0.5)."
        )
        return AMOUNT

    settings = get_settings()
    if amount < settings["min_amount_swap"]:
        await update.message.reply_text(
            f"Минимальная сумма для обмена: {settings['min_amount_swap']}."
        )
        return AMOUNT
    if amount > settings["max_amount_swap"]:
        await update.message.reply_text(
            f"Максимальная сумма для обмена: {settings['max_amount_swap']}."
        )
        return AMOUNT

    from_currency = context.user_data.get("from_currency")
    to_currency = context.user_data.get("to_currency")
    if not from_currency or not to_currency:
        await update.message.reply_text(
            f"Сессия сброшена. {session_reset_exchange_hint()}"
        )
        return CHOOSING

    try:
        rates_ref = get_effective_exchange_rates()
        rate = rates_ref[from_currency] / rates_ref[to_currency]
        total = amount * rate
    except (requests.RequestException, KeyError, ValueError) as exc:
        logging.exception("Ошибка расчета курса: %s", exc)
        await update.message.reply_text("Не удалось получить курс. Попробуйте позже.")
        return CHOOSING

    context.user_data["amount"] = amount
    context.user_data["rate"] = rate
    context.user_data["total"] = total

    mode = _exchange_mode_from_context(context)
    context.user_data["exchange_step"] = "wallet"
    await update.message.reply_text(
        f"Курс: 1 {from_currency} = {rate:.8f} {to_currency}\n"
        f"Итоговая сумма: {total:.8f} {to_currency}\n"
        f"{prompt_exchange_wallet(mode, to_currency)}",
        reply_markup=exchange_step_keyboard(),
    )
    return WALLET


async def finalize_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message or not update.message.text:
        return WALLET
    if not await ensure_not_blocked(update):
        return CHOOSING

    wallet_address = update.message.text.strip()
    
    if wallet_address == BUTTON_CANCEL:
        context.user_data.clear()
        return await cancel(update, context)
    
    if wallet_address == BUTTON_BACK:
        context.user_data.pop("amount", None)
        context.user_data.pop("rate", None)
        context.user_data.pop("total", None)
        context.user_data["exchange_step"] = "amount"
        mode = _exchange_mode_from_context(context)
        fc = context.user_data.get("from_currency")
        if fc:
            await update.message.reply_text(
                prompt_exchange_amount(mode, fc), reply_markup=exchange_step_keyboard()
            )
        else:
            await update.message.reply_text(
                f"Сессия сброшена. {session_reset_exchange_hint()}"
            )
        return AMOUNT
    if wallet_address == BUTTON_BACK_TO_MENU:
        context.user_data.clear()
        return await show_menu(update, context)
    if len(wallet_address) < 8:
        await update.message.reply_text(
            "Похоже, адрес слишком короткий. Введите корректный адрес."
        )
        return WALLET

    user_id = update.effective_user.id
    from_currency = context.user_data.get("from_currency")
    to_currency = context.user_data.get("to_currency")
    amount = context.user_data.get("amount")
    rate = context.user_data.get("rate")
    total = context.user_data.get("total")

    if None in (from_currency, to_currency, amount, rate, total):
        await update.message.reply_text(
            f"Сессия сброшена. {session_reset_exchange_hint()}"
        )
        context.user_data.clear()
        return CHOOSING

    mode = _exchange_mode_from_context(context)
    order_type = mode if mode in ("buy", "sell") else "swap"

    order_id = save_order(
        telegram_id=user_id,
        from_currency=from_currency,
        to_currency=to_currency,
        amount=amount,
        rate=rate,
        total=total,
        wallet_address=wallet_address,
        order_type=order_type,
    )

    main_menu_kb = ReplyKeyboardMarkup(
        [[KeyboardButton("🏠 Главное меню")]], 
        resize_keyboard=True, 
        one_time_keyboard=False
    )

    await update.message.reply_text(
        f"Заявка #{order_id} создана:\n"
        f"Отдаю: {amount} {from_currency}\n"
        f"Получаю: {total:.8f} {to_currency}\n"
        f"Курс: {rate:.8f}\n"
        f"Адрес получения: {wallet_address}\n"
        f"Статус: {STATUS_PENDING}\n"
        f"Проверить статус: /status {order_id} или пункт меню «Статус заявки»",
	reply_markup=main_menu_kb
    )

    context.user_data.clear()
    return CHOOSING


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.effective_user or not update.message:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    rows = get_user_orders(update.effective_user.id, limit=10)
    if not rows:
        await update.message.reply_text("Пока нет заявок.")
        return CHOOSING
    lines = ["Здесь все ваши обмены:"]
    for row in rows:
        lines.append(format_order_line_short(row))
    await update.message.reply_text("\n".join(lines))
    return CHOOSING


async def operation_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.effective_user or not update.message:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    rows = get_user_orders(update.effective_user.id, limit=30)
    if not rows:
        await update.message.reply_text("История пуста.")
        return CHOOSING
    lines = ["История операций (до 30 последних):"]
    for row in rows:
        lines.append(format_order_line_short(row))
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3900] + "\n… (обрежьте фильтром по номеру заявки: /status <id>)"
    await update.message.reply_text(text)
    return CHOOSING


async def order_status_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    parts = update.message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().isdigit():
        oid = int(parts[1].strip())
        await send_order_status_message(update, context, oid)
        return CHOOSING
    await update.message.reply_text(
        "Введите номер заявки (только число), например: 42",
        reply_markup=input_step_keyboard(),
    )
    return ORDER_STATUS_WAIT


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    args = context.args or []
    if not args:
        await update.message.reply_text("Использование: /status <номер_заявки>")
        return CHOOSING
    if not args[0].isdigit():
        await update.message.reply_text("Номер заявки должен быть числом.")
        return CHOOSING
    await send_order_status_message(update, context, int(args[0]))
    return CHOOSING


async def send_order_status_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: int
) -> None:
    del context
    is_sell_order_expired(order_id)
    row = get_order_by_id(order_id)
    uid = update.effective_user.id if update.effective_user else None

    if not row:
        msg = "Заявка не найдена."
    elif uid is not None and row[1] != uid and not is_admin(uid):
        msg = "Эта заявка принадлежит другому пользователю."
    else:
        _oid, tg_id, from_c, to_c, amount, rate, total, wallet, status, created_at, updated_at = row
        rem = get_order_remaining_minutes(_oid)
        timer_line = f"\n⏳ Осталось времени на отправку: {rem} мин." if rem is not None else ""
        msg = (
            f"Заявка #{_oid}\n"
            f"Статус: {status}{timer_line}\n"
            f"Отдаю: {amount} {from_c}\n"
            f"Получаю: {total:.8f} {to_c}\n"
            f"Курс: {rate:.8f}\n"
            f"Реквизиты: {wallet}\n"
            f"Создана: {created_at}\n"
            f"Обновлена: {updated_at or '—'}"
        )

    if update.message:
        await update.message.reply_text(msg)
    elif update.callback_query and update.effective_chat:
        await update.callback_query.edit_message_text(msg)


async def handle_order_status_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return ORDER_STATUS_WAIT
    if not await ensure_not_blocked(update):
        return CHOOSING
    text = update.message.text.strip()
    if text in (BUTTON_BACK, BUTTON_BACK_TO_MENU):
        return await show_menu(update, context)
    if not text.isdigit():
        await update.message.reply_text("Введите номер заявки числом или вернитесь назад.")
        return ORDER_STATUS_WAIT
    await send_order_status_message(update, context, int(text))
    return CHOOSING


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await operation_history(update, context)


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.message:
        return CHOOSING
    settings = get_settings()
    await update.message.reply_text(f"Свяжитесь с поддержкой: {settings['support_contact']}")
    uid = update.effective_user.id if update.effective_user else None
    if uid is not None and is_user_blocked(uid):
        return BLOCKED_USER
    return CHOOSING

async def my_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING
    await update.message.reply_text(
        "👛 <b>Ваш кошелёк</b>\n\n"
        "Баланс: 0.00000000 BTC\n"
        "Баланс: 0.00000000 LTC\n"
        "Баланс: 0.00000000 USDT TRC\n\n"
        "Примерно: 0 RUB\n\n"
        "⚙️ Раздел находится в разработке. Пополнение, вывод и обмен будут доступны позже.",
        parse_mode="HTML"
    )
    return CHOOSING


async def my_cabinet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.effective_user:
        return CHOOSING

    user_id = update.effective_user.id
    user_orders = get_user_orders(user_id, limit=100)
    completed_orders = len([r for r in user_orders if r[7] == STATUS_DONE])

    text = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"📊 Статус: <b>Новичок</b>\n"
        f"✅ Успешных сделок: <b>{completed_orders}</b>\n"
        f"👥 Приглашено друзей: <b>0</b>\n"
        f"💰 Баланс реферальных: <b>0 RUB</b>\n\n"
        f"🔗 Ссылка для приглашения: (будет сгенерирована)\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=cabinet_keyboard())
    return CHOOSING

async def cabinet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    if not query:
        return None
    await query.answer()
    
    data = query.data
    user_id = update.effective_user.id
    
    if data == "cab:orders":
        rows = get_user_orders(user_id, limit=10)
        if not rows:
            text = "Пока нет заявок."
        else:
            lines = ["Здесь все ваши обмены:"]
            for row in rows:
                lines.append(format_order_line_short(row))
            text = "\n".join(lines)
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text
        )
        return CHOOSING
        
    elif data == "cab:invite":
        await query.edit_message_text("✉️ Функция «Отправить приглашения» находится в разработке.")
        return CHOOSING
        
    elif data == "cab:withdraw":
        await query.edit_message_text("💸 Функция «Вывод средств» находится в разработке.")
        return CHOOSING
        
    elif data == "cab:back":
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="👇 Основное меню:",
            reply_markup=main_menu_keyboard(user_id)
        )
        return CHOOSING

async def news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return CHOOSING
    await update.message.reply_text(
        "📰 <b>Новости Astral</b>\n\n"
        "Ссылка на официальную группу пока не настроена. Следите за обновлениями в боте!"
    )
    return CHOOSING


async def leave_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return CHOOSING
    await update.message.reply_text(
        "✍️ <b>Оставить отзыв</b>\n\n"
        "Напишите ваш отзыв одним сообщением, и мы передадим его администратору."
    )
    return CHOOSING


async def more_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return CHOOSING
    await update.message.reply_text(
        "⚙️ <b>Дополнительные разделы:</b>",
        parse_mode="HTML",
        reply_markup=more_menu_keyboard()
    )
    return CHOOSING

async def miniapp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.message:
        return CHOOSING
    if not await ensure_not_blocked(update):
        return CHOOSING
    miniapp_url = get_miniapp_url()
    if not miniapp_url:
        await update.message.reply_text(
            "Mini App URL не настроен. Укажите MINI_APP_URL в .env и перезапустите бота."
        )
        return CHOOSING
    try:
        await update.message.reply_text(
            f"Откройте Mini App:\n{miniapp_url}",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(BUTTON_MINIAPP, web_app=WebAppInfo(url=miniapp_url))]]
            ),
        )
    except BadRequest as exc:
        logging.warning("Inline Web App недоступен: %s", exc)
        await update.message.reply_text(
            "Не удалось открыть кнопку Mini App (ошибка Telegram). Проверьте, что MINI_APP_URL — "
            f"рабочий HTTPS-адрес.\nУказано сейчас: {miniapp_url}"
        )
    return CHOOSING


async def refresh_rates_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    del context
    if not update.message or not update.effective_user:
        return CHOOSING
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return CHOOSING
    try:
        fresh = get_exchange_rates(force_refresh=True)
    except (requests.RequestException, ValueError) as exc:
        logging.exception("refresh_rates: %s", exc)
        await update.message.reply_text(
            f"Не удалось обновить курсы: {exc}\nСтарые значения из кэша продолжают действовать."
        )
        return CHOOSING
    lines = ["Курсы обновлены из CoinGecko:"]
    for c in ("BTC", "ETH", "USDT", "RUB", "TON"):
        if c in fresh:
            lines.append(f"{c}: ${fresh[c]:,.6f}")
    lines.append(f"\nКэш действителен ещё {RATES_TTL:.0f} с.")
    await update.message.reply_text("\n".join(lines))
    return CHOOSING


async def close_ticket_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await close_ticket_cmd(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    
    if not update.message or not update.effective_user:
        return ConversationHandler.END
    
    try:
        await update.message.delete()
    except TelegramError:
        pass
    
    miniapp_url = get_miniapp_url()
    
    status_text = (
        "🧹 <b>Все действия отменены. Экран очищен.</b>\n\n"
        "Начинаем заново. Выберите действие в меню ниже или откройте Mini App:"
    )
    
    if miniapp_url:
        inline_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚀 Открыть Mini App", web_app=WebAppInfo(url=miniapp_url))]
        ])
        await update.message.reply_text(
            status_text,
            parse_mode="HTML",
            reply_markup=inline_kb
        )
    else:
        await update.message.reply_text(status_text, parse_mode="HTML")
    
    menu_msg = await update.message.reply_text(
        "👇 Основное меню:",
        reply_markup=main_menu_keyboard(update.effective_user.id)
    )
    context.user_data["last_menu_msg_id"] = menu_msg.message_id
    
    return CHOOSING


# --- Обработчик выбора валюты (ВНУТРИ ConversationHandler) ---

async def currency_select_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    """Обработчик inline-кнопок выбора валюты."""
    query = update.callback_query
    if not query or not query.data:
        return None
    await query.answer()

    data = query.data

    if data == "cur_show_others":
        await query.edit_message_reply_markup(
            reply_markup=currency_inline_keyboard(show_others=True)
        )
        return None
    if data == "cur_show_popular":
        await query.edit_message_reply_markup(
            reply_markup=currency_inline_keyboard(show_others=False)
        )
        return None

    if not data.startswith("cur_select:"):
        return None

    ticker = data.split(":", 1)[1]
    if ticker not in ALLOWED:
        await query.edit_message_text(f"Неверная валюта. Доступно: {allowed_text()}")
        return None

    mode = _exchange_mode_from_context(context)
    step = context.user_data.get("exchange_step", "first")

    # --- ПЕРВЫЙ ШАГ ---
    if step == "first":
        if mode == EXCHANGE_MODE_BUY:
            context.user_data["to_currency"] = ticker
            context.user_data["exchange_step"] = "second"
            await query.edit_message_text(
                prompt_buy_pay_currency(),
                reply_markup=currency_inline_keyboard(show_others=False),
            )
            return TO_CURRENCY
        else:
            context.user_data["from_currency"] = ticker
            context.user_data["exchange_step"] = "second"
            await query.edit_message_text(
                prompt_exchange_second_currency(mode),
                reply_markup=currency_inline_keyboard(show_others=False),
            )
            return TO_CURRENCY

    # --- ВТОРОЙ ШАГ ---
    if step == "second":
        if mode == EXCHANGE_MODE_BUY:
            to_currency = context.user_data.get("to_currency")
            if ticker == to_currency:
                await query.answer(
                    "Валюта оплаты должна отличаться от покупаемой.",
                    show_alert=True,
                )
                return TO_CURRENCY
            context.user_data["from_currency"] = ticker
            from_currency = ticker
        else:
            from_currency = context.user_data.get("from_currency")
            if not from_currency:
                await query.edit_message_text(
                    f"Сессия сброшена. {session_reset_exchange_hint()}"
                )
                return CHOOSING
            if ticker == from_currency:
                await query.answer(
                    "Валюты должны отличаться.",
                    show_alert=True,
                )
                return TO_CURRENCY
            context.user_data["to_currency"] = ticker

        context.user_data["exchange_step"] = "amount"
        await query.edit_message_text(
            prompt_exchange_amount(mode, from_currency),
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Введите сумму:",
            reply_markup=exchange_step_keyboard(),
        )
        return AMOUNT

    return None


# --- Обработчик контекстных inline-кнопок (ВНЕ ConversationHandler) ---

async def context_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    query = update.callback_query
    if not query or not query.data:
        return None
    await query.answer()

    data = query.data
    uid = update.effective_user.id if update.effective_user else 0
    chat_id = update.effective_chat.id

    if data == "ctx:continue_buy":
        draft = context.user_data.get("buy_draft")
        if not draft:
            await query.edit_message_text("Черновик не найден. Начните заново из меню.")
            return CHOOSING

        # Включаем нижнюю клавиатуру навигации ("Назад" / "В меню")
        await context.bot.send_message(
            chat_id=chat_id,
            text="▶️ Продолжаем оформление покупки...",
            reply_markup=buy_navigation_keyboard(),
        )

        # 1. Валюта ещё не выбрана
        if "currency" not in draft:
            context.user_data.pop("buy_last_state", None)
            keyboard = [[InlineKeyboardButton(c, callback_data=f"buy_cur:{c}")] for c in ALLOWED_BUY_CRYPTO]
            await query.edit_message_text(
                "Какую криптовалюту хотите купить? Выберите вариант из списка.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return BUY_CURRENCY

        crypto = draft["currency"]

        # 2. Сеть ещё не выбрана
        if "network" not in draft:
            context.user_data["buy_last_state"] = BUY_CURRENCY
            networks = CRYPTO_NETWORKS.get(crypto, [])
            keyboard = [[InlineKeyboardButton(net, callback_data=f"buy_net:{net}")] for net in networks]
            keyboard.append([InlineKeyboardButton("В меню", callback_data="buy_menu")])
            await query.edit_message_text(
                f"Вы выбрали: <b>{crypto}</b>\nВыберите сеть для получения:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return BUY_NETWORK

        network = draft["network"]

        # 3. Способ оплаты ещё не выбран
        if "payment_method" not in draft:
            context.user_data["buy_last_state"] = BUY_NETWORK
            keyboard = [[InlineKeyboardButton(m, callback_data=f"buy_pay:{m}")] for m in PAYMENT_METHODS]
            keyboard.append([InlineKeyboardButton("В меню", callback_data="buy_menu")])
            await query.edit_message_text(
                f"Вы покупаете: <b>{crypto}</b>\n"
                f"Сеть получения: <b>{network}</b>\n"
                f"Выберите способ оплаты.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return BUY_PAYMENT

        method = draft["payment_method"]

        # 4. Режим ввода суммы (RUB / крипта) не выбран
        if "amount_mode" not in draft:
            context.user_data["buy_last_state"] = BUY_PAYMENT
            keyboard = [
                [InlineKeyboardButton("Сумму оплаты (в RUB)", callback_data="buy_mode:fiat")],
                [InlineKeyboardButton("Сумму криптовалюты", callback_data="buy_mode:crypto")],
                [InlineKeyboardButton("В меню", callback_data="buy_menu")],
            ]
            await query.edit_message_text(
                f"К покупке: <b>{crypto}</b>\n"
                f"Сеть: <b>{network}</b>\n"
                f"Способ оплаты: <b>{method}</b>\n\n"
                f"Какую сумму хотите указать?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return BUY_AMOUNT_MODE

        # 5. Сумма ещё не введена
        if "pay_amount" not in draft:
            context.user_data["buy_last_state"] = BUY_AMOUNT_MODE
            prompt = (
                "Введите сумму в рублях (RUB), которую хотите оплатить:"
                if draft["amount_mode"] == "fiat"
                else f"Введите количество {crypto}, которое хотите получить:"
            )
            await query.edit_message_text(prompt)
            return BUY_AMOUNT_INPUT

        # 6. Адрес кошелька ещё не введён -> возвращаем на подтверждение расчёта
        if "wallet" not in draft:
            context.user_data["buy_last_state"] = BUY_AMOUNT_INPUT
            keyboard = [
                [InlineKeyboardButton("Подтвердить расчёт", callback_data="buy_rate:confirm")],
                [InlineKeyboardButton("Изменить сумму", callback_data="buy_rate:change")],
                [InlineKeyboardButton("В меню", callback_data="buy_menu")],
            ]
            await query.edit_message_text(
                f"Проверьте условия покупки:\n\n"
                f"Оплата: <b>{draft['pay_amount']:.2f} RUB</b>\n"
                f"Способ оплаты: <b>{method}</b>\n"
                f"Получение: <b>{draft['receive_amount']:.6f} {crypto}</b>\n"
                f"Сеть получения: <b>{network}</b>\n"
                f"Курс: <b>1 {crypto} = {draft['rate']:.2f} RUB</b>\n\n"
                f"Подтвердите расчёт, чтобы продолжить.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return BUY_RATE_CONFIRM

        # 7. Всё заполнено -> возвращаем на финальное подтверждение
        context.user_data["buy_last_state"] = BUY_WALLET_CONFIRM
        keyboard = [
            [InlineKeyboardButton("Подтвердить и создать заявку", callback_data="buy_final:create")],
            [InlineKeyboardButton("В меню", callback_data="buy_menu")],
        ]
        await query.edit_message_text(
            f"Проверьте данные перед созданием заявки:\n\n"
            f"Оплата: <b>{draft['pay_amount']:.2f} RUB</b>\n"
            f"Способ оплаты: <b>{method}</b>\n"
            f"Получение: <b>{draft['receive_amount']:.6f} {crypto}</b>\n"
            f"Сеть получения: <b>{network}</b>\n"
            f"Адрес получения: <code>{draft['wallet']}</code>\n\n"
            f"Подтвердите условия и создание заявки.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return BUY_FINAL_CONFIRM

    if data == "ctx:continue_draft":
        await query.edit_message_text("▶️ Функция «Продолжить обмен» находится в разработке.")
        return None

    if data == "ctx:continue_sell":
        draft = context.user_data.get("sell_draft")
        if not draft:
            await query.edit_message_text("Черновик продажи не найден. Начните заново из меню.")
            return CHOOSING

        await context.bot.send_message(
            chat_id=chat_id,
            text="▶️ Продолжаем оформление продажи...",
            reply_markup=buy_navigation_keyboard(),
        )

        if "currency" not in draft:
            keyboard = [[InlineKeyboardButton(c, callback_data=f"sell_cur:{c}")] for c in ALLOWED_SELL_CRYPTO]
            keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])
            await query.edit_message_text(
                "Какую криптовалюту хотите продать? Выберите вариант из списка.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return SELL_CURRENCY

        if "network" not in draft:
            networks = CRYPTO_NETWORKS.get(draft["currency"], [])
            keyboard = [[InlineKeyboardButton(net, callback_data=f"sell_net:{net}")] for net in networks]
            keyboard.append([InlineKeyboardButton("В меню", callback_data="sell_menu")])
            await query.edit_message_text(
                f"Вы выбрали: <b>{draft['currency']}</b>.\nВыберите сеть отправки:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return SELL_NETWORK

        if "payment_code" not in draft:
            return await sell_show_payment_methods(update, context, query)

        if "crypto_amount" not in draft:
            return await sell_show_amount_mode(update, context, query)

        if draft.get("payment_code") == "card" and "card_details" not in draft:
            return await sell_show_rate_confirm(update, context, query)

        return await sell_show_final_confirm(update, context, query)
    
    if data == "ctx:active_orders":
        expire_overdue_sell_orders()
        rows = get_user_orders(uid, limit=20)
        lines = ["🗂 Ваши активные заявки:"]
        for r in rows:
            if r[7] in ACTIVE_ORDER_STATUSES:
                rem = get_order_remaining_minutes(r[0])
                timer_str = f" (осталось ~{rem} мин.)" if rem is not None else ""
                lines.append(
                    f"\n#{r[0]} | {r[7]}{timer_str}\n"
                    f"{r[3]} {r[1]} → {r[5]:.6f} {r[2]}"
                )
        if len(lines) == 1:
            lines.append("Нет активных заявок.")
        await query.edit_message_text("\n".join(lines))
        return None

    if data.startswith("ctx:open_order:"):
        oid = int(data.split(":")[-1])
        await send_order_status_message(update, context, oid)
        return None

    # ...

    if data == "ctx:repeat":
        rows = get_user_orders(uid, limit=50)
        done_orders = [r for r in rows if r[7] == STATUS_DONE]

        if not done_orders:
            await query.edit_message_text("У вас нет выполненных заявок для повторения.")
            return None

        buttons = [
            [InlineKeyboardButton(f"#{r[0]} | {r[1]} → {r[2]}", callback_data=f"ctx:repeat_order:{r[0]}")]
            for r in done_orders
        ]
        await query.edit_message_text(
            "🔁 <b>Выберите выполненную заявку для повторения:</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return None

    if data.startswith("ctx:repeat_order:"):
        oid = int(data.split(":")[-1])
        row = get_order_by_id(oid)
        if not row:
            await query.edit_message_text("Заявка не найдена.")
            return None

        _oid, tg_id, from_c, to_c, amount, rate, total, wallet, status, created_at, updated_at = row

        _clear_exchange_draft(context)
        context.user_data["exchange_mode"] = EXCHANGE_MODE_SWAP
        context.user_data["from_currency"] = from_c
        context.user_data["to_currency"] = to_c
        context.user_data["exchange_step"] = "amount"

        await query.edit_message_text(
            f"🔁 <b>Повтор обмена</b>\n\n"
            f"Направление: {from_c} → {to_c}\n\n"
            f"Введите сумму в {from_c} (сколько отдаёте):",
            parse_mode="HTML",
            reply_markup=None,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Введите сумму:",
            reply_markup=exchange_step_keyboard(),
        )
        return AMOUNT

    if data == "ctx:delete_completed":
        deleted = delete_completed_orders(uid)
        await query.edit_message_text(
            f"🗑 Удалено выполненных заявок: {deleted}\n\nИстория обновлена."
        )
        inline_kb = get_context_inline_keyboard(uid, context)
        if inline_kb:
            await context.bot.send_message(
                chat_id=chat_id,
                text="👇 Выберите действие:",
                reply_markup=inline_kb,
            )
        await context.bot.send_message(
            chat_id=chat_id,
            text="Основное меню:",
            reply_markup=main_menu_keyboard(uid),
        )
        return None

    return None


# --- Админка: настройки и статистика ---

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return CHOOSING
    
    settings = get_settings()
    text = (
        f"⚙️ <b>Текущие настройки:</b>\n\n"
        f"💰 Комиссия: {settings['commission_percent']}%\n"
        f"📉 Мин. сумма обмена: {settings['min_amount_swap']}\n"
        f"📈 Макс. сумма обмена: {settings['max_amount_swap']}\n"
        f"🏦 Мин. вывод RUB: {settings['min_amount_withdraw_rub']}\n"
        f"🆘 Поддержка: {settings['support_contact']}\n\n"
        f"Чтобы изменить, введите: <code>/set_key значение</code>\n"
        f"Например: <code>/set_commission_percent 2.5</code>\n"
        f"Доступные ключи: commission_percent, min_amount_swap, max_amount_swap, min_amount_withdraw_rub, support_contact"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    return CHOOSING


async def set_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return CHOOSING
    
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Использование: /set_key значение")
        return CHOOSING
    
    key = parts[0].lstrip("/").replace("set_", "")
    value = parts[1].strip()
    
    if key in ("commission_percent", "min_amount_swap", "max_amount_swap", "min_amount_withdraw_rub"):
        try:
            value = float(value)
        except ValueError:
            await update.message.reply_text("Значение должно быть числом.")
            return CHOOSING
    
    if update_setting(key, value):
        await update.message.reply_text(f"✅ Настройка {key} обновлена на {value}.")
    else:
        await update.message.reply_text("❌ Неизвестный ключ настройки.")
    
    return CHOOSING


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return CHOOSING
    
    stats = get_admin_stats()
    
    lines = [
        "📊 <b>Статистика обменника</b>",
        "",
        f"📋 Всего заявок: {stats['total_orders']}",
    ]
    
    if stats["by_type"]:
        lines.append("\n📊 По типам:")
        for t, count in stats["by_type"].items():
            lines.append(f"  • {t}: {count}")
    
    lines.append(f"\n💰 Общая сумма в RUB: {stats['total_rub']:,.2f} ₽")
    
    if stats["top_users"]:
        lines.append("\n👥 Топ-5 пользователей по заявкам:")
        for user_id, count in stats["top_users"]:
            lines.append(f"  • ID {user_id}: {count} заявок")
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    return CHOOSING


# --- Админка ---


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not update.message:
        return ConversationHandler.END
    sync_admin_roles()
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Доступ запрещён.")
        return ConversationHandler.END
    await update.message.reply_text(
        "Панель администратора. Выберите раздел:",
        reply_markup=admin_menu_keyboard(),
    )
    return A_MENU


async def admin_menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return A_MENU
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text.strip()
    if text == BUTTON_A_EXIT:
        await update.message.reply_text(
            "Вы вышли из админки.",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
        return ConversationHandler.END
    if text == BUTTON_A_ORDERS:
        await update.message.reply_text(format_recent_orders_broadcast(25))
        return A_MENU
    if text == "🛒 Заявки на покупку":
        rows = list_purchase_orders() # Нужна новая функция в БД
        if not rows:
            await update.message.reply_text("Новых заявок на покупку нет.")
            return A_MENU
        for r in rows:
            oid, tg_id, from_c, to_c, amount, total, network, method, status = r
            text_msg = (
                f"Заявка #{oid} | {status}\n"
                f"User: {tg_id}\n"
                f"Покупка: {total:.6f} {to_c}\n"
                f"Сеть: {network}\n"
                f"Оплата: {amount:.2f} {from_c} ({method})\n"
            )
            # Отправляем сообщение со статусом и кнопками изменения статуса
            await update.message.reply_text(text_msg, reply_markup=status_keyboard_purchase(oid))
        return A_MENU
    if text == "💸 Заявки на продажу":
        rows = list_sell_orders()
        if not rows:
            await update.message.reply_text("Активных заявок на продажу нет.")
            return A_MENU
        for r in rows:
            oid, tg_id, from_c, to_c, amount, total, network, method, wallet, status, tx_hash = r
            is_qr = (method == SELL_METHOD_QR)
            tx_line = f"TX Hash: <code>{tx_hash}</code>\n" if tx_hash else "TX Hash: не указан\n"
            text_msg = (
                f"💸 <b>Продажа #{oid}</b> | {status}\n"
                f"User: <code>{tg_id}</code>\n"
                f"Отправляет: <b>{amount:.6f} {from_c}</b> ({network or '—'})\n"
                f"К выплате: <b>{total:.2f} {to_c}</b>\n"
                f"Способ: {method or '—'}\n"
                f"Реквизиты: <code>{wallet}</code>\n"
                f"{tx_line}"
            )
            await update.message.reply_text(
                text_msg,
                parse_mode="HTML",
                reply_markup=status_keyboard_sell(oid, is_qr=is_qr),
            )
        return A_MENU
    if text == BUTTON_A_TICKETS:
        await update.message.reply_text(format_tickets_broadcast())
        return A_MENU
    if text == BUTTON_A_REFRESH_RATES:
        try:
            fresh = get_exchange_rates(force_refresh=True)
            sample = ", ".join(
                f"{c}: ${fresh[c]:,.6f}"
                for c in ("BTC", "ETH", "USDT", "RUB")
                if c in fresh
            )
            await update.message.reply_text(
                "Курсы обновлены из CoinGecko.\n"
                f"Пример: {sample}\n"
                f"Кэш действителен ещё {RATES_TTL:.0f} с.",
                reply_markup=admin_menu_keyboard(),
            )
        except (requests.RequestException, ValueError) as exc:
            logging.exception("Ошибка принудительного обновления курсов: %s", exc)
            await update.message.reply_text(
                "Не удалось обновить курсы. Проверьте доступ к CoinGecko.\n"
                f"Причина: {exc}",
                reply_markup=admin_menu_keyboard(),
            )
        return A_MENU
    if text == BUTTON_A_CONFIRM:
        await update.message.reply_text("Введите номер заявки для смены статуса:")
        return A_CONF_ORDER
    if text == BUTTON_A_RATES:
        mults = list_rate_multipliers()
        info = (
            "Текущие множители к биржевым курсам:\n"
            + ("\n".join(f"{c}: ×{m:.6g}" for c, m in mults) if mults else "(по умолчанию ×1 для всех)")
            + f"\n\nДоступные валюты: {allowed_text()}\nВведите тикер (например USDT):"
        )
        await update.message.reply_text(info)
        return A_RATE_CUR
    if text == BUTTON_A_RESERVES:
        res = list_reserves()
        info = (
            "Текущие резервы:\n"
            + ("\n".join(f"{c}: {a:,.8f}" for c, a in res) if res else "(не заданы)")
            + f"\n\nВведите тикер валюты ({allowed_text()}):"
        )
        await update.message.reply_text(info)
        return A_RES_CUR
    if text == BUTTON_A_BLOCK:
        await update.message.reply_text("Введите Telegram ID пользователя для блокировки (число):")
        return A_BLOCK_ID
    if text == BUTTON_A_UNBLOCK:
        await update.message.reply_text("Введите Telegram ID для разблокировки (число):")
        return A_UNBLOCK_ID

    await update.message.reply_text("Выберите пункт меню админки.")
    return A_MENU


async def admin_route_if_panel_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    if not update.message or not update.message.text:
        return None
    t = update.message.text.strip()
    if t in _ADMIN_PANEL_REPLY_TEXTS:
        context.user_data.pop("admin_rate_cur", None)
        context.user_data.pop("admin_res_cur", None)
        return await admin_menu_router(update, context)
    return None


async def admin_confirm_order_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return A_CONF_ORDER
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Номер должен быть числом. Повторите или /admin.")
        return A_CONF_ORDER
    oid = int(text)
    row = get_order_by_id(oid)
    if not row:
        await update.message.reply_text("Заявка не найдена.")
        return A_MENU
    _o, tg_id, from_c, to_c, amount, rate, total, wallet, status, created_at, updated_at = row
    await update.message.reply_text(
        f"Заявка #{oid}\n"
        f"User: {tg_id}\n"
        f"{amount} {from_c} → {total:.8f} {to_c}\n"
        f"Курс: {rate:.8f}\n"
        f"Кошелёк: {wallet}\n"
        f"Текущий статус: {status}\n"
        f"Создана: {created_at}\n"
        f"Обновлена: {updated_at or '—'}\n\n"
        "Выберите новый статус:",
        reply_markup=status_keyboard(oid),
    )
    return A_MENU


async def admin_rate_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_RATE_CUR
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    cur = update.message.text.strip().upper()
    if cur not in ALLOWED:
        await update.message.reply_text(f"Неверный тикер. Доступно: {allowed_text()}")
        return A_RATE_CUR
    context.user_data["admin_rate_cur"] = cur
    await update.message.reply_text(
        f"Валюта {cur}. Введите множитель к биржевому курсу (например 1 или 0.995):"
    )
    return A_RATE_MULT


async def admin_rate_mult(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_RATE_MULT
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    cur = context.user_data.get("admin_rate_cur")
    if not cur:
        await update.message.reply_text("Сессия сброшена. Откройте /admin снова.")
        return A_MENU
    try:
        m = float(update.message.text.strip().replace(",", "."))
        set_currency_multiplier(str(cur), m)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return A_RATE_MULT
    context.user_data.pop("admin_rate_cur", None)
    await update.message.reply_text(
        f"Для {cur} установлен множитель ×{m:.8g}", reply_markup=admin_menu_keyboard()
    )
    return A_MENU


async def admin_res_currency(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_RES_CUR
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    cur = update.message.text.strip().upper()
    if cur not in ALLOWED:
        await update.message.reply_text(f"Неверный тикер. Доступно: {allowed_text()}")
        return A_RES_CUR
    context.user_data["admin_res_cur"] = cur
    now = get_reserve(cur)
    await update.message.reply_text(
        f"Текущий резерв {cur}: {now:,.8f}\nВведите новое значение резерва (число):"
    )
    return A_RES_AMT


async def admin_res_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_RES_AMT
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    cur = context.user_data.get("admin_res_cur")
    if not cur:
        await update.message.reply_text("Сессия сброшена. Откройте /admin снова.")
        return A_MENU
    try:
        amt = float(update.message.text.strip().replace(",", "."))
        set_reserve_amount(str(cur), amt)
    except ValueError as e:
        await update.message.reply_text(str(e))
        return A_RES_AMT
    context.user_data.pop("admin_res_cur", None)
    await update.message.reply_text(
        f"Резерв {cur} обновлён: {amt:,.8f}", reply_markup=admin_menu_keyboard()
    )
    return A_MENU


async def admin_block_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_BLOCK_ID
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Введите числовой Telegram ID.")
        return A_BLOCK_ID
    tid = int(text)
    if update.effective_user and tid == update.effective_user.id:
        await update.message.reply_text("Нельзя заблокировать самого себя.")
        return A_MENU
    ok = set_user_blocked(tid, True)
    await update.message.reply_text(
        "Пользователь заблокирован." if ok else "Пользователь не найден.",
        reply_markup=admin_menu_keyboard(),
    )
    return A_MENU


async def admin_unblock_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return A_UNBLOCK_ID
    routed = await admin_route_if_panel_button(update, context)
    if routed is not None:
        return routed
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("Введите числовой Telegram ID.")
        return A_UNBLOCK_ID
    tid = int(text)
    ok = set_user_blocked(tid, False)
    await update.message.reply_text(
        "Пользователь разблокирован." if ok else "Пользователь не найден.",
        reply_markup=admin_menu_keyboard(),
    )
    return A_MENU


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.effective_user and update.message:
        await update.message.reply_text(
            "Админ-панель закрыта.",
            reply_markup=main_menu_keyboard(update.effective_user.id),
        )
    return ConversationHandler.END


# --- Диалог админ ↔ пользователь ---


async def ticket_start_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    try:
        query = update.callback_query
        if not query or not query.data or not update.effective_user:
            logging.warning(
                "ticket_start_callback: нет query/data/user (query=%r, user=%r)",
                query, update.effective_user,
            )
            return None

        logging.info(
            "ticket_start_callback: data=%s admin_id=%s",
            query.data, update.effective_user.id,
        )
        await query.answer()

        uid = update.effective_user.id
        if not is_admin(uid):
            logging.warning("ticket_start_callback: %s не админ", uid)
            await query.edit_message_text("Нет доступа.")
            return None

        m = re.match(r"^ticket:(\d+)$", query.data)
        if not m:
            logging.warning("ticket_start_callback: callback_data не подходит: %s", query.data)
            return None
        target_user_id = int(m.group(1))

        previous = find_ticket_by_admin(uid)
        if previous and previous[0] != target_user_id:
            close_ticket(previous[0])
            try:
                await context.bot.send_message(
                    chat_id=previous[0],
                    text="Администратор переключился на другого пользователя. Обращение закрыто.",
                )
            except TelegramError:
                pass

        logging.info("ticket_start_callback: тикет создан %s ↔ %s", target_user_id, uid)

        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "С вами начал общение администратор поддержки.\n"
                    "Пишите сюда — сообщения попадут напрямую оператору.\n"
                    "Чтобы завершить обращение, нажмите «✅ Завершить обращение»."
                ),
                reply_markup=support_step_keyboard(),
            )
        except TelegramError as exc:
            logging.warning("Не удалось уведомить пользователя %s: %s", target_user_id, exc)

        user_name = "Пользователь"
        user_username = "—"
        try:
            with get_conn() as conn:
                cur = conn.cursor()
                _ensure_column(conn, "support_tickets", "name", "TEXT")
                _ensure_column(conn, "support_tickets", "username", "TEXT")
                cur.execute(
                    "SELECT name, username FROM support_tickets WHERE user_id = ?", 
                    (target_user_id,)
                )
                row = cur.fetchone()
                if row:
                    user_name = row[0] if row[0] else "Пользователь"
                    user_username = row[1] if row[1] else "—"
        except Exception as e:
            logging.error(f"Ошибка получения имени из тикета: {e}")

        await query.edit_message_text(
            f"Вы в диалоге с пользователем {user_name} (@{user_username}) [ID: {target_user_id}].\n"
            "Отправьте сообщение — оно будет доставлено пользователю от имени поддержки.\n"
            "Для выхода: /exit_dialog или кнопка ниже.",
            reply_markup=exit_dialog_keyboard(),
        )
        return A_REPLY_TO_USER

    except Exception:
        logging.exception("ticket_start_callback: необработанная ошибка")
        try:
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    "Ошибка при открытии диалога. Попробуйте ещё раз или напишите /admin."
                )
        except TelegramError:
            pass
        return None


async def admin_reply_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text or not update.effective_user:
        return A_REPLY_TO_USER
    text = update.message.text.strip()
    if text == "/exit_dialog":
        return await admin_exit_dialog(update, context)

    ticket = find_ticket_by_admin(update.effective_user.id)
    if not ticket:
        await update.message.reply_text(
            "Нет активного диалога. Откройте /admin и нажмите кнопку тикета заново.",
            reply_markup=exit_dialog_keyboard(),
        )
        return A_MENU

    target_user_id = ticket[0]
    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"💬 Сообщение от поддержки:\n{text}",
            reply_markup=support_step_keyboard(),
        )
        save_chat_message(target_user_id, "admin", text)
        
        await update.message.reply_text(
            "Отправлено пользователю.",
            reply_markup=exit_dialog_keyboard(),
        )
    except TelegramError as exc:
        logging.warning("Не доставлено пользователю %s: %s", target_user_id, exc)
        if "Chat not found" in str(exc) or "bot was blocked" in str(exc):
            close_ticket(target_user_id)
            await update.message.reply_text(
                f"Пользователь {target_user_id} заблокировал бота. Диалог автоматически закрыт.",
                reply_markup=admin_menu_keyboard(),
            )
            return A_MENU
        else:
            await update.message.reply_text(
                f"Не удалось доставить: {exc}",
                reply_markup=exit_dialog_keyboard(),
            )
    return A_REPLY_TO_USER


async def admin_exit_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user:
        ticket = find_ticket_by_admin(update.effective_user.id)
        if ticket:
            user_id = ticket[0]
            close_ticket(user_id)
            save_chat_message(user_id, "system", "Оператор завершил диалог. Спасибо за обращение!")
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="Администратор завершил обращение. Спасибо за обращение!",
                )
            except TelegramError as exc:
                logging.warning("Не удалось уведомить пользователя %s: %s", user_id, exc)
            if update.message:
                await update.message.reply_text(
                    "Диалог закрыт.",
                    reply_markup=admin_menu_keyboard(),
                )
            return A_MENU
    if update.message:
        await update.message.reply_text(
            "Активных диалогов нет.",
            reply_markup=admin_menu_keyboard(),
        )
    return A_MENU


async def dialog_exit_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    query = update.callback_query
    if not query or not update.effective_user:
        return None
    await query.answer()

    if not is_admin(update.effective_user.id):
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id if update.effective_chat else update.effective_user.id,
            text="Нет доступа.",
        )
        return None

    ticket = find_ticket_by_admin(update.effective_user.id)
    if not ticket:
        try:
            await query.edit_message_text(
                "У вас нет активного диалога.",
                reply_markup=None,
            )
        except TelegramError:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id if update.effective_chat else update.effective_user.id,
            text="Возврат в админ-панель:",
            reply_markup=admin_menu_keyboard(),
        )
        return A_MENU

    user_id = ticket[0]
    close_ticket(user_id)
    save_chat_message(user_id, "system", "Оператор завершил диалог. Спасибо за обращение!")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="Администратор завершил обращение. Спасибо за обращение!",
        )
    except TelegramError as exc:
        logging.warning("Не удалось уведомить пользователя %s: %s", user_id, exc)

    try:
        await query.edit_message_text(
            f"Диалог с пользователем {user_id} закрыт.",
            reply_markup=None,
        )
    except TelegramError:
        pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id if update.effective_chat else update.effective_user.id,
        text="Возврат в админ-панель:",
        reply_markup=admin_menu_keyboard(),
    )
    return A_MENU


async def on_app_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = getattr(context, "error", None)
    logging.getLogger(__name__).error(
        "Необработанная ошибка при обработке update: %s",
        repr(err),
        exc_info=err if isinstance(err, BaseException) else None,
    )

async def run_db(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


async def admin_callback_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> Optional[int]:
    query = update.callback_query
    if not query or not query.data or not update.effective_user:
        return None
    await query.answer()
    uid = update.effective_user.id
    if not is_admin(uid):
        await query.edit_message_text("Нет доступа.")
        return None
    m = re.match(r"^a:(\d+):([PRDC])$", query.data)
    if not m:
        return None
    oid = int(m.group(1))
    code = m.group(2)
    new_status = _STATUS_CB.get(code)
    if new_status not in ADMIN_ALLOWED_STATUSES:
        return None
    if not update_order_status(oid, new_status):
        await query.edit_message_text("Не удалось обновить заявку.")
        return A_MENU
    await query.edit_message_text(f"Заявка #{oid}: статус «{new_status}».")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Готово. Основное меню:",
        reply_markup=main_menu_keyboard(uid),
    )
    return ConversationHandler.END

async def check_expired_sell_orders_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Фоновая задача: каждые 60 секунд переводит просроченные заявки на продажу в статус 'Истекла'."""
    expired_orders = await run_db(expire_overdue_sell_orders)
    for oid, tg_id in expired_orders:
        logging.info("Заявка на продажу #%s истекла (60 мин) -> %s", oid, STATUS_EXPIRED)
        try:
            await context.bot.send_message(
                chat_id=tg_id,
                text=(
                    f"⏳ Срок отправки криптовалюты (60 минут) по заявке <b>#{oid}</b> истёк.\n"
                    f"Статус заявки изменён на: <b>{STATUS_EXPIRED}</b>.\n\n"
                    f"Заявка перенесена в историю операций. Если вы ещё не отправляли средства, "
                    f"создайте новую заявку в меню «Продать»."
                ),
                parse_mode="HTML",
            )
        except TelegramError as exc:
            logging.warning("Не удалось уведомить пользователя %s об истечении #%s: %s", tg_id, oid, exc)


def main() -> None:
    load_dotenv(override=True)
    init_db()
    sync_admin_roles()

    token = os.getenv("BOT_TOKEN", "YOUR_TOKEN")
    if token == "YOUR_TOKEN":
        raise RuntimeError("Укажите токен бота в переменной окружения BOT_TOKEN")

    admins = _admin_id_set()
    logger = logging.getLogger(__name__)
    logger.info(
        "Загружено админ-ID из ADMIN_TELEGRAM_IDS: %s",
        sorted(admins) if admins else "(пусто — только роль из БД)",
    )
    logger.info("TTL кэша курсов: %.0f с", RATES_TTL)

    application = ApplicationBuilder().token(token).build()
    if application.job_queue:
        application.job_queue.run_repeating(
            check_expired_sell_orders_job,
            interval=60,
            first=15,
        )

    # --- Админ-диалог ---
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", admin_entry),
            CallbackQueryHandler(ticket_start_callback, pattern=r"^ticket:\d+$"),
            CallbackQueryHandler(admin_sell_status_callback, pattern=r"^sa:\d+:QR_TIME$"),
        ],
        states={
            A_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_router),
                CallbackQueryHandler(admin_callback_status, pattern=r"^a:\d+:[PRDC]$"),
                CallbackQueryHandler(admin_sell_status_callback, pattern=r"^sa:\d+:QR_TIME$"),
                CallbackQueryHandler(ticket_start_callback, pattern=r"^ticket:\d+$"),
            ],
            A_CONF_ORDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_confirm_order_id),
                CallbackQueryHandler(admin_callback_status, pattern=r"^a:\d+:[PRDC]$"),
            ],
            A_RATE_CUR: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_currency)],
            A_RATE_MULT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_rate_mult)],
            A_RES_CUR: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_res_currency)],
            A_RES_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_res_amount)],
            A_BLOCK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_block_id)],
            A_UNBLOCK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_unblock_id)],
            A_REPLY_TO_USER: [
                CommandHandler("exit_dialog", admin_exit_dialog),
                CallbackQueryHandler(ticket_start_callback, pattern=r"^ticket:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_to_user),
            ],
            A_SELL_QR_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_sell_qr_time_input),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", admin_cancel),
            CommandHandler("exit_dialog", admin_exit_dialog),
        ],
        per_user=True,
        per_chat=True,
    )

    # --- Пользовательский диалог ---
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING: [
                CommandHandler("exchange", exchange_command),
                CommandHandler("rates", rates),
                CommandHandler("refresh_rates", refresh_rates_cmd),
                CommandHandler("my_orders", my_orders),
                CommandHandler("history", history_cmd),
                CommandHandler("status", cmd_status),
                CommandHandler("support", support),
                CommandHandler("miniapp", miniapp),
                CommandHandler("close_ticket", close_ticket_user_cmd),
                CommandHandler("settings", admin_settings),
                CommandHandler("stats", admin_stats),
                CommandHandler("set_commission_percent", set_setting),
                CommandHandler("set_min_amount_swap", set_setting),
                CommandHandler("set_max_amount_swap", set_setting),
                CommandHandler("set_min_amount_withdraw_rub", set_setting),
                CommandHandler("set_support_contact", set_setting),
                CallbackQueryHandler(cabinet_callback, pattern=r"^cab:"),
                CallbackQueryHandler(context_callback, pattern=r"^ctx:"),
                MessageHandler(filters.PHOTO | filters.Document.ALL, handle_user_media_in_choosing),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_buttons),
            ],
            BLOCKED_USER: [
                CommandHandler("start", start),
                CommandHandler("support", support),
                MessageHandler(filters.TEXT & ~filters.COMMAND, blocked_user_messages),
            ],
            ORDER_STATUS_WAIT: [
                CommandHandler("status", cmd_status),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order_status_id),
            ],
            FROM_CURRENCY: [
                CallbackQueryHandler(currency_select_callback, pattern=r"^cur_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_exchange_from),
            ],
            TO_CURRENCY: [
                CallbackQueryHandler(currency_select_callback, pattern=r"^cur_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_exchange_to),
            ],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount)],
            WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_order)],
            USER_IN_SUPPORT: [
                CommandHandler("close_ticket", close_ticket_user_cmd),
                MessageHandler(filters.PHOTO | filters.Document.ALL, user_in_support_media),
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_in_support_text),
            ],
            BUY_CURRENCY: [
                CallbackQueryHandler(buy_currency_selected, pattern=r"^buy_cur:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_NETWORK: [
                CallbackQueryHandler(buy_network_selected, pattern=r"^buy_net:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_PAYMENT: [
                CallbackQueryHandler(buy_payment_selected, pattern=r"^buy_pay:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_AMOUNT_MODE: [
                CallbackQueryHandler(buy_amount_mode_selected, pattern=r"^buy_mode:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_amount_input),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_RATE_CONFIRM: [
                CallbackQueryHandler(buy_rate_confirm, pattern=r"^buy_rate:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_WALLET_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_wallet_input),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_WALLET_CONFIRM: [
                CallbackQueryHandler(buy_wallet_confirm, pattern=r"^buy_wallet:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_FINAL_CONFIRM: [
                CallbackQueryHandler(buy_final_create, pattern=r"^buy_final:"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_PAYMENT_WAIT: [
                CallbackQueryHandler(buy_paid_callback, pattern=r"^buy_paid:|^buy_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            BUY_RECEIPT_UPLOAD: [
                CallbackQueryHandler(buy_upload_receipt_callback, pattern=r"^buy_upload_receipt:\d+$"),
                MessageHandler(filters.PHOTO | filters.Document.ALL, buy_receipt_upload),
                CallbackQueryHandler(lambda u, c: show_menu(u, c), pattern=r"^buy_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), buy_back),
            ],
            SELL_CURRENCY: [
                CallbackQueryHandler(sell_currency_selected, pattern=r"^sell_cur:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_NETWORK: [
                CallbackQueryHandler(sell_network_selected, pattern=r"^sell_net:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_PAYMENT: [
                CallbackQueryHandler(sell_payment_selected, pattern=r"^sell_pay:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_AMOUNT_MODE: [
                CallbackQueryHandler(sell_amount_mode_selected, pattern=r"^sell_mode:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_AMOUNT_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_amount_input),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_RATE_CONFIRM: [
                CallbackQueryHandler(sell_rate_confirm, pattern=r"^sell_rate:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_CARD_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_card_input),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_CARD_CONFIRM: [
                CallbackQueryHandler(sell_card_confirm, pattern=r"^sell_card:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_FINAL_CONFIRM: [
                CallbackQueryHandler(sell_final_create, pattern=r"^sell_final:|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_DEPOSIT_WAIT: [
                CallbackQueryHandler(sell_sent_callback, pattern=r"^sell_sent:\d+$|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_CHECKING_WAIT: [
                CallbackQueryHandler(sell_checking_actions_callback, pattern=r"^sell_txhash:\d+$|^sell_my_orders$|^sell_menu$"),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
            SELL_TXHASH_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & ~filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_txhash_input),
                MessageHandler(filters.Regex(f"^({BUTTON_BACK}|{BUTTON_BACK_TO_MENU})$"), sell_back),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
            CommandHandler("exchange", exchange_command),
            CommandHandler("close_ticket", close_ticket_user_cmd),
            CallbackQueryHandler(lambda u, c: show_menu(u, c), pattern=r"^buy_menu$|^sell_menu$")
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    application.add_handler(admin_conv, group=-1)
    application.add_handler(conv_handler, group=0)

    # --- Inline-кнопка «Завершить диалог» — вне ConversationHandler ---
    application.add_handler(
        CallbackQueryHandler(dialog_exit_callback, pattern=r"^dialog:exit$"),
        group=1,
    )

    application.add_handler(
        CallbackQueryHandler(admin_purchase_status_callback, pattern=r"^pa:\d+:[A-Z]+$"),
        group=1,
    )

    # Все кнопки продажи, кроме QR_TIME (она обрабатывается в admin_conv для перехода в A_SELL_QR_TIME)
    application.add_handler(
        CallbackQueryHandler(
            admin_sell_status_callback,
            pattern=r"^sa:\d+:(CRYPTO_OK|QR_ASK|DONE|CANCEL)$",
        ),
        group=1,
    )

    # Глобальный логгер необработанных апдейтов (DEBUG_UPDATES=1)
    debug_updates = os.getenv("DEBUG_UPDATES", "0") == "1"

    async def log_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            if update.callback_query:
                logger.warning(
                    "UNHANDLED update: callback data=%r from user=%s chat=%s",
                    update.callback_query.data,
                    update.effective_user.id if update.effective_user else None,
                    update.effective_chat.id if update.effective_chat else None,
                )
            elif update.message:
                logger.warning(
                    "UNHANDLED update: message text=%r from user=%s chat=%s",
                    update.message.text,
                    update.effective_user.id if update.effective_user else None,
                    update.effective_chat.id if update.effective_chat else None,
                )
            else:
                logger.warning("UNHANDLED update: %r", update)
        except Exception:
            logger.exception("log_all_updates упал")

    if debug_updates:
        application.add_handler(CallbackQueryHandler(log_all_updates), group=99)
        application.add_handler(MessageHandler(filters.ALL, log_all_updates), group=99)
        logger.info("DEBUG_UPDATES=1 — включён логгер необработанных апдейтов")

    application.add_error_handler(on_app_error)
    application.run_polling()


if __name__ == "__main__":
    main()