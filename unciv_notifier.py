#!/usr/bin/env python3
# unciv_notifier_prod.py
"""
Production-ready Unciv Telegram Notifier with multilingual support and persistent interval setting.
"""

import os
import asyncio
import logging
import json
import tempfile
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Any

import aiohttp
from aiohttp.client_exceptions import ClientError
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from dotenv import load_dotenv

# ---------------------------
# Конфіг / змінні середовища
# ---------------------------
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("unciv_notifier")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    logger.critical("TELEGRAM_TOKEN not set in environment variables! Exiting.")
    raise SystemExit("TELEGRAM_TOKEN required")

BASE_URL = os.getenv("UNCIV_BASE_URL", "https://uncivserver.xyz/jsons/")
STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")
# Значення за замовчуванням (60) буде використане, якщо не знайдено у .env та не збережено у state_file
MONITOR_INTERVAL_SECONDS_DEFAULT = int(os.getenv("MONITOR_INTERVAL_SECONDS", "60")) 
REMINDER_SCHEDULE = [
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
]

ERROR_DEBOUNCE_SECONDS = 300  # 5 minutes

# ---------------------------
# Глобальний стан (в пам'яті)
# ---------------------------
state_lock = asyncio.Lock()

GAME_ID: Optional[str] = None
CURRENT_GAME_URL: Optional[str] = None
LAST_TURN_PLAYER: Optional[str] = None  # normalized uppercase
MAIN_CHAT_ID: Optional[int] = None
NOTIFY_PAUSED: bool = False
AVAILABLE_NATIONS: list[str] = []
PLAYER_LOGINS: Dict[str, str] = {}  # normalized -> @username
PLAYER_NOTIFICATION_STATE: Dict[str, Dict[str, Any]] = {}  # normalized -> state
ETAG_CACHE: Dict[str, str] = {}  # url -> etag

BOT_LANGUAGE: str = os.getenv("BOT_LANGUAGE", "en") # 'en' або 'ua'

_last_error_time_by_chat: Dict[int, float] = {}

# ---------------------------
# 1. СЛОВНИК ПЕРЕКЛАДІВ
# ---------------------------

TRANSLATIONS = {
    "en": {
        "welcome": "<b>👋 Welcome to Unciv Notifier!</b>\nUse /help for commands.",
        "help_text": (
            "<b>Unciv Notifier Commands</b>\n\n"
            "<b>/setgame &lt;Game_ID&gt;</b> - Set game ID and start monitoring.\n"
            "<b>/setinterval &lt;seconds&gt;</b> - Change monitoring interval (default 60s).\n"
            "<b>/subscribe &lt;Nation&gt; &lt;@username&gt;</b> - Subscribe for nation ping.\n"
            "<b>/unsubscribe &lt;@username&gt;</b> - Remove subscription by username.\n"
            "<b>/pause</b> - Toggle reminders pause.\n"
            "<b>/status</b> - Current status (game, turn, pause).\n"
            "<b>/list</b> - List all subscriptions.\n"
            "<b>/setlang &lt;en|ua&gt;</b> - Change bot language (Default: en).\n"
            "<b>/help</b> - This message.\n"
        ),
        "setgame_usage": "Please specify the Game ID. Format: /setgame <Game_ID>",
        "setgame_fail_fetch": "❌ Failed to fetch data for ID {game_id}.",
        "setgame_fail_connect": "❌ Failed to connect to server: {error}",
        "setgame_success": (
            "✅ Monitoring established.\n<b>ID:</b> {game_id}\n<b>Nations:</b> {nations_list}\n"
            "Monitoring interval: {interval}s"
        ),
        "setgame_no_humans": "No human players found.",
        "setinterval_usage": "Specify interval in seconds. Example: /setinterval 120",
        "setinterval_success": "Monitoring interval set to {interval} seconds.",
        "subscribe_usage": "Format: /subscribe <Nation> @username",
        "subscribe_username_format": "Username must start with @.",
        "subscribe_nation_not_found": "Nation not found in the current game. Use /setgame to refresh the list.",
        "subscribe_success": "✅ Subscription saved: {nation} -> {username}",
        "unsubscribe_usage": "Specify @username to remove subscription. Format: /unsubscribe @username",
        "unsubscribe_success": "✅ Subscription removed: {username} (Nation: {nation})",
        "unsubscribe_not_found": "Login {username} not found in subscriptions.",
        "pause_on": "⏸️ Reminders paused.",
        "pause_off": "▶️ Reminders resumed!",
        "pause_resumed_ping": " {login}, it's your turn (Nation {nation}).",
        "pause_resumed_no_ping": " Nation {nation}'s turn is still waiting.",
        "status_no_game": "❗ No game set. Use /setgame <Game_ID>.",
        "status_text": "<b>Status</b>\n<b>Game ID:</b> {game_id}\n<b>Notifications paused:</b> {paused}\n",
        "status_paused_yes": "Yes",
        "status_paused_no": "No",
        "status_current_player": "<b>Last/current player:</b> {nation} ({login})\n",
        "status_login_none": "(not subscribed)",
        "status_unknown_player": "<b>Last/current player:</b> unknown\n",
        "list_empty": "No subscriptions.",
        "list_title": "<b>Subscriptions:</b>",
        "setlang_usage": "Specify the language: /setlang en or /setlang ua",
        "setlang_success": "✅ Language set to {lang_name} ({lang_code}).",
        "setlang_name_en": "English",
        "setlang_name_ua": "Ukrainian",
        "notify_main_ping": "🚨 <b>{nation}</b>. It's your turn, {login}!",
        "notify_main_no_ping": "🚨 <b>NEW TURN!</b> Nation <b>{nation}</b>!",
        "notify_reminder_ping": "🔔 <b>REMINDER!</b> {login}, please take your turn. (Elapsed: {delay})",
        "notify_reminder_no_ping": "🔔 <b>REMINDER!</b> Nation <b>{nation}</b> turn is waiting. (Elapsed: {delay})",
        "error_server_down": "⚠️ Cannot reach the game server. Notifications paused temporarily.",
    },
    "ua": {
        "welcome": "<b>👋 Ласкаво просимо до Unciv Notifier!</b>\nВикористайте /help для команд.",
        "help_text": (
            "<b>Команди Unciv Notifier</b>\n\n"
            "<b>/setgame &lt;Game_ID&gt;</b> - Встановити ID гри та почати моніторинг.\n"
            "<b>/setinterval &lt;seconds&gt;</b> - Змінити інтервал моніторингу (за замовчуванням 60s).\n"
            "<b>/subscribe &lt;Нація&gt; &lt;@username&gt;</b> - Підписатися на пінг для нації.\n"
            "<b>/unsubscribe &lt;@username&gt;</b> - Видалити підписку за логіном.\n"
            "<b>/pause</b> - Переключити паузу нагадувань.\n"
            "<b>/status</b> - Поточний стан (гра, хід, пауза).\n"
            "<b>/list</b> - Список усіх підписок.\n"
            "<b>/setlang &lt;en|ua&gt;</b> - Змінити мову бота (За замовчуванням: en).\n"
            "<b>/help</b> - Це повідомлення.\n"
        ),
        "setgame_usage": "Будь ласка, вкажіть ID гри. Формат: /setgame <Game_ID>",
        "setgame_fail_fetch": "❌ Не вдалося отримати дані для ID {game_id}.",
        "setgame_fail_connect": "❌ Не вдалося підключитися до сервера: {error}",
        "setgame_success": (
            "✅ Моніторинг встановлено.\n<b>ID:</b> {game_id}\n<b>Нації:</b> {nations_list}\n"
            "Інтервал моніторингу: {interval}с"
        ),
        "setgame_no_humans": "Не знайдено гравців-людей.",
        "setinterval_usage": "Вкажіть інтервал у секундах. Приклад: /setinterval 120",
        "setinterval_success": "Інтервал моніторингу встановлено на {interval} секунд.",
        "subscribe_usage": "Формат: /subscribe <Нація> @username",
        "subscribe_username_format": "Username повинен починатися з @.",
        "subscribe_nation_not_found": "Націю не знайдено в поточній грі. Виконайте /setgame, щоб оновити список.",
        "subscribe_success": "✅ Підписка збережена: {nation} -> {username}",
        "unsubscribe_usage": "Вкажіть @username для видалення підписки. Формат: /unsubscribe @username",
        "unsubscribe_success": "✅ Підписка видалена: {username} (Нація: {nation})",
        "unsubscribe_not_found": "Логін {username} не знайдений у підписках.",
        "pause_on": "⏸️ Нагадування призупинено.",
        "pause_off": "▶️ Нагадування відновлено!",
        "pause_resumed_ping": " {login}, ваш хід (Нація {nation}).",
        "pause_resumed_no_ping": " Хід нації {nation} досі очікує.",
        "status_no_game": "❗ Немає встановленої гри. Виконайте /setgame <Game_ID>.",
        "status_text": "<b>Статус</b>\n<b>Game ID:</b> {game_id}\n<b>Пауза сповіщень:</b> {paused}\n",
        "status_paused_yes": "Так",
        "status_paused_no": "Ні",
        "status_current_player": "<b>Останній/поточний гравець:</b> {nation} ({login})\n",
        "status_login_none": "(не підписано)",
        "status_unknown_player": "<b>Останній/поточний гравець:</b> невідомо\n",
        "list_empty": "Підписок немає.",
        "list_title": "<b>Підписки:</b>",
        "setlang_usage": "Вкажіть мову: /setlang en або /setlang ua",
        "setlang_success": "✅ Мову встановлено на {lang_name} ({lang_code}).",
        "setlang_name_en": "Англійська",
        "setlang_name_ua": "Українська",
        "notify_main_ping": "🚨 <b>{nation}</b>. Ваш хід, {login}!",
        "notify_main_no_ping": "🚨 <b>НОВИЙ ХІД!</b> Нація <b>{nation}</b>!",
        "notify_reminder_ping": "🔔 <b>НАГАДУВАННЯ!</b> {login}, зробіть свій хід. (Пройшло: {delay})",
        "notify_reminder_no_ping": "🔔 <b>НАГАДУВАННЯ!</b> Хід нації <b>{nation}</b> досі очікує. (Пройшло: {delay})",
        "error_server_down": "⚠️ Не можу дістатися сервера гри. Сповіщення призупинено тимчасово.",
    }
}

def _(key: str, **kwargs) -> str:
    """Отримати переклад за ключем та форматувати його."""
    lang_data = TRANSLATIONS.get(BOT_LANGUAGE, TRANSLATIONS['en'])
    text = lang_data.get(key, TRANSLATIONS['en'].get(key, f"Translation missing for {key} in {BOT_LANGUAGE}"))
    return text.format(**kwargs)

# ---------------------------
# Утиліти для роботи зі станом (асинхронно)
# ---------------------------
async def atomic_write_json(path: str, data: dict):
    """Атомарний запис JSON у файл (через тимчасовий файл)."""
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="state_", suffix=".json")
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

async def save_state():
    """Сохраняет минимальный стан у файл (под mutex)."""
    global GAME_ID, CURRENT_GAME_URL, MAIN_CHAT_ID, PLAYER_LOGINS, LAST_TURN_PLAYER, BOT_LANGUAGE, MONITOR_INTERVAL_SECONDS_DEFAULT
    async with state_lock:
        payload = {
            "GAME_ID": GAME_ID,
            "CURRENT_GAME_URL": CURRENT_GAME_URL,
            "MAIN_CHAT_ID": MAIN_CHAT_ID,
            "PLAYER_LOGINS": PLAYER_LOGINS,
            "LAST_TURN_PLAYER": LAST_TURN_PLAYER,
            "BOT_LANGUAGE": BOT_LANGUAGE, 
            "MONITOR_INTERVAL": MONITOR_INTERVAL_SECONDS_DEFAULT, # <-- ЗБЕРІГАЄМО ІНТЕРВАЛ
        }
        try:
            await atomic_write_json(STATE_FILE, payload)
            logger.debug("State saved.")
        except Exception as e:
            logger.exception("Failed to save state: %s", e)

async def load_state():
    """Завантажує стан з файлу (пом'якшено)."""
    global GAME_ID, CURRENT_GAME_URL, MAIN_CHAT_ID, PLAYER_LOGINS, LAST_TURN_PLAYER, BOT_LANGUAGE, MONITOR_INTERVAL_SECONDS_DEFAULT
    if not os.path.exists(STATE_FILE):
        logger.info("State file not found; starting fresh.")
        return False
    async with state_lock:
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                payload = json.load(f)
            GAME_ID = payload.get("GAME_ID")
            CURRENT_GAME_URL = payload.get("CURRENT_GAME_URL")
            MAIN_CHAT_ID = payload.get("MAIN_CHAT_ID")
            PLAYER_LOGINS = payload.get("PLAYER_LOGINS", {})
            LAST_TURN_PLAYER = payload.get("LAST_TURN_PLAYER")
            BOT_LANGUAGE = payload.get("BOT_LANGUAGE", "en")
            # <-- ВІДНОВЛЮЄМО ІНТЕРВАЛ. Використовуємо 60 як дефолт, якщо у файлі немає
            MONITOR_INTERVAL_SECONDS_DEFAULT = payload.get("MONITOR_INTERVAL", 60)
            logger.info("State loaded from file. GAME_ID=%s, Lang=%s, Interval=%s", GAME_ID, BOT_LANGUAGE, MONITOR_INTERVAL_SECONDS_DEFAULT)
            return True
        except Exception as e:
            logger.exception("Failed to load state: %s", e)
            return False

def normalize(n: Optional[str]) -> Optional[str]:
    return n.upper() if n else n

def get_raw_nation_name(normalized_name: str) -> str:
    return next((n for n in AVAILABLE_NATIONS if n.upper() == normalized_name), normalized_name)

# ---------------------------
# HTTP: async fetch з підтримкою ETag
# ---------------------------
async def fetch_game_json(session: aiohttp.ClientSession, url: str, timeout: int = 10) -> Optional[dict]:
    headers = {}
    etag = ETAG_CACHE.get(url)
    if etag:
        headers['If-None-Match'] = etag

    try:
        async with session.get(url, headers=headers, timeout=timeout) as resp:
            if resp.status == 304:
                return {"_not_modified": True}
            resp.raise_for_status()
            etag_new = resp.headers.get('ETag') or resp.headers.get('etag')
            if etag_new:
                ETAG_CACHE[url] = etag_new
            data = await resp.json()
            return data
    except asyncio.TimeoutError:
        logger.warning("Timeout fetching %s", url)
        raise
    except ClientError as e:
        logger.warning("ClientError fetching %s: %s", url, e)
        raise

# ---------------------------
# Telegram command handlers
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAIN_CHAT_ID
    if update.effective_chat:
        MAIN_CHAT_ID = update.effective_chat.id
        await save_state()
    await update.message.reply_html(_("welcome"))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_("help_text"), parse_mode=ParseMode.HTML)

async def set_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановлює ID гри і стартує моніторинг (або оновлює URL)."""
    global GAME_ID, CURRENT_GAME_URL, AVAILABLE_NATIONS, LAST_TURN_PLAYER, PLAYER_NOTIFICATION_STATE, MAIN_CHAT_ID, PLAYER_LOGINS

    if update.effective_chat:
        MAIN_CHAT_ID = update.effective_chat.id

    if not context.args:
        await update.message.reply_text(_("setgame_usage"))
        return

    new_game_id = context.args[0].strip()
    new_game_url = f"{BASE_URL}{new_game_id}"

    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_game_json(session, new_game_url)
            if data is None or data.get("_not_modified"):
                await update.message.reply_text(_("setgame_fail_fetch", game_id=new_game_id))
                return
        except Exception as e:
            await update.message.reply_text(_("setgame_fail_connect", error=e))
            return

    # --- ФІНАЛЬНЕ ВИПРАВЛЕННЯ: Безумовне очищення стану при /setgame ---
    
    # 1. Очищуємо підписки та старий стан у пам'яті.
    PLAYER_LOGINS.clear()             
    PLAYER_NOTIFICATION_STATE.clear() 
    AVAILABLE_NATIONS.clear()
    LAST_TURN_PLAYER = None
    logger.info("Unconditionally cleared PLAYER_LOGINS and previous game state in memory on /setgame.") 
    
    # 2. Встановлюємо новий ID
    GAME_ID = new_game_id
    CURRENT_GAME_URL = new_game_url
    # -------------------------------------------------------------------

    try:
        civs = data.get('civilizations', []) if isinstance(data, dict) else []
        human_civs = [c.get('civName') for c in civs if c.get('playerType') == 'Human']
        AVAILABLE_NATIONS = [n for n in human_civs if n]
    except Exception:
        AVAILABLE_NATIONS = []

    await save_state() # <-- АСИНХРОННЕ ЗБЕРЕЖЕННЯ ПУСТОГО СЛОВНИКА LOGINS В .JSON

    nations_list = ", ".join(AVAILABLE_NATIONS) if AVAILABLE_NATIONS else _("setgame_no_humans")
    
    current_interval = MONITOR_INTERVAL_SECONDS_DEFAULT

    await update.message.reply_html(
        _("setgame_success", game_id=GAME_ID, nations_list=nations_list, interval=current_interval)
    )

    logger.info("Set game to %s. Human nations: %s", GAME_ID, ", ".join(AVAILABLE_NATIONS))

async def set_interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Дозволяє поміняти інтервал моніторингу в секундах і зберігає це."""
    global MONITOR_INTERVAL_SECONDS_DEFAULT
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(_("setinterval_usage"))
        return
    new_interval = int(context.args[0])
    MONITOR_INTERVAL_SECONDS_DEFAULT = new_interval

    jobs = context.job_queue.get_jobs_by_name('monitor_job')
    if jobs:
        for j in jobs:
            j.schedule_removal()
    
    context.job_queue.run_repeating(monitor_game_state, interval=new_interval, first=1, name='monitor_job')
    
    await save_state() # <-- ЗБЕРІГАЄМО НОВИЙ ІНТЕРВАЛ У ФАЙЛ СТАНУ
    
    await update.message.reply_text(_("setinterval_success", interval=new_interval))

async def set_language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Встановлює мову бота."""
    global BOT_LANGUAGE

    if not context.args or context.args[0].lower() not in ['en', 'ua']:
        await update.message.reply_text(_("setlang_usage"))
        return
    
    new_lang = context.args[0].lower()
    BOT_LANGUAGE = new_lang
    await save_state()
    
    lang_name_key = f"setlang_name_{new_lang}"
    lang_name = _(lang_name_key)

    await update.message.reply_text(_("setlang_success", lang_name=lang_name, lang_code=new_lang))

async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Підписка на пінг для нації."""
    global PLAYER_LOGINS, MAIN_CHAT_ID, AVAILABLE_NATIONS
    if update.effective_chat:
        MAIN_CHAT_ID = update.effective_chat.id

    if len(context.args) < 2:
        await update.message.reply_text(_("subscribe_usage"))
        return
    username_raw = context.args[-1]
    nation_input = " ".join(context.args[:-1]).strip()

    if not username_raw.startswith("@"):
        await update.message.reply_text(_("subscribe_username_format"))
        return

    # --- ВИПРАВЛЕННЯ: Використовуємо пошук підрядка для більш гнучкого порівняння ---
    nation_input_upper = nation_input.upper()
    found = next(
        (n for n in AVAILABLE_NATIONS if nation_input_upper in n.upper()), 
        None
    )
    # ---------------------------------------------------------------------------------
    
    if not found:
        await update.message.reply_text(_("subscribe_nation_not_found"))
        return

    norm = normalize(found)
    PLAYER_LOGINS[norm] = username_raw
    await save_state()
    await update.message.reply_text(_("subscribe_success", nation=found, username=username_raw))

async def unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global PLAYER_LOGINS
    if not context.args or not context.args[0].startswith("@"):
        await update.message.reply_text(_("unsubscribe_usage"))
        return
    username = context.args[0]
    found_key = next((k for k, v in PLAYER_LOGINS.items() if v == username), None)
    if found_key:
        raw_name = get_raw_nation_name(found_key)
        del PLAYER_LOGINS[found_key]
        await save_state()
        await update.message.reply_text(_("unsubscribe_success", username=username, nation=raw_name))
    else:
        await update.message.reply_text(_("unsubscribe_not_found", username=username))

async def pause_notifications(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключає паузу."""
    global NOTIFY_PAUSED, LAST_TURN_PLAYER, PLAYER_NOTIFICATION_STATE
    NOTIFY_PAUSED_BEFORE = NOTIFY_PAUSED
    NOTIFY_PAUSED = not NOTIFY_PAUSED
    
    if NOTIFY_PAUSED:
        await update.message.reply_text(_("pause_on"))
    else:
        msg = _("pause_off")
        if LAST_TURN_PLAYER and NOTIFY_PAUSED_BEFORE:
            raw = get_raw_nation_name(LAST_TURN_PLAYER)
            login = PLAYER_LOGINS.get(LAST_TURN_PLAYER)
            if login:
                msg += _("pause_resumed_ping", login=login, nation=raw)
            else:
                msg += _("pause_resumed_no_ping", nation=raw)
            
            # скинути таймери
            PLAYER_NOTIFICATION_STATE[LAST_TURN_PLAYER] = {
                'last_turn_change_time': datetime.now().isoformat(),
                'next_reminder_index': 0,
                'last_reminded_time': datetime.now().isoformat(),
            }
        await update.message.reply_text(msg)
    await save_state()

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує поточний статус бота/гри."""
    global GAME_ID, LAST_TURN_PLAYER, NOTIFY_PAUSED, PLAYER_LOGINS
    if not GAME_ID:
        await update.message.reply_text(_("status_no_game"))
        return
    
    paused_text = _("status_paused_yes") if NOTIFY_PAUSED else _("status_paused_no")
    s = _("status_text", game_id=GAME_ID, paused=paused_text)

    if LAST_TURN_PLAYER:
        raw = get_raw_nation_name(LAST_TURN_PLAYER)
        login = PLAYER_LOGINS.get(LAST_TURN_PLAYER)
        login_text = login if login else _("status_login_none")
        s += _("status_current_player", nation=raw, login=login_text)
    else:
        s += _("status_unknown_player")
        
    await update.message.reply_text(s, parse_mode=ParseMode.HTML)

async def list_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показує всі підписки."""
    if not PLAYER_LOGINS:
        await update.message.reply_text(_("list_empty"))
        return
    lines = [_("list_title")]
    for norm, username in PLAYER_LOGINS.items():
        raw = get_raw_nation_name(norm)
        lines.append(f"{raw} -> {username}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

# ---------------------------
# Моніторинг гри (JobQueue)
# ---------------------------
async def send_debounced_error(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message: str):
    """Відправляє повідомлення про помилку, але не частіше ніж ERROR_DEBOUNCE_SECONDS."""
    now = time.time()
    last = _last_error_time_by_chat.get(chat_id, 0)
    if now - last >= ERROR_DEBOUNCE_SECONDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=message)
            _last_error_time_by_chat[chat_id] = now
        except Exception as e:
            logger.exception("Failed to send error message to chat %s: %s", chat_id, e)

async def monitor_game_state(context: ContextTypes.DEFAULT_TYPE):
    """Основна функція опитування гри, надсилання сповіщень та нагадувань."""
    global CURRENT_GAME_URL, LAST_TURN_PLAYER, PLAYER_NOTIFICATION_STATE, MAIN_CHAT_ID, NOTIFY_PAUSED, PLAYER_LOGINS

    if not CURRENT_GAME_URL:
        return

    async with aiohttp.ClientSession() as session:
        try:
            data = await fetch_game_json(session, CURRENT_GAME_URL)
        except Exception:
            logger.warning("Fetch game failed.")
            if MAIN_CHAT_ID:
                await send_debounced_error(context, MAIN_CHAT_ID, _("error_server_down"))
            return

        if isinstance(data, dict) and data.get("_not_modified"):
            current_player_raw = LAST_TURN_PLAYER
            if not current_player_raw:
                return
        else:
            current_player_raw = data.get("currentPlayer") if isinstance(data, dict) else None

        if not current_player_raw:
            logger.info("No currentPlayer found in game JSON; skipping.")
            return

        current_norm = normalize(current_player_raw)

        # --- НОВЕ: Обчислюємо час початку ходу з JSON (currentTurnStartTime) ---
        current_turn_start_ms = data.get("currentTurnStartTime")
        current_turn_time = datetime.now() # Fallback, якщо поле відсутнє
        if current_turn_start_ms and isinstance(current_turn_start_ms, (int, float)):
            try:
                # Конвертуємо мілісекунди в секунди
                current_turn_time = datetime.fromtimestamp(current_turn_start_ms / 1000)
            except ValueError:
                logger.error("Invalid currentTurnStartTime value: %s", current_turn_start_ms)
        
        turn_start_time_iso = current_turn_time.isoformat()
        # ------------------------------------------------------------------------

        if LAST_TURN_PLAYER is None:
            LAST_TURN_PLAYER = current_norm
            
            # --- ВИПРАВЛЕННЯ: Ініціалізуємо стан нагадувань для поточного гравця, використовуючи час з JSON ---
            PLAYER_NOTIFICATION_STATE[current_norm] = {
                'last_turn_change_time': turn_start_time_iso, # <-- ВИКОРИСТОВУЄМО ЧАС З ГРИ
                'next_reminder_index': 0,
                'last_reminded_time': turn_start_time_iso,    # <-- ВИКОРИСТОВУЄМО ЧАС З ГРИ
            }
            # -----------------------------------------------------------------------
            
            try:
                civs = data.get('civilizations', []) if isinstance(data, dict) else []
                human_civs = [c.get('civName') for c in civs if c.get('playerType') == 'Human']
                if human_civs:
                    AVAILABLE_NATIONS.clear()
                    AVAILABLE_NATIONS.extend([n for n in human_civs if n])
            except Exception:
                pass
            
            await save_state() # Зберігаємо ініціалізований стан
            
            logger.info("Initial last player set to %s. Reminder state initialized.", LAST_TURN_PLAYER)
            return

        if current_norm != LAST_TURN_PLAYER:
            logger.info("Turn changed: %s -> %s", LAST_TURN_PLAYER, current_norm)
            LAST_TURN_PLAYER = current_norm
            
            player_login = PLAYER_LOGINS.get(current_norm)
            raw_name = current_player_raw
            if MAIN_CHAT_ID and not NOTIFY_PAUSED:
                if player_login:
                    text = _("notify_main_ping", nation=raw_name, login=player_login)
                else:
                    text = _("notify_main_no_ping", nation=raw_name)
                try:
                    await context.bot.send_message(chat_id=MAIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.exception("Failed to send main notification: %s", e)

            PLAYER_NOTIFICATION_STATE[current_norm] = {
                'last_turn_change_time': turn_start_time_iso, # <-- ВИКОРИСТОВУЄМО ЧАС З ГРИ
                'next_reminder_index': 0,
                'last_reminded_time': turn_start_time_iso,    # <-- ВИКОРИСТОВУЄМО ЧАС З ГРИ
            }
            await save_state()
            return

        if current_norm in PLAYER_NOTIFICATION_STATE and MAIN_CHAT_ID and not NOTIFY_PAUSED:
            st = PLAYER_NOTIFICATION_STATE[current_norm]
            # Перевірка, чи це об'єкт datetime чи рядок
            last_reminded_raw = st.get('last_reminded_time')
            if isinstance(last_reminded_raw, str):
                 last_reminded = datetime.fromisoformat(last_reminded_raw)
            else:
                 # Якщо збереження сталося некоректно або це перше завантаження без ініціалізації, використовуємо поточний час
                 last_reminded = datetime.now()
                 st['last_reminded_time'] = last_reminded.isoformat() # Фіксуємо його
                 
            idx = st.get('next_reminder_index', 0)
            if idx < len(REMINDER_SCHEDULE):
                target = last_reminded + REMINDER_SCHEDULE[idx]
                if datetime.now() >= target:
                    
                    login = PLAYER_LOGINS.get(current_norm)
                    delay_str = str(REMINDER_SCHEDULE[idx])
                    
                    if login:
                        msg = _("notify_reminder_ping", login=login, delay=delay_str)
                    else:
                        raw_name = get_raw_nation_name(current_norm)
                        msg = _("notify_reminder_no_ping", nation=raw_name, delay=delay_str)
                        
                    try:
                        await context.bot.send_message(chat_id=MAIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
                        logger.info("Reminder sent for %s (idx=%s)", current_norm, idx)
                    except Exception:
                        logger.exception("Failed to send reminder")
                        
                    st['last_reminded_time'] = datetime.now().isoformat()
                    st['next_reminder_index'] = idx + 1
                    await save_state()
            else:
                logger.debug("Reminders exhausted for %s", current_norm)

# ---------------------------
# Boot / main
# ---------------------------
def build_app():
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команди
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setgame", set_game))
    application.add_handler(CommandHandler("setinterval", set_interval_cmd))
    application.add_handler(CommandHandler("subscribe", subscribe))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe))
    application.add_handler(CommandHandler("pause", pause_notifications))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("list", list_subscriptions))
    application.add_handler(CommandHandler("setlang", set_language_cmd))
    return application

async def async_main_setup():
    """Асинхронна частина, що запускається один раз для налаштування."""
    await load_state()

def main():
    """Синхронний запуск бота, який уникає конфліктів циклу подій."""
    try:
        asyncio.run(async_main_setup())
    except RuntimeError as e:
        # Ця помилка може виникати, якщо цикл подій уже запущено (наприклад, у деяких середовищах IDE)
        if "already running" in str(e):
             logger.warning(f"Async setup via asyncio.run failed: Event loop already running. Proceeding with existing loop.")
             # Якщо цикл уже запущений, спробуємо просто викликати load_state
             try:
                 # Створюємо новий цикл і запускаємо load_state
                 loop = asyncio.get_event_loop()
                 loop.run_until_complete(load_state())
             except Exception:
                 logger.exception("Fallback load_state failed.")
        else:
            logger.exception(f"Async setup failed: {e}")
    except Exception as e:
        logger.exception(f"Async setup failed: {e}")

    app = build_app()

    # Інтервал береться з щойно завантаженого стану
    interval = MONITOR_INTERVAL_SECONDS_DEFAULT 
    if app.job_queue:
        app.job_queue.run_repeating(monitor_game_state, interval=interval, first=3, name='monitor_job')
    logger.info("Bot starting. Monitoring interval: %s", interval)

    # Запуск polling (СИНХРОННИЙ, блокуючий метод)
    app.run_polling(poll_interval=1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Shutting down (keyboard interrupt).")
    except Exception:
        logger.exception("An unhandled exception occurred during bot execution.")
