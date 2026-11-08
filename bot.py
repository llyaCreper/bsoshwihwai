# -*- coding: utf-8 -*-
"""
Roblox Age Verification Telegram Bot
v3: оптимизировано для Railway — быстрый отклик, меньше RAM/CPU
"""
import os
import sys

os.environ["PYTHONIOENCODING"] = "utf-8"
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import re
import logging
import requests
import asyncio
from urllib.parse import quote as _url_quote
import time as _time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import aiohttp  # noqa: F401 — для async HTTP запросов
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler,
)

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
logging.info("🤖 Roblox Bot v3 starting — Railway optimized")


def escape_md(text: str) -> str:
    """Экранирует спецсимволы Markdown v1."""
    return re.sub(r'([_*\[\]`\\])', r'\\\1', str(text))


# ══════════════════════════════════════════════════════════════════════
# 🌐 ПРОКСИ — пул резидентных прокси (формат: ip:port:login:password)
# ══════════════════════════════════════════════════════════════════════
# Укажите путь к файлу с прокси (один прокси на строку: ip:port:login:password)
# Или положите файл proxies.txt рядом с bot.py
CONFIG_PROXY_FILE = ""  # Пример: "/app/proxies.txt"

_proxy_pool: list[str] = []
_proxy_lock  = __import__("threading").Lock()
_proxy_index = 0

def _fetch_proxies_from_url(url: str) -> list[str]:
    """
    Загружает список прокси с URL (Telegraph, Pastebin, любой текстовый файл).
    Каждый прокси на отдельной строке.
    Telegraph пример: https://telegra.ph/Moi-proksi-11-07
    """
    try:
        import re as _re
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        text = r.text

        # Telegraph возвращает HTML — извлекаем текст из <p> и <code> тегов
        if "telegra.ph" in url or "<html" in text[:200].lower():
            # Убираем HTML теги
            text = _re.sub(r"<br\s*/?>", "\n", text, flags=_re.IGNORECASE)
            text = _re.sub(r"</p>|</li>|</div>", "\n", text, flags=_re.IGNORECASE)
            text = _re.sub(r"<[^>]+>", "", text)
            # Декодируем HTML entities
            text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'").replace("&quot;", '"')

        proxies = []
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and (":" in line or line.startswith("http")):
                proxies.append(line)
        logging.info("_fetch_proxies_from_url: загружено %d прокси с %s", len(proxies), url)
        return proxies
    except Exception as e:
        logging.error("_fetch_proxies_from_url ошибка (%s): %s", url, e)
        return []


def _load_proxy_pool() -> list[str]:
    """
    Загружает пул прокси из нескольких источников (в порядке приоритета):
      1. PROXY_SOURCE_URL — ссылка на Telegraph/Pastebin/любой текст с прокси
      2. PROXY_URL        — один прокси напрямую
      3. PROXY_LIST       — список через запятую или перенос строки
      4. proxies.txt      — файл рядом со скриптом
    """
    proxies: list[str] = []

    # 1. Загрузка с URL (Telegraph, Pastebin и т.д.)
    source_url = os.environ.get("PROXY_SOURCE_URL", "").strip()
    if source_url:
        logging.info("Загружаю прокси с URL: %s", source_url)
        proxies = _fetch_proxies_from_url(source_url)
        if proxies:
            logging.info("Загружено %d прокси с %s", len(proxies), source_url)
            return proxies
        logging.warning("С URL не загружено ни одного прокси, пробую другие источники")

    # 2. Одиночный прокси из PROXY_URL
    single = os.environ.get("PROXY_URL", "").strip()
    if single:
        logging.info("Используется единственный прокси из PROXY_URL")
        return [single]

    # 3. Список из PROXY_LIST
    env_list = os.environ.get("PROXY_LIST", "").strip()
    if env_list:
        for line in env_list.replace(";", "\n").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                proxies.append(line)
        if proxies:
            logging.info("Загружено %d прокси из PROXY_LIST", len(proxies))
            return proxies

    # 4. Файл proxies.txt
    candidates = []
    if CONFIG_PROXY_FILE:
        candidates.append(CONFIG_PROXY_FILE)
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxies.txt"))
    for path in candidates:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            proxies.append(line)
                if proxies:
                    logging.info("Загружено %d прокси из %s", len(proxies), path)
                    return proxies
            except Exception as e:
                logging.warning("Ошибка чтения %s: %s", path, e)

    if not proxies:
        logging.warning("Прокси не найдены — запросы к Roblox идут напрямую (может блокироваться)")
    return proxies


def _parse_proxy(proxy_str: str) -> dict | None:
    """
    Парсит строку прокси в формат для requests.
    Поддерживает все форматы VProxy и других провайдеров:
      hostname:port:login:password      ← формат VProxy (рекомендуется)
      hostname:port@login:password
      login:password:hostname:port
      login:password@hostname:port
      hostname:port
      socks5://login:pass@hostname:port
      http://login:pass@hostname:port
    Протокол берётся из env PROXY_PROTOCOL (по умолчанию http).
    """
    s = proxy_str.strip()
    if not s:
        return None

    # Протокол: из env или http по умолчанию
    protocol = os.environ.get("PROXY_PROTOCOL", "http").lower().rstrip("://")

    # Формат http://hostname:port:login:password (протокол + 4 части через двоеточие)
    for scheme in ("http://", "https://", "socks5://", "socks4://"):
        if s.startswith(scheme):
            rest = s[len(scheme):]
            rest_parts = rest.split(":")
            # http://host:port:login:password → конвертируем в стандартный URL
            if len(rest_parts) == 4 and rest_parts[1].isdigit():
                host, port, login, password = rest_parts
                url = f"{scheme[:-3]}://{_url_quote(login, safe='')}:{_url_quote(password, safe='')}@{host}:{port}"
                return {"http": url, "https": url}
            # Уже готовый URL-формат (http://login:pass@host:port) — используем как есть
            return {"http": s, "https": s}

    parts = s.split(":")

    if len(parts) == 4:
        a, b, c, d = parts

        # hostname:port:login:password — порт это число во 2й позиции
        if b.isdigit():
            host, port, login, password = a, b, c, d
        # login:password:hostname:port — порт это число в 4й позиции
        elif d.isdigit():
            login, password, host, port = a, b, c, d
        else:
            logging.warning("Неизвестный формат прокси (4 части): %s", s)
            return None

        url = f"{protocol}://{_url_quote(login, safe='')}:{_url_quote(password, safe='')}@{host}:{port}"
        return {"http": url, "https": url}

    # Формат hostname:port@login:password или login:password@hostname:port
    if len(parts) == 3 and "@" in s:
        left, right = s.split("@", 1)
        left_parts  = left.split(":")
        right_parts = right.split(":")
        # hostname:port @ login:password
        if len(left_parts) == 2 and left_parts[1].isdigit():
            host, port = left_parts
            login, password = right_parts[0], right_parts[1] if len(right_parts) > 1 else ""
        # login:password @ hostname:port
        elif len(right_parts) == 2 and right_parts[1].isdigit():
            login, password = left_parts[0], left_parts[1] if len(left_parts) > 1 else ""
            host, port = right_parts
        else:
            logging.warning("Неизвестный формат прокси (@): %s", s)
            return None
        url = f"{protocol}://{_url_quote(login, safe='')}:{_url_quote(password, safe='')}@{host}:{port}"
        return {"http": url, "https": url}

    # Формат hostname:port (без авторизации)
    if len(parts) == 2 and parts[1].isdigit():
        url = f"{protocol}://{s}"
        return {"http": url, "https": url}

    logging.warning("Неизвестный формат прокси: %s", s)
    return None


def get_next_proxy() -> dict | None:
    """Возвращает следующий прокси из пула (round-robin). None если пул пуст."""
    global _proxy_index
    if not _proxy_pool:
        return None
    with _proxy_lock:
        proxy_str = _proxy_pool[_proxy_index % len(_proxy_pool)]
        _proxy_index += 1
    return _parse_proxy(proxy_str)


def _make_roblox_session(cookie: str) -> "requests.Session":
    """Создаёт requests.Session с куки Roblox и, если есть пул, с прокси."""
    s = requests.Session()
    s.cookies[".ROBLOSECURITY"] = cookie
    s.headers.update({
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Content-Type": "application/json;charset=utf-8",
        "Accept":       "application/json, text/plain, */*",
        "Origin":       "https://www.roblox.com",
        "Referer":      "https://www.roblox.com/my/account#!/info",
    })
    proxy = get_next_proxy()
    if proxy:
        s.proxies.update(proxy)
    return s

# ══════════════════════════════════════════════════════════════════════
# 👑 АДМИНИСТРАТОРЫ И НАСТРОЙКИ ПЛАТЕЖЕЙ
# ══════════════════════════════════════════════════════════════════════

# 1. Вставьте сюда ваши Telegram User ID (узнать свой ID можно в боте @userinfobot).
# Можно указать один или несколько:
CONFIG_ADMIN_IDS = [
    "00000000",   # Админ 1
    "0000000",    # Админ 2
    "000000000",  # Админ 3
]

# 2. Токен Crypto Pay из @CryptoBot (Crypto Pay -> Create App -> API Token).
# Если пока нет токена — оставьте пустым ""
CONFIG_CRYPTO_BOT_TOKEN = ""
CONFIG_XROCKET_TOKEN    = ""  # Токен xRocket: @xRocket -> Create App -> API Token

# 3. ID канала для логов (необязательно, например "-1001234567890"). Оставьте "" если не нужен.
CONFIG_LOG_CHANNEL_ID = "-1000000000000"  # Замените на ID вашего канала (начинается с -100)

def load_admin_ids() -> set[int]:
    """Загружает ID администраторов из env ADMIN_IDS, файла admin_ids.txt или CONFIG_ADMIN_IDS."""
    ids: set[int] = set()
    env_admins = os.environ.get("ADMIN_IDS", "").strip()
    if env_admins:
        for x in env_admins.replace(";", ",").split(","):
            x = x.strip()
            if x.isdigit():
                ids.add(int(x))

    admin_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_ids.txt")
    if os.path.exists(admin_file):
        try:
            with open(admin_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        for part in line.replace(";", ",").split(","):
                            part = part.strip()
                            if part.isdigit() and part not in ("0", "00000000", "0000000", "000000000"):
                                ids.add(int(part))
        except Exception:
            pass

    for item in CONFIG_ADMIN_IDS:
        s = str(item).strip()
        if s.isdigit() and s not in ("0", "00000000", "0000000", "000000000"):
            ids.add(int(s))

    return ids

def load_xrocket_token() -> str:
    """Загружает токен xRocket из env или CONFIG_XROCKET_TOKEN."""
    token = os.environ.get("XROCKET_TOKEN", "").strip()
    if token:
        return token
    if CONFIG_XROCKET_TOKEN and CONFIG_XROCKET_TOKEN.strip():
        return CONFIG_XROCKET_TOKEN.strip()
    return ""

def load_crypto_bot_token() -> str:
    """Загружает токен CryptoBot из env, файла crypto_token.txt или CONFIG_CRYPTO_BOT_TOKEN."""
    token = os.environ.get("CRYPTO_BOT_TOKEN", "").strip()
    if token:
        return token

    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crypto_token.txt")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            pass

    if CONFIG_CRYPTO_BOT_TOKEN and CONFIG_CRYPTO_BOT_TOKEN.strip():
        return CONFIG_CRYPTO_BOT_TOKEN.strip()

    return ""

def load_log_channel_id() -> str:
    """Загружает ID канала логов из env, файла log_channel.txt или CONFIG_LOG_CHANNEL_ID."""
    placeholder_ids = {"", "0", "-1000000000000", "-1000000000"}
    cid = os.environ.get("LOG_CHANNEL_ID", "").strip()
    if cid and cid not in placeholder_ids:
        return cid

    file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log_channel.txt")
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line not in placeholder_ids:
                            return line
        except Exception:
            pass

    if CONFIG_LOG_CHANNEL_ID and CONFIG_LOG_CHANNEL_ID.strip():
        val = CONFIG_LOG_CHANNEL_ID.strip()
        if val not in placeholder_ids:
            return val

    return ""

DEFAULT_ADMIN_PASSWORD = "Admin#Roblox2026!SecKey"
ADMIN_PASSWORD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_password.txt")

def get_admin_password() -> str:
    """Загружает пароль администратора из env, файла admin_password.txt или DEFAULT_ADMIN_PASSWORD."""
    env_pass = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_pass:
        return env_pass
    if os.path.exists(ADMIN_PASSWORD_FILE):
        try:
            with open(ADMIN_PASSWORD_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line
        except Exception:
            pass
    return DEFAULT_ADMIN_PASSWORD

def save_admin_password(new_pass: str) -> bool:
    """Сохраняет новый пароль администратора в файл admin_password.txt."""
    try:
        with open(ADMIN_PASSWORD_FILE, "w", encoding="utf-8") as f:
            f.write("# Password\n" + new_pass.strip() + "\n")
        return True
    except Exception as e:
        logging.error("Failed to save admin password: %s", e)
        return False


BOT_TOKEN        = os.environ.get("BOT_TOKEN")
CRYPTO_BOT_TOKEN  = load_crypto_bot_token()
XROCKET_TOKEN     = load_xrocket_token()
DATABASE_URL     = os.environ.get("DATABASE_URL", "")

# Загружаем пул прокси при старте
_proxy_pool = _load_proxy_pool()
if _proxy_pool:
    logging.info("🌐 Пул прокси активен: %d шт.", len(_proxy_pool))
else:
    logging.info("🌐 Прокси не настроены — прямое подключение")

def get_bot_token() -> str:
    """
    Получает BOT_TOKEN (Railway-first):
    1. Из переменной окружения BOT_TOKEN
    2. Из файла bot_token.txt (только для локального запуска)
    """
    token = os.environ.get("BOT_TOKEN", "").strip()
    if token:
        return token
    # Fallback: читаем из файла (только локальный запуск)
    token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_token.txt")
    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                t = f.read().strip()
            if t:
                return t
        except Exception:
            pass
    raise RuntimeError("BOT_TOKEN не задан! Добавьте его в переменные окружения Railway.")

_WORKER_URLS = [
    u.strip() for u in [
        os.environ.get("WORKER_1_URL", ""),
        os.environ.get("WORKER_2_URL", ""),
        os.environ.get("WORKER_3_URL", ""),
        os.environ.get("WORKER_4_URL", ""),
    ] if u.strip()
]
WORKER_SECRET = os.environ.get("WORKER_SECRET", "12345")

_worker_index = 0
_worker_lock  = __import__("threading").Lock()

_worker_health:     dict[str, bool]  = {url: True for url in _WORKER_URLS}
_worker_health_lock = __import__("threading").Lock()
WORKER_HEALTH_TTL   = int(os.environ.get("WORKER_HEALTH_TTL", 30))
_worker_dead_since: dict[str, float] = {}




def _mark_worker(url: str, alive: bool):
    with _worker_health_lock:
        _worker_health[url] = alive
        if alive:
            _worker_dead_since.pop(url, None)
        else:
            _worker_dead_since.setdefault(url, _time.time())


def _worker_considered_alive(url: str) -> bool:
    with _worker_health_lock:
        if _worker_health.get(url, True):
            return True
        dead_since = _worker_dead_since.get(url, 0)
        if (_time.time() - dead_since) >= WORKER_HEALTH_TTL:
            _worker_health[url] = True
            _worker_dead_since.pop(url, None)
            return True
        return False


def get_worker_order() -> list[str]:
    global _worker_index
    if not _WORKER_URLS:
        return []
    with _worker_lock:
        start = _worker_index % len(_WORKER_URLS)
        _worker_index += 1
    ordered = _WORKER_URLS[start:] + _WORKER_URLS[:start]
    alive   = [u for u in ordered if _worker_considered_alive(u)]
    dead    = [u for u in ordered if u not in alive]
    return alive + dead


def _call_single_worker(worker_url: str, cookie: str, method: str, worker_timeout: int) -> "str | dict | None":
    """Вызывает один воркер и возвращает результат или None при ошибке."""
    proxies = {"http": None, "https": None} if ("127.0.0.1" in worker_url or "localhost" in worker_url) else None
    try:
        r = requests.post(
            f"{worker_url}/get-link",
            json={"cookie": cookie, "method": method, "secret": WORKER_SECRET},
            headers={"X-Secret": WORKER_SECRET},
            timeout=worker_timeout,
            proxies=proxies,
        )
        if r.status_code == 200:
            _mark_worker(worker_url, True)
            data = r.json()
            if data.get("link"):
                logging.info("Воркер %s вернул ссылку", worker_url)
                return data["link"]
            if data.get("challengeId") and data.get("csrf"):
                logging.info("Воркер %s вернул challenge", worker_url)
                outer = data["challengeId"]
                inner = data.get("innerChallengeId") or outer
                return {
                    "challengeId":      outer,
                    "innerChallengeId": inner,
                    "csrf":             data["csrf"],
                    "emailSent":        data.get("emailSent", False),
                }
            logging.warning("Воркер %s вернул 200, но нет link/challenge: %s", worker_url, r.text[:200])
            return None
        if r.status_code == 503:
            _mark_worker(worker_url, False)
            logging.warning("Воркер %s занят (503)", worker_url)
            return None
        logging.warning("Воркер %s вернул ошибку HTTP %s: %s", worker_url, r.status_code, r.text[:200])
        return None
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        _mark_worker(worker_url, False)
        logging.warning("Воркер %s недоступен (%s): %s", worker_url, type(e).__name__, e)
        return None
    except Exception as e:
        logging.warning("Воркер %s ошибка: %s", worker_url, e)
        return None


def call_worker_2fa(cookie: str, method: str) -> "str | dict | None":
    """
    Вызывает первый доступный живой воркер последовательно.
    Для 2FA параллельный запуск недопустим, т.к. разные воркеры отправят разные коды
    и инвалидируют challengeId сессии.
    Возвращает:
      - str:  готовая ссылка withpersona.com
      - dict: {challengeId, csrf, emailSent} — нужен email-код от пользователя
      - None: все воркеры недоступны
    """
    order = get_worker_order()
    if not order:
        return get_url_via_api_2fa(cookie, method)

    worker_timeout = int(os.environ.get("WORKER_TIMEOUT", 620))
    logging.info("Опрашиваем воркеры по очереди: %s", order)

    for worker_url in order:
        logging.info("Отправка задачи на воркер %s", worker_url)
        res = _call_single_worker(worker_url, cookie, method, worker_timeout)
        if res is not None:
            return res

    logging.warning("Ни один воркер не ответил — переключаемся на прямой fallback бота (get_url_via_api_2fa)")
    return get_url_via_api_2fa(cookie, method)


def call_worker_2fa_sequential(cookie: str, method: str) -> "str | dict | None":
    """Резервный последовательный вызов — используется если parallel не нужен."""
    order = get_worker_order()
    if not order:
        return get_url_via_api_2fa(cookie, method)

    worker_timeout = int(os.environ.get("WORKER_TIMEOUT", 620))
    for worker_url in order:
        res = _call_single_worker(worker_url, cookie, method, worker_timeout)
        if res is not None:
            return res
    return None


ADMIN_IDS: set[int] = load_admin_ids()
LOG_CHANNEL_ID: str = load_log_channel_id()
CRYPTO_BOT_TOKEN: str  = load_crypto_bot_token()
XROCKET_TOKEN:    str  = load_xrocket_token()

executor = ThreadPoolExecutor(max_workers=8)

# ──────────────────────────── SERVICE MODE FLAGS ────────────────────────────
# Управление режимами через админ-панель (сбрасываются при перезапуске бота).
# True = режим включён для пользователей.
SERVICE_NO2FA_ENABLED: bool = True   # Без 2FA (бесплатно)
SERVICE_2FA_ENABLED:   bool = True   # С 2FA (платно)
pending_payments: dict[str, dict] = {}

# ──────────────────────────── STATES ────────────────────────────

WAITING_LANG                        = 0
WAITING_MENU                        = 1
WAITING_COOKIE                      = 2
WAITING_BUY                         = 3
WAITING_PAYMENT                     = 4
WAITING_PROMO                       = 5
WAITING_ADMIN                       = 6
WAITING_ADMIN_PASSWORD              = 7
WAITING_ADMIN_ADD_BALANCE           = 8
WAITING_ADMIN_ADD_BALANCE_AMOUNT    = 9
WAITING_ADMIN_CREATE_PROMO          = 10
WAITING_ADMIN_ADD_SUB               = 11
WAITING_ADMIN_REMOVE_BALANCE        = 12
WAITING_ADMIN_REMOVE_BALANCE_AMOUNT = 13
WAITING_ADMIN_BROADCAST             = 14
WAITING_2FA_CODE                    = 15
WAITING_ADMIN_CHANGE_PASSWORD       = 16

PRICE_2FA     = 3.00
DISCOUNTS_2FA = {5: 0.10, 10: 0.15, 25: 0.25}
SUBSCRIPTIONS = {1: 3.00, 3: 5.00, 7: 10.00, 30: 25.00, 0: 50.00}

# ──────────────────────────── DATABASE (connection pool) ────────────────────────────

try:
    import psycopg2
    import psycopg2.pool
    import psycopg2.extras
    _DB_OP_ERR = psycopg2.OperationalError
except ImportError:
    psycopg2 = None
    _DB_OP_ERR = Exception
from contextlib import contextmanager

import sqlite3

# Автоматический выбор БД:
# Если DATABASE_URL задан (PostgreSQL на сервере / Railway) -> используем psycopg2
# Если DATABASE_URL пуст (локальный запуск) -> автоматически используем SQLite (bot.db)
_USE_SQLITE = not bool(DATABASE_URL)
_SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
_SQLITE_LOCK = __import__("threading").Lock()

class _SQLiteCursorWrapper:
    def __init__(self, cursor):
        self._c = cursor
    def execute(self, sql, params=None):
        sql = sql.replace("%s", "?")
        if "ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'ru'" in sql:
            try:
                return self._c.execute("ALTER TABLE users ADD COLUMN lang TEXT DEFAULT 'ru'")
            except Exception:
                return self._c
        if params is not None:
            return self._c.execute(sql, params)
        return self._c.execute(sql)
    def fetchone(self):
        return self._c.fetchone()
    def fetchall(self):
        return self._c.fetchall()
    @property
    def rowcount(self):
        return self._c.rowcount

class _SQLiteConnWrapper:
    def __init__(self, conn):
        self._conn = conn
    def cursor(self):
        return _SQLiteCursorWrapper(self._conn.cursor())
    def commit(self):
        self._conn.commit()
    def rollback(self):
        self._conn.rollback()

_sqlite_conn = None

def _get_sqlite_conn():
    global _sqlite_conn
    if _sqlite_conn is None:
        _sqlite_conn = sqlite3.connect(_SQLITE_PATH, check_same_thread=False)
        _sqlite_conn.create_function("GREATEST", 2, max)
    return _sqlite_conn

_DB_POOL: "psycopg2.pool.ThreadedConnectionPool | None" = None
_DB_POOL_LOCK = __import__("threading").Lock()

def _get_pool():
    global _DB_POOL
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Run 'pip install psycopg2-binary' to use database.")
    if _DB_POOL is None:
        with _DB_POOL_LOCK:
            if _DB_POOL is None:
                _DB_POOL = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=5,
                    dsn=DATABASE_URL,
                    connect_timeout=10,
                )
    return _DB_POOL


@contextmanager
def _db():
    if _USE_SQLITE:
        with _SQLITE_LOCK:
            conn = _get_sqlite_conn()
            try:
                yield _SQLiteConnWrapper(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    else:
        pool = _get_pool()
        conn = pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            pool.putconn(conn)


def _db_execute_with_retry(fn, retries: int = 3):
    """Оборачивает DB-операцию в retry-логику при transient errors."""
    last_exc = None
    for attempt in range(retries):
        try:
            return fn()
        except _DB_OP_ERR as e:
            last_exc = e
            logging.warning("DB OperationalError (attempt %d/%d): %s", attempt + 1, retries, e)
            # Сбрасываем пул при ошибке соединения
            global _DB_POOL
            with _DB_POOL_LOCK:
                if _DB_POOL is not None:
                    try:
                        _DB_POOL.closeall()
                    except Exception:
                        pass
                    _DB_POOL = None
            _time.sleep(0.5 * (attempt + 1))
        except Exception as e:
            raise e
    raise last_exc


def db_init():
    with _db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                attempts INTEGER DEFAULT 0,
                spent REAL DEFAULT 0.0,
                lang TEXT DEFAULT 'ru'
            )
        """)
        c.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT DEFAULT 'ru'
        """)
        c.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_earned REAL DEFAULT 0.0
        """)
        c.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS ref_given INTEGER DEFAULT 0
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                attempts INTEGER DEFAULT 1,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER DEFAULT 1
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS used_promos (
                user_id BIGINT,
                code TEXT,
                PRIMARY KEY (user_id, code)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id BIGINT PRIMARY KEY,
                expires_at TEXT,
                is_forever INTEGER DEFAULT 0
            )
        """)
        # Индексы для ускорения частых запросов
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_users_spent ON users(spent DESC)
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_used_promos_uid ON used_promos(user_id)
        """)
        # Реферальная система
        c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referred_id  BIGINT PRIMARY KEY,
                referrer_id  BIGINT NOT NULL
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT 'true'
            )
        """)


def db_get_user(user_id):
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT username, attempts, spent FROM users WHERE user_id=%s", (user_id,))
            return c.fetchone()
    return _db_execute_with_retry(_fn)


def db_upsert_user(user_id, username, attempts_delta=0, spent_delta=0.0):
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO users (user_id, username, attempts, spent)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET username=EXCLUDED.username,
                    attempts=GREATEST(0, users.attempts + %s),
                    spent=GREATEST(0.0, users.spent + %s)
            """, (user_id, username, max(0, attempts_delta), max(0.0, spent_delta), attempts_delta, spent_delta))
    _db_execute_with_retry(_fn)


def db_get_attempts(user_id) -> int:
    row = db_get_user(user_id)
    return row[1] if row else 0


def db_deduct_attempt(user_id, username) -> bool:
    """
    Атомарно списывает 1 попытку.
    Возвращает True если попытка была и успешно списана.
    Один UPDATE вместо SELECT + UPDATE — защита от race condition.
    """
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE users
                SET attempts = attempts - 1,
                    spent    = GREATEST(0.0, spent + %s)
                WHERE user_id = %s AND attempts > 0
            """, (PRICE_2FA, user_id))
            if c.rowcount == 0:
                return False
            c.execute("UPDATE users SET username = %s WHERE user_id = %s", (username, user_id))
            return True
    return _db_execute_with_retry(_fn)


def db_get_all_users():
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, username, attempts, spent FROM users ORDER BY spent DESC")
            return c.fetchall()
    return _db_execute_with_retry(_fn)


def db_get_top(limit=10):
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute(
                "SELECT user_id, username, spent FROM users WHERE spent > 0 ORDER BY spent DESC LIMIT %s",
                (limit,),
            )
            return c.fetchall()
    return _db_execute_with_retry(_fn)


def db_create_promo(code, attempts, max_uses):
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO promo_codes (code, attempts, uses, max_uses)
                VALUES (%s, %s, 0, %s)
                ON CONFLICT (code) DO UPDATE
                SET attempts=EXCLUDED.attempts, max_uses=EXCLUDED.max_uses
            """, (code, attempts, max_uses))
    _db_execute_with_retry(_fn)


def db_use_promo(user_id, code, username=""):
    """
    Атомарно проверяет промокод и начисляет попытки.
    UPDATE с условием uses < max_uses вместо SELECT→check→UPDATE (защита от TOCTOU race).
    """
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT attempts, max_uses FROM promo_codes WHERE code=%s", (code,))
            row = c.fetchone()
            if not row:
                return False, "not_found", 0
            attempts, max_uses = row
            c.execute("SELECT 1 FROM used_promos WHERE user_id=%s AND code=%s", (user_id, code))
            if c.fetchone():
                return False, "used_by_you", 0
            c.execute(
                "UPDATE promo_codes SET uses = uses + 1 WHERE code = %s AND uses < max_uses",
                (code,),
            )
            if c.rowcount == 0:
                return False, "used", 0
            c.execute("INSERT INTO used_promos (user_id, code) VALUES (%s, %s)", (user_id, code))
            c.execute("""
                INSERT INTO users (user_id, username, attempts, spent)
                VALUES (%s, %s, %s, 0.0)
                ON CONFLICT (user_id) DO UPDATE
                SET attempts=GREATEST(0, users.attempts + %s)
            """, (user_id, username, max(0, attempts), attempts))
            return True, "ok", attempts
    return _db_execute_with_retry(_fn)


def db_get_all_promos():
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT code, attempts, uses, max_uses FROM promo_codes")
            return c.fetchall()
    return _db_execute_with_retry(_fn)


def db_get_subscription(user_id):
    import datetime

    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT expires_at, is_forever FROM subscriptions WHERE user_id=%s", (user_id,))
            return c.fetchone()

    row = _db_execute_with_retry(_fn)
    if not row:
        return False, None, False
    expires_at_str, is_forever = row
    if is_forever:
        return True, "Навсегда", True
    if expires_at_str:
        try:
            exp = datetime.datetime.fromisoformat(expires_at_str)
            now = datetime.datetime.now(datetime.timezone.utc)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=datetime.timezone.utc)
            if exp > now:
                return True, expires_at_str, False
        except Exception:
            pass
    return False, expires_at_str, False


def db_set_subscription(user_id, days):
    import datetime

    def _fn():
        with _db() as conn:
            c = conn.cursor()
            if days == 0:
                c.execute("""
                    INSERT INTO subscriptions (user_id, expires_at, is_forever)
                    VALUES (%s, NULL, 1)
                    ON CONFLICT (user_id) DO UPDATE SET expires_at=NULL, is_forever=1
                """, (user_id,))
            else:
                c.execute("SELECT expires_at, is_forever FROM subscriptions WHERE user_id=%s", (user_id,))
                existing = c.fetchone()
                now = datetime.datetime.now(datetime.timezone.utc)
                if existing and not existing[1] and existing[0]:
                    try:
                        exp = datetime.datetime.fromisoformat(existing[0])
                        if exp.tzinfo is None:
                            exp = exp.replace(tzinfo=datetime.timezone.utc)
                        if exp > now:
                            now = exp
                    except Exception:
                        pass
                new_exp = (now + datetime.timedelta(days=days)).isoformat()
                c.execute("""
                    INSERT INTO subscriptions (user_id, expires_at, is_forever)
                    VALUES (%s, %s, 0)
                    ON CONFLICT (user_id) DO UPDATE SET expires_at=%s, is_forever=0
                """, (user_id, new_exp, new_exp))

    _db_execute_with_retry(_fn)


def db_get_lang(user_id) -> str | None:
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT lang FROM users WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            return row[0] if row else None
    return _db_execute_with_retry(_fn)


def db_set_lang(user_id, lang):
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO users (user_id, lang) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET lang=EXCLUDED.lang
            """, (user_id, lang))
    _db_execute_with_retry(_fn)


def db_is_new_user(user_id) -> bool:
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,))
            return c.fetchone() is None
    return _db_execute_with_retry(_fn)

# ──────────────────────────── ROBLOX VERIFICATION BUTTONS ────────────────────────────

VERIFY_BUTTON_TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "continue": "▶️ Продолжить",
        "camera":   "▶️ Продолжить с камерой",
        "id":       "▶️ Продолжить с ID",
    },
    "en": {
        "continue": "▶️ Continue",
        "camera":   "▶️ Continue with camera",
        "id":       "▶️ Continue with ID",
    },
    "es": {
        "continue": "▶️ Continuar",
        "camera":   "▶️ Continuar con la cámara",
        "id":       "▶️ Continuar con ID",
    },
    "pt": {
        "continue": "▶️ Continuar",
        "camera":   "▶️ Continuar com a câmera",
        "id":       "▶️ Continuar com ID",
    },
    "de": {
        "continue": "▶️ Weiter",
        "camera":   "▶️ Mit Kamera fortfahren",
        "id":       "▶️ Mit ID fortfahren",
    },
    "fr": {
        "continue": "▶️ Continuer",
        "camera":   "▶️ Continuer avec la caméra",
        "id":       "▶️ Continuer avec l'ID",
    },
    "it": {
        "continue": "▶️ Continua",
        "camera":   "▶️ Continua con la fotocamera",
        "id":       "▶️ Continua con ID",
    },
    "tr": {
        "continue": "▶️ Devam Et",
        "camera":   "▶️ Kamera ile devam et",
        "id":       "▶️ Kimlik ile devam et",
    },
    "pl": {
        "continue": "▶️ Kontynuuj",
        "camera":   "▶️ Kontynuuj z kamerą",
        "id":       "▶️ Kontynuuj z ID",
    },
    "id": {
        "continue": "▶️ Lanjutkan",
        "camera":   "▶️ Lanjutkan dengan kamera",
        "id":       "▶️ Lanjutkan dengan ID",
    },
    "vi": {
        "continue": "▶️ Tiếp tục",
        "camera":   "▶️ Tiếp tục bằng camera",
        "id":       "▶️ Tiếp tục bằng ID",
    },
    "ja": {
        "continue": "▶️ 続行",
        "camera":   "▶️ カメラで続行",
        "id":       "▶️ IDで続行",
    },
    "ko": {
        "continue": "▶️ 계속",
        "camera":   "▶️ 카메라로 계속",
        "id":       "▶️ ID로 계속",
    },
    "zh": {
        "continue": "▶️ 继续",
        "camera":   "▶️ 使用摄像头继续",
        "id":       "▶️ 使用ID继续",
    },
    "th": {
        "continue": "▶️ ดำเนินการต่อ",
        "camera":   "▶️ ดำเนินการต่อด้วยกล้อง",
        "id":       "▶️ ดำเนินการต่อด้วย ID",
    },
    "ar": {
        "continue": "▶️ متابعة",
        "camera":   "▶️ متابعة باستخدام الكاميرا",
        "id":       "▶️ متابعة باستخدام ID",
    },
    "uk": {
        "continue": "▶️ Продовжити",
        "camera":   "▶️ Продовжити з камерою",
        "id":       "▶️ Продовжити з ID",
    },
    "fil": {
        "continue": "▶️ Magpatuloy",
        "camera":   "▶️ Magpatuloy gamit ang camera",
        "id":       "▶️ Magpatuloy gamit ang ID",
    },
    "hi": {
        "continue": "▶️ जारी रखें",
        "camera":   "▶️ कैमरे के साथ जारी रखें",
        "id":       "▶️ आईडी के साथ जारी रखें",
    },
}

def get_continue_button_text(method: str | None, lang: str | None = None) -> str:
    """
    Возвращает текст кнопки для ссылки Persona с переводом на все поддерживаемые языки Roblox:
      - Continue (общий)
      - Continue with camera (метод camera)
      - Continue with ID (метод id)
    """
    l = (lang or "ru").lower().strip()
    if "-" in l:
        l = l.split("-")[0]
    if "_" in l:
        l = l.split("_")[0]
    trans = VERIFY_BUTTON_TRANSLATIONS.get(l, VERIFY_BUTTON_TRANSLATIONS.get("en", VERIFY_BUTTON_TRANSLATIONS["ru"]))
    
    m = (method or "camera").lower().strip()
    if m == "camera":
        return trans.get("camera", "▶️ Continue with camera")
    elif m == "id":
        return trans.get("id", "▶️ Continue with ID")
    else:
        return trans.get("continue", "▶️ Continue")

# ──────────────────────────── TEXTS ────────────────────────────

TEXTS = {
    "ru": {
        "main_menu_title":   "*Roblox Age Verification Bot*",
        "main_menu_sub":     "Сервис для получения ссылки by @dedbed12",
        "main_menu_attempts":"🔐 Попытки 2FA: *{attempts}* | {sub_status}",
        "main_menu_action":  "Выберите действие ниже:",
        "btn_get_link":      "Получить ссылку",
        "btn_buy":           "Купить попытки",
        "btn_promo":         "Промокод",
        "btn_top":           "Топ пользователей",
        "btn_help":          "Помощь",
        "btn_support":       "Саппорт",
        "btn_admin":         "Админ панель",
        "btn_back":          "Назад",
        "btn_cancel":        "Отмена",
        "btn_buy_5":         "5 попыток — $13.50 (−10%)",
        "btn_buy_10":        "10 попыток — $25.50 (−15%)",
        "btn_buy_25":        "25 попыток — $56.25 (−25%)",
        "btn_sub_1d":        "📅 1 день — $3.00",
        "btn_sub_3d":        "📅 3 дня — $5.00",
        "btn_sub_7d":        "📅 7 дней — $10.00",
        "btn_sub_30d":       "📅 30 дней — $25.00",
        "btn_sub_inf":       "♾️ Навсегда — $50.00",
        "btn_buy_attempts":  "🎯 Попытки 2FA",
        "btn_buy_sub":       "♾️ Подписка (безлимит)",
        "sub_active":        "✅ Подписка активна до: *{expires}*",
        "sub_active_forever":"✅ Подписка: *Навсегда*",
        "sub_none":          "❌ Подписки нет",
        "sub_bought":        "✅ Подписка активирована!\n{desc}",
        "help_text": (
            "📖 *Инструкция*\n\n"
            "┌─ 📷 *Ссылка Camera (Face)* — $1.0\n"
            "│   • Persona\\-ссылка для селфи\n"
            "│   • Метод: прямая ссылка\n"
            "└─ • Время: 30 секунд\n\n"
            "┌─ 🪪 *Ссылка ID (Паспорт)* — $1.0\n"
            "│   • Persona\\-ссылка для документов\n"
            "│   • Метод: прямая ссылка\n"
            "└─ • Время: 30 секунд\n\n"
            "⚠️ *Важно*\n"
            "• Дублируйте кук или убирайте IP\\-lock\n"
            "• Верифицированные аккаунты не подходят\n"
            "• Если вы не хотите ломание куков — бот и владелец не советует фрешать куки\n\n"
            "📌 Поддержка: @dedbed12"
        ),
        "top_title":         "*Топ пользователей*\n",
        "top_empty":         "Пока никто не совершил покупок.",
        "buy_title":         "*Магазин*",
        "buy_price":         "🔓 Без 2FA — *БЕСПЛАТНО*\n🔐 С 2FA — *$1.00* за попытку",
        "promo_title":       "*Активация промокода*",
        "promo_enter":       "Введите промокод:",
        "promo_ok":          "Промокод активирован! +*{attempts}* попыток\nВсего: *{total}*",
        "promo_not_found":   "Промокод не найден!",
        "promo_used":        "Промокод уже использован!",
        "promo_used_by_you": "Вы уже использовали этот промокод!",
        "checking_cookie":   "Проверяю cookie...",
        "invalid_cookie":    "Неверный cookie! Попробуй снова.\n\n/start",
        "processing":        "Аккаунт: *{name}*\nМетод: *{method}*\nОсталось попыток: *{attempts}*\n\nЖди, получаю ссылку...",
        "link_received":     "*Ссылка получена!*\n\n⏱ Действует *10 минут* — используй сразу!\n🔗 Одноразовая ссылка:",
        "failed_returned":   "❌ Не удалось получить ссылку — попытка возвращена!",
        "link_received_free_2fa": "*Ссылка получена (без 2FA)*!\n\nПопытка возвращена на баланс (бесплатно).\n\n⏱ Действует *10 минут* — используй сразу!\n🔗 Одноразовая ссылка:",
        "need_2fa_mode":     "⚠️ *На этом аккаунте включена 2FA!*\n\nПожалуйста, выберите в меню: *🔐 С 2FA*.",
        "method_camera":     "Camera",
        "method_id":         "ID",
        "choose_method":     "*Выберите метод верификации:*",
        "btn_camera":        "📷 Camera (Лицо)",
        "btn_id":            "🪪 ID (Паспорт)",
        "invoice_created":   "*Счёт создан!*\n\nСумма: *${amount}*\nПопыток: *{attempts}*",
        "btn_check_pay":     "✅ Проверить оплату",
        "btn_pay":           "💳 Оплатить",
        "choose_payment":    "💳 *Выберите способ оплаты:*\n\nСумма: *${amount}*",
        "btn_pay_crypto":    "🤖 CryptoBot",
        "btn_pay_xrocket":   "🚀 xRocket",
        "pay_method_unavail":"❌ Способ оплаты недоступен. Обратитесь в поддержку.",
        "pay_ok":            "*Оплата получена!*\nНачислено *{attempts}* попыток\nВсего: *{total}*",
        "pay_pending":       "Оплата ещё не получена. Попробуй позже.",
        "pay_error":         "Ошибка создания счёта!",
        "lang_set":          "✅ Язык установлен: Русский",
        "ask_2fa":           "На каком аккаунте получить ссылку?",
        "btn_no_2fa":        "🔓 Без 2FA",
        "btn_with_2fa":      "🔐 С 2FA",
        "2fa_ask_cookie":    "🔐 *Режим 2FA*\n\nОтправь `.ROBLOSECURITY` cookie",
        "no2fa_ask_cookie":  "🔓 *Режим БЕЗ 2FA*\n\nОтправь `.ROBLOSECURITY` cookie",
        "choose_lang":       "🌐 Выберите язык / Choose language:",
        "btn_referral":      "👥 Рефералы",
        "ref_menu_title":    "*👥 Реферальная программа*",
        "ref_menu_body":     "За каждую оплаченную 2FA-ссылку твоего реферала ты получаешь *20%* ($0.20).\nКаждый полный *$1.00* автоматически превращается в *1 попытку 2FA*.\n\n👤 Рефералов: *{count}*\n💰 Заработано всего: *${earned:.2f}*\n\n🔗 Твоя реферальная ссылка:\n`{ref_link}`",
        "ref_no_bot":        "Не удалось получить имя бота.",
    },
    "en": {
        "main_menu_title":   "*Roblox Age Verification Bot*",
        "main_menu_sub":     "Service for getting verification link by @dedbed12",
        "main_menu_attempts":"🔐 2FA Attempts: *{attempts}* | {sub_status}",
        "main_menu_action":  "Choose an action below:",
        "btn_get_link":      "Get link",
        "btn_buy":           "Buy attempts",
        "btn_promo":         "Promo code",
        "btn_top":           "Top users",
        "btn_help":          "Help",
        "btn_support":       "Support",
        "btn_admin":         "Admin panel",
        "btn_back":          "Back",
        "btn_cancel":        "Cancel",
        "btn_buy_5":         "5 attempts — $13.50 (−10%)",
        "btn_buy_10":        "10 attempts — $25.50 (−15%)",
        "btn_buy_25":        "25 attempts — $56.25 (−25%)",
        "btn_sub_1d":        "📅 1 day — $3.00",
        "btn_sub_3d":        "📅 3 days — $5.00",
        "btn_sub_7d":        "📅 7 days — $10.00",
        "btn_sub_30d":       "📅 30 days — $25.00",
        "btn_sub_inf":       "♾️ Forever — $50.00",
        "btn_buy_attempts":  "🎯 2FA Attempts",
        "btn_buy_sub":       "♾️ Subscription (unlimited)",
        "sub_active":        "✅ Subscription active until: *{expires}*",
        "sub_active_forever":"✅ Subscription: *Forever*",
        "sub_none":          "❌ No subscription",
        "sub_bought":        "✅ Subscription activated!\n{desc}",
        "help_text": (
            "📖 *Instructions*\n\n"
            "┌─ 📷 *Camera Link (Face)* — $1.0\n"
            "│   • Persona link for selfie\n"
            "│   • Method: direct link\n"
            "└─ • Time: 30 seconds\n\n"
            "┌─ 🪪 *ID Link (Passport)* — $1.0\n"
            "│   • Persona link for documents\n"
            "│   • Method: direct link\n"
            "└─ • Time: 30 seconds\n\n"
            "⚠️ *Important*\n"
            "• Duplicate the cookie or remove IP\\-lock\n"
            "• Verified accounts are not suitable\n"
            "• If you don't want cookies to break — the bot and owner advise against refreshing cookies\n\n"
            "📌 Support: @dedbed12"
        ),
        "top_title":         "*Top users*\n",
        "top_empty":         "No purchases yet.",
        "buy_title":         "*Shop*",
        "buy_price":         "🔓 Without 2FA — *FREE*\n🔐 With 2FA — *$1.00* per attempt",
        "promo_title":       "*Promo code activation*",
        "promo_enter":       "Enter promo code:",
        "promo_ok":          "Promo activated! +*{attempts}* attempts\nTotal: *{total}*",
        "promo_not_found":   "Promo code not found!",
        "promo_used":        "Promo code already used!",
        "promo_used_by_you": "You have already used this promo code!",
        "checking_cookie":   "Checking cookie...",
        "invalid_cookie":    "Invalid cookie! Try again.\n\n/start",
        "processing":        "Account: *{name}*\nMethod: *{method}*\nAttempts left: *{attempts}*\n\nPlease wait, obtaining link...",
        "link_received":     "*Link received!*\n\n⏱ Valid for *10 minutes* — use it now!\n🔗 One-time link:",
        "failed_returned":   "❌ Failed to get link — attempt returned!",
        "link_received_free_2fa": "*Link received (No 2FA)*!\n\nAttempt refunded to your balance (free).\n\n⏱ Valid for *10 minutes* — use it now!\n🔗 One-time link:",
        "need_2fa_mode":     "⚠️ *2FA is enabled on this account!*\n\nPlease select in menu: *🔐 With 2FA*.",
        "method_camera":     "Camera",
        "method_id":         "ID",
        "choose_method":     "*Choose verification method:*",
        "btn_camera":        "📷 Camera (Face)",
        "btn_id":            "🪪 ID (Passport)",
        "invoice_created":   "*Invoice created!*\n\nAmount: *${amount}*\nAttempts: *{attempts}*",
        "btn_check_pay":     "✅ Check payment",
        "btn_pay":           "💳 Pay",
        "choose_payment":    "💳 *Choose payment method:*\n\nAmount: *${amount}*",
        "btn_pay_crypto":    "🤖 CryptoBot",
        "btn_pay_xrocket":   "🚀 xRocket",
        "pay_method_unavail":"❌ Payment method unavailable. Contact support.",
        "pay_ok":            "*Payment received!*\nAdded *{attempts}* attempts\nTotal: *{total}*",
        "pay_pending":       "Payment not received yet. Try later.",
        "pay_error":         "Invoice creation error!",
        "lang_set":          "✅ Language set: English",
        "ask_2fa":           "Which account type do you want to verify?",
        "btn_no_2fa":        "🔓 Without 2FA",
        "btn_with_2fa":      "🔐 With 2FA",
        "2fa_ask_cookie":    "🔐 *2FA Mode*\n\nSend your `.ROBLOSECURITY` cookie",
        "no2fa_ask_cookie":  "🔓 *No 2FA Mode*\n\nSend your `.ROBLOSECURITY` cookie",
        "choose_lang":       "🌐 Выберите язык / Choose language:",
        "btn_referral":      "👥 Referrals",
        "ref_menu_title":    "*👥 Referral Program*",
        "ref_menu_body":     "You earn *20%* ($0.20) for every paid 2FA link your referral gets.\nEvery full *$1.00* is automatically converted to *1 attempt*.\n\n👤 Referrals: *{count}*\n💰 Total earned: *${earned:.2f}*\n\n🔗 Your referral link:\n`{ref_link}`",
        "ref_no_bot":        "Could not get bot username.",
    },
}


# ──────────────────────────── REFERRAL DB HELPERS ────────────────────────────

REFERRAL_PERCENT = 0.20   # 20% от стоимости 2FA попытки рефереру

def db_set_referral(referred_id: int, referrer_id: int) -> bool:
    """Привязывает нового пользователя к рефереру. Возвращает True если привязка новая."""
    if referred_id == referrer_id:
        return False
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute(
                """INSERT INTO referrals (referred_id, referrer_id)
                   VALUES (%s, %s)
                   ON CONFLICT (referred_id) DO NOTHING""",
                (referred_id, referrer_id),
            )
            return (c.rowcount or 0) > 0
    try:
        return _db_execute_with_retry(_fn)
    except Exception as e:
        logging.error("db_set_referral error: %s", e)
        return False

def db_get_referrer(user_id: int) -> int | None:
    """Возвращает ID реферера пользователя или None."""
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT referrer_id FROM referrals WHERE referred_id=%s", (user_id,))
            row = c.fetchone()
            return row[0] if row else None
    try:
        return _db_execute_with_retry(_fn)
    except Exception as e:
        logging.error("db_get_referrer error: %s", e)
        return None

def db_get_setting(key: str, default: str = "true") -> str:
    try:
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT value FROM bot_settings WHERE key=%s", (key,))
            row = c.fetchone()
            return row[0] if row else default
    except Exception:
        return default

def db_set_setting(key: str, value: str):
    try:
        with _db() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO bot_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=%s",
                (key, value, value)
            )
    except Exception as e:
        logging.error("db_set_setting: %s", e)

def db_get_service_flags() -> tuple[bool, bool]:
    no2fa = db_get_setting("service_no2fa", "true").lower() == "true"
    fa2   = db_get_setting("service_2fa",   "true").lower() == "true"
    return no2fa, fa2

def db_get_referral_stats(user_id: int) -> tuple[int, float]:
    """Возвращает (кол-во рефералов, суммарный заработок реферера)."""
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=%s", (user_id,))
            count = c.fetchone()[0] or 0
            c.execute("SELECT ref_earned FROM users WHERE user_id=%s", (user_id,))
            row = c.fetchone()
            earned = float(row[0]) if row else 0.0
            return count, earned
    try:
        return _db_execute_with_retry(_fn)
    except Exception as e:
        logging.error("db_get_referral_stats error: %s", e)
        return 0, 0.0

def db_add_ref_reward(referrer_id: int, amount: float):
    """Начисляет рефереру reward: добавляет попытку каждые $1.00 накопленного заработка."""
    def _fn():
        with _db() as conn:
            c = conn.cursor()
            # Обновляем ref_earned
            c.execute(
                """INSERT INTO users (user_id, username, attempts, spent, ref_earned)
                   VALUES (%s, '', 0, 0.0, %s)
                   ON CONFLICT (user_id) DO UPDATE
                   SET ref_earned = COALESCE(users.ref_earned, 0.0) + EXCLUDED.ref_earned""",
                (referrer_id, amount),
            )
            # Выдаём попытки: 1 попытка за каждый полный $1.00
            c.execute("SELECT ref_earned, ref_given FROM users WHERE user_id=%s", (referrer_id,))
            row = c.fetchone()
            if not row:
                return 0
            earned = float(row[0] or 0)
            given  = int(row[1] or 0)
            new_attempts = int(earned) - given
            if new_attempts > 0:
                c.execute(
                    """UPDATE users SET attempts = GREATEST(0, attempts + %s), ref_given = COALESCE(ref_given,0) + %s
                       WHERE user_id=%s""",
                    (new_attempts, new_attempts, referrer_id),
                )
                return new_attempts
            return 0
    try:
        return _db_execute_with_retry(_fn)
    except Exception as e:
        logging.error("db_add_ref_reward error: %s", e)
        return 0

def t(user_id, key, lang_override=None, **kwargs) -> str:
    lang = lang_override or db_get_lang(user_id) or "ru"
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, TEXTS["ru"].get(key, key))
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError) as e:
            logging.error("t() format error: key=%s, kwargs=%s, err=%s", key, kwargs, e)
    return text

# ──────────────────────────── HELPERS ────────────────────────────

def get_url_via_api(cookie: str, method: str) -> str | None:
    """Прямой вызов API для аккаунтов без 2FA."""
    try:
        s = _make_roblox_session(cookie)
        url  = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
        body = (
            {"generateLink": True, "ageEstimation": False, "parentVerification": False}
            if method == "id"
            else {"generateLink": True, "ageEstimation": True, "parentVerification": False}
        )
        r1   = s.post(url, json=body, timeout=12)
        csrf = r1.headers.get("x-csrf-token")
        if r1.status_code == 200:
            link = _extract_link(r1)
            if link:
                return link
        if csrf:
            s.headers["x-csrf-token"] = csrf
            r2 = s.post(url, json=body, timeout=12)
            if r2.status_code == 200:
                return _extract_link(r2)
        return None
    except Exception as e:
        logging.error("API error: %s", e)
        return None


def get_url_via_api_2fa(cookie: str, method: str) -> "str | dict | None":
    """
    Автономный вызов 2FA без воркеров (прямо из бота).
    Если аккаунт требует 2FA, отправляет код на email и возвращает challenge dict.
    """
    import base64 as _b64
    import json as _json

    try:
        s = _make_roblox_session(cookie)
        url = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
        body = (
            {"generateLink": True, "ageEstimation": False, "parentVerification": False}
            if method == "id"
            else {"generateLink": True, "ageEstimation": True, "parentVerification": False}
        )

        r1 = s.post(url, json=body, timeout=12)
        csrf = r1.headers.get("x-csrf-token")
        if csrf:
            s.headers["x-csrf-token"] = csrf

        if r1.status_code == 200:
            link = _extract_link(r1)
            if link:
                return link

        r2 = s.post(url, json=body, timeout=12)
        csrf2 = r2.headers.get("x-csrf-token") or csrf
        if csrf2:
            s.headers["x-csrf-token"] = csrf2

        if r2.status_code == 200:
            link = _extract_link(r2)
            if link:
                return link

        ctype = r2.headers.get("rblx-challenge-type", "")
        cid = r2.headers.get("rblx-challenge-id", "")
        cmeta = r2.headers.get("rblx-challenge-metadata", "")

        if r2.status_code == 403 and ctype == "twostepverification" and cid:
            meta_json = {}
            if cmeta:
                try:
                    padded = cmeta + "=" * (-len(cmeta) % 4)
                    meta_json = _json.loads(_b64.b64decode(padded).decode("utf-8", errors="ignore"))
                except Exception:
                    pass

            inner_cid = (
                meta_json.get("challengeId")
                or meta_json.get("twostepVerificationChallengeId")
                or meta_json.get("challenge_id")
                or cid
            )
            action_type = meta_json.get("actionType") or "Generic"
            uid = meta_json.get("userId") or meta_json.get("user_id")

            if not uid:
                r_me = s.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
                if r_me.status_code == 200:
                    uid = r_me.json().get("id")

            if uid and inner_cid:
                send_url = f"https://twostepverification.roblox.com/v1/users/{uid}/challenges/email/send-code"
                payload = {"actionType": action_type, "challengeId": inner_cid}
                r_send = s.post(send_url, json=payload, timeout=12)
                if r_send.status_code == 403 and r_send.headers.get("x-csrf-token"):
                    s.headers["x-csrf-token"] = r_send.headers["x-csrf-token"]
                    r_send = s.post(send_url, json=payload, timeout=12)

                if r_send.status_code in (200, 204):
                    logging.info("get_url_via_api_2fa: email успешно отправлен")
                    return {
                        "challengeId": cid,
                        "innerChallengeId": inner_cid,
                        "csrf": s.headers.get("x-csrf-token") or csrf2,
                        "emailSent": True,
                    }

        return None
    except Exception as e:
        logging.error("get_url_via_api_2fa error: %s", e)
        return None


def _is_persona_link(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return (
            p.scheme == "https"
            and (p.netloc == "withpersona.com" or p.netloc.endswith(".withpersona.com"))
            and ("inquiry-id" in p.query or "reference-id" in p.query)
        )
    except Exception:
        return False


def extract_link_from_raw(body: str) -> str | None:
    """
    Извлекает ссылку Persona из тела ответа.
    Приоритет: полный URL → inquiry-id + session-token по отдельности.
    """
    if not body:
        return None
    # Полный URL — берём всё до пробела/кавычки/обратного слеша
    m = re.search(
        r'https://[^\s"\'\\]+withpersona\.com[^\s"\'\\]*inquiry-id=[^\s"\'\\]+',
        body,
    )
    if m:
        link = m.group(0).rstrip("\\),;")
        # Убедимся что session-token не был обрезан — если есть отдельно, дописываем
        if "session-token" not in link:
            tok_m = re.search(r'session-token=([A-Za-z0-9_\-\.]+)', body)
            if tok_m:
                link += f"&session-token={tok_m.group(1)}"
        return link
    # Только inquiry-id — собираем ссылку вручную
    m = re.search(r'inquiry-id=(inq_[A-Za-z0-9]+)', body)
    if m:
        inq_id = m.group(1)
        tok_m  = re.search(r'session-token=([A-Za-z0-9_\-\.]+)', body)
        link   = f"https://inquiry.withpersona.com/verify?inquiry-id={inq_id}"
        if tok_m:
            link += f"&session-token={tok_m.group(1)}"
        return link
    # reference-id тоже бывает у Persona
    m = re.search(r'reference-id=([A-Za-z0-9_\-]+)', body)
    if m:
        return f"https://inquiry.withpersona.com/verify?reference-id={m.group(1)}"
    return None


def _extract_link(r) -> str | None:
    link = extract_link_from_raw(r.text)
    if not link and _is_persona_link(r.url):
        link = r.url
    return link


# Pending 2FA сессии: user_id → {cookie, csrf, challenge_id, ...}
pending_2fa: dict[int, dict] = {}
PENDING_2FA_TTL   = int(os.environ.get("PENDING_2FA_TTL", 600))  # 10 минут
# Защита от race condition: один пользователь — одна запись за раз
_pending_2fa_lock = __import__("asyncio").Lock()


def _cleanup_pending_2fa() -> int:
    """Удаляет устаревшие pending_2fa записи. Попытки НЕ возвращаются при TTL-очистке."""
    now     = _time.time()
    expired = [
        uid for uid, ctx in pending_2fa.items()
        if (now - ctx.get("created_at", now)) > PENDING_2FA_TTL
    ]
    for uid in expired:
        ctx = pending_2fa.pop(uid, None)
        if ctx:
            logging.info(
                "pending_2fa TTL expired: user_id=%s (attempt_deducted=%s)",
                uid, ctx.get("attempt_was_deducted"),
            )
    return len(expired)


def verify_2fa_code(
    cookie: str,
    user_roblox_id: int,
    csrf: str,
    challenge_id: str,
    code: str,
    method: str = "camera",
    inner_challenge_id: str | None = None,
) -> tuple[bool, str | None]:
    """
    Верифицирует 6-значный код 2FA через Roblox API.
    Возвращает (True, link) при успехе или (False, error_code) при ошибке.

    inner_challenge_id — ID для twostepverification/email/verify (из challenge metadata).
    challenge_id       — outer ID для challenge/v1/continue.

    Flow:
      1. POST twostepverification/.../email/verify  → verificationToken
      2. POST challenge/v1/continue                  → подтверждает решение challenge
      3. POST age-verification/start-verification    → ссылка Persona (с заголовками решённого challenge)
    """
    import json as _json
    import base64 as _b64

    inner_challenge_id = inner_challenge_id or challenge_id
    try:
        s = _make_roblox_session(cookie)
        if csrf:
            s.headers["x-csrf-token"] = csrf

        def _update_csrf(resp) -> None:
            """Всегда обновляем CSRF из ответа, если он есть."""
            new_c = resp.headers.get("x-csrf-token")
            if new_c:
                s.headers["x-csrf-token"] = new_c

        # ── Шаг 1: верифицируем email-код ──
        verify_url = (
            f"https://twostepverification.roblox.com/v1/users/{user_roblox_id}"
            f"/challenges/email/verify"
        )
        payload_verify = {
            "challengeId": inner_challenge_id,
            "actionType":  "Generic",
            "code":        code.strip(),
        }
        r_verify = s.post(verify_url, json=payload_verify, timeout=12)
        _update_csrf(r_verify)

        # Если 403 из-за CSRF — обновляем и повторяем один раз
        if r_verify.status_code == 403:
            r_verify = s.post(verify_url, json=payload_verify, timeout=12)
            _update_csrf(r_verify)

        if r_verify.status_code == 429:
            return False, "rate_limited"

        if r_verify.status_code not in (200, 204):
            body_text = r_verify.text[:300]
            logging.warning("verify_2fa step1 failed: %s %s", r_verify.status_code, body_text)
            if r_verify.status_code == 400:
                err_code = ""
                try:
                    err = r_verify.json()
                    err_code = (err.get("code") or err.get("error") or "").upper()
                except Exception:
                    pass
                if not err_code:
                    err_code = body_text.upper()
                if (
                    "EXPIRED" in err_code
                    or "INVALID_CHALLENGE" in err_code
                    or "CHALLENGE_EXPIRED" in err_code
                    or "SESSION" in err_code
                ):
                    return False, "session_expired"
                return False, "invalid_code"
            elif r_verify.status_code == 403:
                return False, "session_expired"
            else:
                return False, "exception"

        # ── Шаг 2: извлекаем verificationToken ──
        try:
            verification_token = r_verify.json().get("verificationToken", "")
        except Exception:
            verification_token = ""

        if not verification_token:
            logging.warning("verify_2fa step2: нет verificationToken: %s", r_verify.text[:100])
            return False, "invalid_code"

        logging.info("verify_2fa step2: got verificationToken")

        # ── Шаг 3: подтверждаем challenge ──
        def _build_metadata(inner_cid: str) -> tuple[str, str]:
            meta_obj = {
                "rememberDevice": False,
                "verificationToken": verification_token,
                "challengeId": inner_cid,
                "actionType": "Generic",
            }
            meta_json = _json.dumps(meta_obj, separators=(",", ":"))
            meta_b64 = _b64.b64encode(meta_json.encode("utf-8")).decode("utf-8")
            return meta_json, meta_b64

        def _do_continue(outer_cid: str = challenge_id, inner_cid: str = inner_challenge_id) -> requests.Response:
            meta_json, _ = _build_metadata(inner_cid)
            payload = {
                "challengeId":       outer_cid,
                "challengeType":     "twostepverification",
                "challengeMetadata": meta_json,
            }
            logging.info("challenge/v1/continue: outer=%s, inner=%s", outer_cid, inner_cid)
            r = s.post("https://apis.roblox.com/challenge/v1/continue", json=payload, timeout=12)
            _update_csrf(r)
            if r.status_code == 403 and not r.headers.get("rblx-challenge-type"):
                r = s.post("https://apis.roblox.com/challenge/v1/continue", json=payload, timeout=12)
                _update_csrf(r)
            return r

        r_cont = _do_continue(challenge_id, inner_challenge_id)
        logging.info("verify_2fa step3 (continue): status=%s body=%s", r_cont.status_code, r_cont.text[:150])

        # ── Шаг 4: запрашиваем ссылку ──
        _URL_V  = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
        _BODY_V = (
            {"generateLink": True, "ageEstimation": False, "parentVerification": False}
            if method == "id"
            else {"generateLink": True, "ageEstimation": True, "parentVerification": False}
        )

        curr_outer_cid = challenge_id
        curr_inner_cid = inner_challenge_id

        for attempt in range(4):
            # ВАЖНО: передаем заголовки решённого challenge, чтобы целевой сервис Roblox принял авторизацию!
            _, curr_meta_b64 = _build_metadata(curr_inner_cid)
            headers_with_challenge = {
                "rblx-challenge-id":       curr_outer_cid,
                "rblx-challenge-type":     "twostepverification",
                "rblx-challenge-metadata": curr_meta_b64,
            }
            r_final = s.post(_URL_V, json=_BODY_V, headers=headers_with_challenge, timeout=12)
            _update_csrf(r_final)
            logging.info(
                "verify_2fa step4 attempt=%d: status=%s url=%s body=%s",
                attempt, r_final.status_code, r_final.url, r_final.text[:200],
            )

            if r_final.status_code == 200:
                link = _extract_link(r_final)
                if link:
                    logging.info("verify_2fa SUCCESS: link=%s", link[:80])
                    return True, link
                logging.warning("verify_2fa step4: 200 но ссылки нет. url=%s body=%s",
                                r_final.url, r_final.text[:300])
                _time.sleep(0.5)
                continue

            if r_final.status_code == 403:
                ctype = r_final.headers.get("rblx-challenge-type", "")
                cid   = r_final.headers.get("rblx-challenge-id", "")
                logging.info(
                    "verify_2fa step4 403: challengeType=%s challengeId=%s",
                    ctype, cid,
                )
                if ctype == "twostepverification" and cid:
                    new_cmeta = r_final.headers.get("rblx-challenge-metadata", "")
                    new_inner_cid = curr_inner_cid
                    if new_cmeta:
                        try:
                            padded = new_cmeta + "=" * (-len(new_cmeta) % 4)
                            decoded_meta = _json.loads(_b64.b64decode(padded).decode("utf-8", errors="ignore"))
                            new_inner_cid = (
                                decoded_meta.get("challengeId")
                                or decoded_meta.get("twostepVerificationChallengeId")
                                or decoded_meta.get("challenge_id")
                                or curr_inner_cid
                            )
                        except Exception as e:
                            logging.warning("step4 metadata decode failed: %s", e)
                    curr_outer_cid = cid
                    curr_inner_cid = new_inner_cid
                    r_retry = _do_continue(outer_cid=cid, inner_cid=new_inner_cid)
                    logging.info("verify_2fa step4 re-continue: status=%s", r_retry.status_code)
                _time.sleep(0.3)
                continue

            logging.warning("verify_2fa step4 unexpected: status=%s body=%s",
                            r_final.status_code, r_final.text[:200])
            break

        logging.error("verify_2fa: не удалось получить ссылку после всех попыток")
        return False, "link_failed"

    except requests.exceptions.Timeout:
        logging.error("verify_2fa_code: timeout")
        return False, "exception"
    except Exception as e:
        logging.error("verify_2fa_code exception: %s", e)
        return False, "exception"


def create_invoice(amount, attempts):
    try:
        r = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            json={
                "asset":       "USDT",
                "amount":      str(round(amount, 2)),
                "description": f"Pokupka {attempts} popytok",
                "expires_in":  300,
            },
            timeout=15,
        )
        data = r.json()
        return data["result"] if data.get("ok") else None
    except Exception:
        return None


def check_invoice(invoice_id):
    try:
        r = requests.get(
            "https://pay.crypt.bot/api/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN},
            params={"invoice_ids": invoice_id},
            timeout=15,
        )
        data = r.json()
        return data["result"]["items"][0] if data.get("ok") and data["result"]["items"] else None
    except Exception:
        return None


# ──────────────────────────── xRocket ────────────────────────────

def xrocket_create_invoice(amount: float, attempts: int):
    """
    Создаёт инвойс xRocket Pay API v2 (новый API).
    Endpoint: POST https://pay.api.xrocket.exchange/api/v1/invoices
    Auth: Authorization: Bearer TOKEN
    Response: { id, links: { web_link, ... }, status, ... }
    """
    if not XROCKET_TOKEN:
        return None
    try:
        r = requests.post(
            "https://pay.api.xrocket.exchange/api/v1/invoices",
            headers={
                "Authorization": f"Bearer {XROCKET_TOKEN}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            json={
                "amount":      str(round(amount, 2)),
                "currency":    "USDT",
                "description": f"Roblox Bot: {attempts} попыток 2FA",
                "numPayments": 1,
            },
            timeout=15,
        )
        logging.info("xRocket create invoice status=%s body=%s", r.status_code, r.text[:300])
        if r.status_code in (200, 201):
            inv = r.json()
            # Новый API возвращает объект напрямую (не обёрнут в data/success)
            # links.web_link — ссылка для оплаты
            links    = inv.get("links") or {}
            pay_url  = (links.get("web_link") or links.get("webLink") or
                        inv.get("payUrl") or inv.get("link") or inv.get("url") or "")
            inv_id   = str(inv.get("id") or inv.get("invoiceId") or "")
            if not pay_url or not inv_id:
                logging.warning("xRocket: нет pay_url или id в ответе: %s", inv)
                return None
            return {"invoice_id": inv_id, "pay_url": pay_url}
        logging.warning("xRocket create error %s: %s", r.status_code, r.text[:300])
        return None
    except Exception as e:
        logging.error("xrocket_create_invoice: %s", e)
        return None


def xrocket_check_invoice(invoice_id: str) -> str | None:
    """
    Проверяет статус инвойса xRocket Pay API v2.
    GET https://pay.api.xrocket.exchange/api/v1/invoices/{id}
    Response: { status: "active" | "paid" | "expired" | ... }
    """
    if not XROCKET_TOKEN:
        return None
    try:
        r = requests.get(
            f"https://pay.api.xrocket.exchange/api/v1/invoices/{invoice_id}",
            headers={
                "Authorization": f"Bearer {XROCKET_TOKEN}",
                "Accept":        "application/json",
            },
            timeout=15,
        )
        if r.status_code == 200:
            inv    = r.json()
            status = (inv.get("status") or "").lower()
            return "paid" if status in ("paid", "completed", "success") else status
        logging.warning("xRocket check error %s: %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logging.error("xrocket_check_invoice: %s", e)
        return None


async def send_log(context, user_id, username, method, elapsed_sec, has_2fa=False):
    if not LOG_CHANNEL_ID:
        logging.warning("send_log: LOG_CHANNEL_ID не задан")
        return
    try:
        cost    = "$1.00" if has_2fa else "БЕСПЛАТНО"
        service = "Camera" if method == "camera" else "ID"
        msg = (
            f"✅ УСПЕШНО\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 ID: {user_id} (@{escape_md(username)})\n"
            f"⚙️ Услуга: {service}\n"
            f"✅ Результат: Ссылка получена\n"
            f"⏲️ Время: {round(elapsed_sec)} сек\n"
            f"💳 Сумма: {cost}"
        )
        await context.bot.send_message(chat_id=int(LOG_CHANNEL_ID), text=msg)
    except Exception as e:
        logging.warning("send_log error: %s", e)


def main_menu_kb(user_id=None):
    lang = (db_get_lang(user_id) or "ru") if user_id else "ru"
    T    = TEXTS[lang]
    buttons = [
        [InlineKeyboardButton(T["btn_get_link"], callback_data="get_link")],
        [InlineKeyboardButton(T["btn_buy"], callback_data="buy"),
         InlineKeyboardButton(T["btn_promo"], callback_data="promo")],
        [InlineKeyboardButton(T["btn_top"], callback_data="top")],
        [InlineKeyboardButton(T["btn_help"], callback_data="help"),
         InlineKeyboardButton(T["btn_support"], url="https://t.me/dedbed12")],
    ]
    if user_id and user_id in ADMIN_IDS:
        buttons.append([InlineKeyboardButton(T["btn_admin"], callback_data="admin")])
    return InlineKeyboardMarkup(buttons)


def _sub_status_text(user_id) -> tuple[str, str]:
    import datetime as _dt
    attempts              = db_get_attempts(user_id)
    is_active, expires, is_forever = db_get_subscription(user_id)
    lang                  = db_get_lang(user_id) or "ru"

    if is_active and is_forever:
        return "∞", ("♾️ Forever" if lang == "en" else "♾️ Навсегда")
    elif is_active:
        try:
            exp     = _dt.datetime.fromisoformat(expires)
            now_utc = _dt.datetime.now(_dt.timezone.utc)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_dt.timezone.utc)
            delta     = exp - now_utc
            total_min = max(0, int(delta.total_seconds() // 60))
            h, m      = divmod(total_min, 60)
            remaining = f"{h}h {m}m" if lang == "en" else f"{h}ч {m}мин"
        except Exception:
            remaining = "?"
        return "∞", remaining
    else:
        return str(attempts), ("No sub" if lang == "en" else "Нет под.")


def _lang_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇷🇺 Русский", callback_data="set_lang_ru"),
        InlineKeyboardButton("🇬🇧 English",  callback_data="set_lang_en"),
    ]])

# ──────────────────────────── HANDLERS ────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    tg       = update.effective_user
    username = tg.username or tg.first_name or "Аноним"

    loop = asyncio.get_running_loop()
    is_new = await loop.run_in_executor(executor, db_is_new_user, user_id)

    if is_new:
        await loop.run_in_executor(executor, db_upsert_user, user_id, username)
        # Обработка реферальной ссылки: /start ref_123456789
        ref_param = (context.args[0] if context.args else "").strip()
        if ref_param.startswith("ref_"):
            try:
                referrer_id = int(ref_param[4:])
                is_new_ref = await loop.run_in_executor(executor, db_set_referral, user_id, referrer_id)
                logging.info("Новый реферал: %s пришёл от %s", user_id, referrer_id)
                if is_new_ref:
                    # Уведомляем реферера о новом реферале
                    ref_name = f"@{username}" if username != "Аноним" else f"ID {user_id}"
                    try:
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                f"👥 По твоей реферальной ссылке зарегистрировался новый пользователь!\n"
                                f"👤 {ref_name}\n\n"
                                f"Ты будешь получать *20%* от каждой его оплаченной 2FA-ссылки."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass  # Реферер мог заблокировать бота
            except (ValueError, Exception) as e:
                logging.warning("Referral parse error: %s", e)
        await update.message.reply_text(
            "🌐 Выберите язык / Choose language:",
            reply_markup=_lang_kb(),
        )
        return WAITING_LANG

    await loop.run_in_executor(executor, db_upsert_user, user_id, username)
    attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
    text = (
        f"{t(user_id, 'main_menu_title')}\n\n"
        f"{t(user_id, 'main_menu_sub')}\n\n"
        f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
        f"{t(user_id, 'main_menu_action')}"
    )
    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=main_menu_kb(user_id)
    )
    return WAITING_MENU


async def handle_set_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    # query.answer() уже вызван в handle_callback
    user_id  = update.effective_user.id
    lang     = "ru" if query.data == "set_lang_ru" else "en"
    loop     = asyncio.get_running_loop()
    await loop.run_in_executor(executor, db_set_lang, user_id, lang)

    attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
    text = (
        f"{t(user_id, 'main_menu_title')}\n\n"
        f"{t(user_id, 'main_menu_sub')}\n\n"
        f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
        f"{t(user_id, 'main_menu_action')}"
    )
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb(user_id))
    return WAITING_MENU


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 Выберите язык / Choose language:", reply_markup=_lang_kb())
    return WAITING_LANG


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loop   = asyncio.get_running_loop()
    rows   = await loop.run_in_executor(executor, db_get_all_users)
    promos = await loop.run_in_executor(executor, db_get_all_promos)
    no2fa_icon = "✅" if SERVICE_NO2FA_ENABLED else "❌"
    fa2_icon   = "✅" if SERVICE_2FA_ENABLED   else "❌"
    proxy_status = f"🌐 *{'ВКЛ — ' + str(len(_proxy_pool)) + ' шт.' if _proxy_pool else 'ВЫКЛ — прямое подключение'}*"
    text = (
        f"🛠 *Админ панель (2FA защита: ID + Пароль)*\n\n"
        f"👥 Пользователей в базе: *{len(rows)}*\n"
        f"🎫 Создано промокодов: *{len(promos)}*\n"
        f"🔐 Статус: *Авторизован*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔧 *Режимы получения ссылок:*\n"
        f"  🔓 Без 2FA: *{'ВКЛ' if SERVICE_NO2FA_ENABLED else 'ВЫКЛ'}*\n"
        f"  🔐 С 2FA: *{'ВКЛ' if SERVICE_2FA_ENABLED else 'ВЫКЛ'}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 *Прокси пул:* {proxy_status}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{no2fa_icon} Без 2FA: {'ВКЛ' if SERVICE_NO2FA_ENABLED else 'ВЫКЛ'}",
                              callback_data="admin_toggle_no2fa"),
         InlineKeyboardButton(f"{fa2_icon} С 2FA: {'ВКЛ' if SERVICE_2FA_ENABLED else 'ВЫКЛ'}",
                              callback_data="admin_toggle_2fa")],
        [InlineKeyboardButton("👥 Список пользователей",  callback_data="admin_users")],
        [InlineKeyboardButton("🎫 Список промокодов",     callback_data="admin_promo_list")],
        [InlineKeyboardButton("➕ Добавить попытки",      callback_data="admin_add_bal"),
         InlineKeyboardButton("➖ Отнять попытки",        callback_data="admin_rem_bal")],
        [InlineKeyboardButton("🎟️ Выдать подписку",      callback_data="admin_add_sub")],
        [InlineKeyboardButton("➕ Создать промокод",      callback_data="admin_create_promo")],
        [InlineKeyboardButton("📢 Рассылка",              callback_data="admin_broadcast")],
        [InlineKeyboardButton("🔑 Сменить пароль",        callback_data="admin_change_password"),
         InlineKeyboardButton("🔒 Выйти",                 callback_data="admin_logout")],
        [InlineKeyboardButton("🏠 На главную",            callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return WAITING_ADMIN


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        logging.warning("Попытка доступа к /admin неавторизованным ID: %s", user_id)
        return WAITING_MENU

    if context.user_data.get("admin_auth"):
        return await show_admin_panel(update, context)

    context.user_data["waiting"] = "admin_password"
    text = (
        "🔐 *Двухфакторная защита админ-панели (ID + Пароль)*\n\n"
        "✅ Ваш Telegram ID подтверждён в списке администраторов.\n"
        "🔑 Введите пароль администратора для входа:"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="main_menu")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    elif update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    return WAITING_ADMIN_PASSWORD

async def _show_payment_method_screen(query, user_id: int, count: int, total: float, discount: float, lang: str):
    """Показывает экран выбора метода оплаты с деталями инвойса."""
    disc_str = f"−{int(discount*100)}%" if discount else ("без скидки" if lang == "ru" else "no discount")
    if lang == "ru":
        text = (
            f"💳 *Выберите способ оплаты*\n\n"
            f"Инвойс: `0`\n"
            f"Попыток: *{count}*\n"
            f"Сумма: *${total:.2f}*\n"
            f"Скидка: {disc_str}\n\n"
            f"Нажмите кнопку ниже для оплаты в USD.\n"
            f"После оплаты нажмите «Я оплатил», попытки зачислятся сразу."
        )
    else:
        text = (
            f"💳 *Choose payment method*\n\n"
            f"Invoice: `0`\n"
            f"Attempts: *{count}*\n"
            f"Amount: *${total:.2f}*\n"
            f"Discount: {disc_str}\n\n"
            f"Tap a button below to pay in USD.\n"
            f"After payment tap «I paid», attempts will be credited immediately."
        )
    method_buttons = []
    if CRYPTO_BOT_TOKEN:
        method_buttons.append(InlineKeyboardButton("🤖 CryptoBot", callback_data=f"paymethod_cb_{count}"))
    if XROCKET_TOKEN:
        method_buttons.append(InlineKeyboardButton("🚀 xRocket",   callback_data=f"paymethod_xr_{count}"))
    if not method_buttons:
        await query.answer(
            "Способы оплаты временно недоступны." if lang == "ru" else "Payment methods unavailable.",
            show_alert=True,
        )
        return
    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            method_buttons,
            [InlineKeyboardButton("◀️ Назад" if lang == "ru" else "◀️ Back", callback_data="buy_attempts")],
        ]),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global SERVICE_NO2FA_ENABLED, SERVICE_2FA_ENABLED
    query    = update.callback_query
    user_id  = update.effective_user.id
    tg       = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    data     = query.data
    loop     = asyncio.get_running_loop()

    # Всегда отвечаем на callback сразу — иначе Telegram покажет бесконечную загрузку
    try:
        await query.answer()
    except Exception:
        pass  # Уже был отвечен или истёк таймаут — продолжаем

    if data == "cancel_2fa":
        return await receive_2fa_code(update, context)

    if data in ("set_lang_ru", "set_lang_en"):
        return await handle_set_lang(update, context)

    if data == "main_menu":
        attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
        text = (
            f"{t(user_id, 'main_menu_title')}\n\n"
            f"{t(user_id, 'main_menu_sub')}\n\n"
            f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
            f"{t(user_id, 'main_menu_action')}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb(user_id))
        return WAITING_MENU

    if data == "help":
        await query.edit_message_text(
            t(user_id, "help_text"),
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_MENU

    if data == "top":
        rows   = await loop.run_in_executor(executor, db_get_top, 10)
        medals = ["🥇", "🥈", "🥉"]
        lines  = [t(user_id, "top_title")]
        if not rows:
            lines.append(t(user_id, "top_empty"))
        else:
            for i, (uid, uname, spent) in enumerate(rows):
                medal = medals[i] if i < 3 else f"{i + 1}."
                lines.append(f"{medal} {uname or '---'} — ${round(spent, 2)}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_MENU

    if data == "promo":
        context.user_data["waiting"] = "promo"
        await query.edit_message_text(
            f"{t(user_id, 'promo_title')}\n\n{t(user_id, 'promo_enter')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_PROMO

    if data == "buy":
        await query.edit_message_text(
            f"{t(user_id, 'buy_title')}\n\n{t(user_id, 'buy_price')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_buy_attempts"), callback_data="buy_attempts")],
                [InlineKeyboardButton(t(user_id, "btn_buy_sub"),      callback_data="buy_sub")],
                [InlineKeyboardButton(t(user_id, "btn_back"),         callback_data="main_menu")],
            ]),
        )
        return WAITING_BUY

    if data == "buy_attempts":
        lang = db_get_lang(user_id) or "ru"
        if lang == "ru":
            text = (
                "🛒 *Покупка попыток*\n\n"
                f"Цена за 1 попытку: *${PRICE_2FA:.0f}*\n\n"
                "Скидки на пакеты:\n"
                "• 5 попыток: *10%*\n"
                "• 10 попыток: *15%*\n"
                "• 25 попыток: *25%*\n\n"
                "_Скидка действует только при выборе кнопки.\n"
                "При своём количестве скидки нет._\n\n"
                "Выберите количество или введите число:"
            )
        else:
            text = (
                "🛒 *Buy Attempts*\n\n"
                f"Price per attempt: *${PRICE_2FA:.0f}*\n\n"
                "Package discounts:\n"
                "• 5 attempts: *10%*\n"
                "• 10 attempts: *15%*\n"
                "• 25 attempts: *25%*\n\n"
                "_Discount applies only to package buttons.\n"
                "Custom amount has no discount._\n\n"
                "Choose a package or type a number:"
            )
        context.user_data["waiting"] = "custom_buy_count"
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_buy_5"),  callback_data="buy_5")],
                [InlineKeyboardButton(t(user_id, "btn_buy_10"), callback_data="buy_10")],
                [InlineKeyboardButton(t(user_id, "btn_buy_25"), callback_data="buy_25")],
                [InlineKeyboardButton(
                    "✏️ Своё количество" if lang == "ru" else "✏️ Custom amount",
                    callback_data="buy_custom"
                )],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy")],
            ]),
        )
        return WAITING_BUY

    if data == "buy_sub":
        lang = db_get_lang(user_id) or "ru"
        await query.edit_message_text(
            "♾️ Выберите период подписки:" if lang == "ru" else "♾️ Choose subscription period:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_sub_1d"),  callback_data="sub_1")],
                [InlineKeyboardButton(t(user_id, "btn_sub_3d"),  callback_data="sub_3")],
                [InlineKeyboardButton(t(user_id, "btn_sub_7d"),  callback_data="sub_7")],
                [InlineKeyboardButton(t(user_id, "btn_sub_30d"), callback_data="sub_30")],
                [InlineKeyboardButton(t(user_id, "btn_sub_inf"), callback_data="sub_0")],
                [InlineKeyboardButton(t(user_id, "btn_back"),    callback_data="buy")],
            ]),
        )
        return WAITING_BUY

    if data.startswith("sub_") and data[4:].isdigit():
        days    = int(data[4:])
        price   = SUBSCRIPTIONS.get(days)
        labels  = {1: t(user_id, "btn_sub_1d"), 3: t(user_id, "btn_sub_3d"),
                   7: t(user_id, "btn_sub_7d"), 30: t(user_id, "btn_sub_30d"),
                   0: t(user_id, "btn_sub_inf")}
        desc    = labels.get(days, f"{days} дней")
        # Показываем выбор метода оплаты (как для попыток)
        method_buttons = []
        if CRYPTO_BOT_TOKEN:
            method_buttons.append(InlineKeyboardButton(t(user_id, "btn_pay_crypto"),  callback_data=f"subpay_cb_{days}"))
        if XROCKET_TOKEN:
            method_buttons.append(InlineKeyboardButton(t(user_id, "btn_pay_xrocket"), callback_data=f"subpay_xr_{days}"))
        if not method_buttons:
            await query.answer(t(user_id, "pay_method_unavail"), show_alert=True)
            return WAITING_BUY
        await query.edit_message_text(
            t(user_id, "choose_payment", amount=f"{price:.2f}"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                method_buttons,
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_sub")],
            ]),
        )
        return WAITING_PAYMENT

    # ── Выбор метода оплаты (подписка) ──
    if data.startswith("subpay_"):
        parts = data.split("_")  # subpay_cb_7 or subpay_xr_7
        method = parts[1]  # cb or xr
        try:
            days = int(parts[2])
        except (ValueError, IndexError):
            return WAITING_MENU
        price  = SUBSCRIPTIONS.get(days)
        labels = {1: t(user_id, "btn_sub_1d"), 3: t(user_id, "btn_sub_3d"),
                  7: t(user_id, "btn_sub_7d"), 30: t(user_id, "btn_sub_30d"),
                  0: t(user_id, "btn_sub_inf")}
        desc = labels.get(days, f"{days} дней")
        if method == "cb":
            try:
                raw = await loop.run_in_executor(executor, create_invoice, price, 1)
            except Exception as e:
                logging.error("CryptoPay sub run_in_executor error: %s", e)
                raw = None
            if not raw:
                await query.edit_message_text(
                    t(user_id, "pay_error"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_sub")
                    ]]),
                )
                return WAITING_BUY
            inv_key = f"sub_{user_id}_{raw['invoice_id']}"
            pay_url = raw["pay_url"]
        else:
            try:
                raw = await loop.run_in_executor(executor, xrocket_create_invoice, price, 1)
            except Exception as e:
                logging.error("xRocket sub run_in_executor error: %s", e)
                raw = None
            if not raw:
                await query.edit_message_text(
                    t(user_id, "pay_error"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_sub")
                    ]]),
                )
                return WAITING_BUY
            inv_key = f"xr_sub_{user_id}_{raw['invoice_id']}"
            pay_url = raw["pay_url"]
        pending_payments[inv_key] = {
            "type": "sub", "user_id": user_id,
            "days": days, "price": price, "desc": desc,
        }
        await query.edit_message_text(
            f"Счёт на подписку: {desc}\nСумма: ${price}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_pay"),       url=pay_url)],
                [InlineKeyboardButton(t(user_id, "btn_check_pay"), callback_data=f"check_{inv_key}")],
                [InlineKeyboardButton(t(user_id, "btn_back"),      callback_data="buy_sub")],
            ]),
        )
        return WAITING_PAYMENT

    if data == "buy_custom":
        lang = db_get_lang(user_id) or "ru"
        context.user_data["waiting"] = "custom_buy_count"
        prompt = (
            "✏️ Введите количество попыток числом (например: 3):"
            if lang == "ru" else
            "✏️ Enter the number of attempts (e.g. 3):"
        )
        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_attempts")
            ]]),
        )
        return WAITING_BUY

    if data.startswith("buy_") and data not in ("buy_attempts", "buy_sub", "buy_custom"):
        try:
            count = int(data.split("_")[1])
        except (ValueError, IndexError):
            return WAITING_MENU
        discount = DISCOUNTS_2FA.get(count, 0)
        total    = round(count * PRICE_2FA * (1 - discount), 2)
        lang     = db_get_lang(user_id) or "ru"
        await _show_payment_method_screen(query, user_id, count, total, discount, lang)
        return WAITING_PAYMENT

    # ── Создание инвойса (попытки) ──
    if data.startswith("paymethod_"):
        parts = data.split("_")  # paymethod_cb_5 or paymethod_xr_5
        pay_method = parts[1]  # cb or xr
        try:
            count = int(parts[2])
        except (ValueError, IndexError):
            return WAITING_MENU
        discount = DISCOUNTS_2FA.get(count, 0)
        total    = round(count * PRICE_2FA * (1 - discount), 2)
        lang     = db_get_lang(user_id) or "ru"
        disc_str = f"−{int(discount*100)}%" if discount else ("без скидки" if lang == "ru" else "no discount")

        if pay_method == "cb":
            try:
                raw = await loop.run_in_executor(executor, create_invoice, total, count)
            except Exception as e:
                logging.error("CryptoPay run_in_executor error: %s", e)
                raw = None
            if not raw:
                await query.edit_message_text(
                    t(user_id, "pay_error"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_attempts")
                    ]]),
                )
                return WAITING_BUY
            inv_key    = f"cb_{raw['invoice_id']}"
            pay_url    = raw["pay_url"]
            inv_number = raw["invoice_id"]
            method_label = "CryptoBot"
        else:
            try:
                raw = await loop.run_in_executor(executor, xrocket_create_invoice, total, count)
            except Exception as e:
                logging.error("xRocket run_in_executor error: %s", e)
                raw = None
            if not raw:
                await query.edit_message_text(
                    t(user_id, "pay_error"),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_attempts")
                    ]]),
                )
                return WAITING_BUY
            inv_key    = f"xr_{raw['invoice_id']}"
            pay_url    = raw["pay_url"]
            inv_number = raw["invoice_id"]
            method_label = "xRocket"

        pending_payments[inv_key] = {"user_id": user_id, "attempts": count, "total": total}

        if lang == "ru":
            text = (
                f"💳 *Оплата через {method_label}*\n\n"
                f"Инвойс: `{inv_number}`\n"
                f"Попыток: *{count}*\n"
                f"Сумма: *${total:.2f}*\n"
                f"Скидка: {disc_str}\n\n"
                f"Нажмите кнопку ниже для оплаты в USD.\n"
                f"После оплаты нажмите «Я оплатил», попытки зачислятся сразу."
            )
            btn_paid = "✅ Я оплатил"
        else:
            text = (
                f"💳 *Payment via {method_label}*\n\n"
                f"Invoice: `{inv_number}`\n"
                f"Attempts: *{count}*\n"
                f"Amount: *${total:.2f}*\n"
                f"Discount: {disc_str}\n\n"
                f"Tap the button below to pay in USD.\n"
                f"After payment tap «I paid», attempts will be credited immediately."
            )
            btn_paid = "✅ I paid"

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Оплатить" if lang == "ru" else "💳 Pay", url=pay_url)],
                [InlineKeyboardButton(btn_paid, callback_data=f"check_{inv_key}")],
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="buy_attempts")],
            ]),
        )
        return WAITING_PAYMENT

    if data.startswith("check_"):
        inv_key = data[6:]
        raw_id  = inv_key.split("_")[-1]

        payment_preview = pending_payments.get(inv_key)
        if payment_preview is not None and payment_preview.get("user_id") != user_id:
            await query.answer("Это не ваш платёж.", show_alert=True)
            return WAITING_MENU

        # Определяем какой шлюз проверять (xr_ и xr_sub_ — оба xRocket)
        if inv_key.startswith("xr_"):
            xr_status = await loop.run_in_executor(executor, xrocket_check_invoice, raw_id)
            is_paid = (xr_status == "paid")
            inv = {"status": xr_status} if xr_status else None
        else:
            inv = await loop.run_in_executor(executor, check_invoice, raw_id)
            is_paid = bool(inv and inv.get("status") == "paid")

        if is_paid:
            payment = pending_payments.pop(inv_key, None)
            if payment is None:
                await query.answer(
                    "Платёж уже обработан или сессия устарела. Обратитесь в поддержку.",
                    show_alert=True,
                )
            elif payment.get("type") == "sub":
                await loop.run_in_executor(executor, db_set_subscription, user_id, payment["days"])
                await loop.run_in_executor(
                    executor, db_upsert_user, user_id, username,
                    0, payment["price"],
                )
                await query.edit_message_text(
                    t(user_id, "sub_bought", desc=payment["desc"]),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                    ]]),
                )
            else:
                cnt = payment["attempts"]
                await loop.run_in_executor(
                    executor, db_upsert_user, user_id, username, cnt, payment["total"]
                )
                total_attempts = await loop.run_in_executor(executor, db_get_attempts, user_id)
                await query.edit_message_text(
                    t(user_id, "pay_ok", attempts=cnt, total=total_attempts),
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                    ]]),
                )
        else:
            await query.answer(t(user_id, "pay_pending"), show_alert=True)
        return WAITING_MENU

    if data == "get_link":
        # Build mode buttons based on what admin has enabled
        mode_buttons = []
        if SERVICE_NO2FA_ENABLED:
            mode_buttons.append(InlineKeyboardButton(t(user_id, "btn_no_2fa"), callback_data="2fa_no"))
        if SERVICE_2FA_ENABLED:
            mode_buttons.append(InlineKeyboardButton(t(user_id, "btn_with_2fa"), callback_data="2fa_yes"))
        if not mode_buttons:
            await query.edit_message_text(
                "⛔ Сервис временно недоступен.\nОбратитесь к администратору.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                ]]),
            )
            return WAITING_MENU
        await query.edit_message_text(
            t(user_id, "ask_2fa"),
            reply_markup=InlineKeyboardMarkup([
                mode_buttons,
                [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
            ]),
        )
        return WAITING_MENU

    if data in ("2fa_yes", "2fa_no"):
        # Double-check mode is still enabled when user picks
        if data == "2fa_no" and not SERVICE_NO2FA_ENABLED:
            await query.answer("⛔ Режим Без 2FA сейчас отключён администратором.", show_alert=True)
            return WAITING_MENU
        if data == "2fa_yes" and not SERVICE_2FA_ENABLED:
            await query.answer("⛔ Режим С 2FA сейчас отключён администратором.", show_alert=True)
            return WAITING_MENU
        context.user_data["has_2fa"] = (data == "2fa_yes")
        await query.edit_message_text(
            t(user_id, "choose_method"),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(t(user_id, "btn_camera"), callback_data="choose_camera")],
                [InlineKeyboardButton(t(user_id, "btn_id"),     callback_data="choose_id")],
                [InlineKeyboardButton(t(user_id, "btn_back"),   callback_data="get_link")],
            ]),
        )
        return WAITING_MENU

    if data in ("choose_camera", "choose_id"):
        context.user_data["method"]  = "camera" if data == "choose_camera" else "id"
        has_2fa                      = context.user_data.get("has_2fa", False)
        cookie_prompt_key            = "2fa_ask_cookie" if has_2fa else "no2fa_ask_cookie"
        await query.edit_message_text(t(user_id, cookie_prompt_key), parse_mode="Markdown")
        return WAITING_COOKIE

    # ── REFERRAL MENU ──
    if data == "referral_menu":
        bot_me = context.bot.username
        if not bot_me:
            try:
                me = await context.bot.get_me()
                bot_me = me.username
            except Exception:
                bot_me = None
        if not bot_me:
            await query.answer(t(user_id, "ref_no_bot"), show_alert=True)
            return WAITING_MENU
        ref_link = f"https://t.me/{bot_me}?start=ref_{user_id}"
        count, earned = await loop.run_in_executor(executor, db_get_referral_stats, user_id)
        await query.edit_message_text(
            t(user_id, "ref_menu_title") + "\n\n" +
            t(user_id, "ref_menu_body", count=count, earned=earned, ref_link=ref_link),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_MENU

    # ── ADMIN TOGGLE SERVICE MODES ──
    if data in ("admin_toggle_no2fa", "admin_toggle_2fa"):
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        no2fa_cur, fa2_cur = await loop.run_in_executor(executor, db_get_service_flags)
        SERVICE_NO2FA_ENABLED, SERVICE_2FA_ENABLED = no2fa_cur, fa2_cur
        if data == "admin_toggle_no2fa":
            SERVICE_NO2FA_ENABLED = not SERVICE_NO2FA_ENABLED
            new_val = "true" if SERVICE_NO2FA_ENABLED else "false"
            await loop.run_in_executor(executor, db_set_setting, "service_no2fa", new_val)
        else:
            SERVICE_2FA_ENABLED = not SERVICE_2FA_ENABLED
            new_val = "true" if SERVICE_2FA_ENABLED else "false"
            await loop.run_in_executor(executor, db_set_setting, "service_2fa", new_val)
        return await show_admin_panel(update, context)

    # ── ADMIN ──
    if data == "admin":
        if user_id not in ADMIN_IDS:
            return WAITING_MENU
        if context.user_data.get("admin_auth"):
            return await show_admin_panel(update, context)
        context.user_data["waiting"] = "admin_password"
        await query.edit_message_text(
            "🔐 *Двухфакторная защита админ-панели (ID + Пароль)*\n\n"
            "✅ Ваш Telegram ID подтверждён в списке администраторов.\n"
            "🔑 Введите пароль администратора для входа:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
            ]]),
        )
        return WAITING_ADMIN_PASSWORD

    if data == "admin_logout":
        context.user_data["admin_auth"] = False
        context.user_data["waiting"] = None
        await query.answer("Вы вышли из админ-панели", show_alert=False)
        attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
        text = (
            f"{t(user_id, 'main_menu_title')}\n\n"
            f"{t(user_id, 'main_menu_sub')}\n\n"
            f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
            f"{t(user_id, 'main_menu_action')}"
        )
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=main_menu_kb(user_id))
        return WAITING_MENU

    if data == "admin_change_password":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_change_password"
        await query.edit_message_text(
            "🔑 *Смена пароля администратора*\n\n"
            "Введите новый надёжный пароль (минимум 4 символа):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ В админку", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_CHANGE_PASSWORD

    if data == "admin_users":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        rows  = await loop.run_in_executor(executor, db_get_all_users)
        lines = ["*👥 Список пользователей:*\n"]
        for uid, uname, atts, spent in rows[:30]:
            lines.append(f"ID: `{uid}` | @{escape_md(uname or '---')} | Поп: {atts} | ${round(spent, 2)}")
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ В админку", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN

    if data == "admin_promo_list":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        rows  = await loop.run_in_executor(executor, db_get_all_promos)
        lines = ["*🎫 Список промокодов:*\n"]
        for code, atts, uses, max_uses in rows:
            lines.append(f"`{code}` — {atts} поп. | {uses}/{max_uses}")
        await query.edit_message_text(
            "\n".join(lines) if rows else "Промокодов нет.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ В админку", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN

    if data == "admin_add_bal":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_add_bal_id"
        await query.edit_message_text(
            "➕ *Добавить попытки*\n\nВведите Telegram ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_ADD_BALANCE

    if data == "admin_rem_bal":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_rem_bal_id"
        await query.edit_message_text(
            "➖ *Отнять попытки*\n\nВведите Telegram ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_REMOVE_BALANCE

    if data == "admin_add_sub":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_add_sub_id"
        await query.edit_message_text(
            "🎟️ *Выдать подписку*\n\nВведите Telegram ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_ADD_SUB

    if data == "admin_create_promo":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_create_promo_text"
        await query.edit_message_text(
            "🎟️ *Создание промокода*\n\nФормат: `КОД ПОПЫТКИ ЛИМИТ`\nПример: `VIP 10 5`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_CREATE_PROMO

    if data == "admin_broadcast":
        if user_id not in ADMIN_IDS or not context.user_data.get("admin_auth"):
            return WAITING_MENU
        context.user_data["waiting"] = "admin_broadcast_text"
        await query.edit_message_text(
            "📢 *Рассылка*\n\nОтправьте сообщение — оно будет разослано всем пользователям бота.\n\nПоддерживается текст, фото, видео.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Отмена", callback_data="admin")
            ]]),
        )
        return WAITING_ADMIN_BROADCAST

    return WAITING_MENU


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_obj  = update.message
    text     = (msg_obj.text or msg_obj.caption or "").strip()
    user_id  = update.effective_user.id
    tg       = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    waiting  = context.user_data.get("waiting")
    loop     = asyncio.get_running_loop()

    if waiting == "custom_buy_count":
        context.user_data["waiting"] = None
        lang = db_get_lang(user_id) or "ru"
        try:
            count = int(text.strip())
            if count < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Введите целое число больше 0." if lang == "ru" else "❌ Enter a whole number greater than 0.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Назад" if lang == "ru" else "◀️ Back", callback_data="buy_attempts")
                ]]),
            )
            return WAITING_BUY
        # Кастомное количество — без скидки
        total = round(count * PRICE_2FA, 2)
        disc_str = "без скидки" if lang == "ru" else "no discount"
        if lang == "ru":
            text_msg = (
                f"💳 *Выберите способ оплаты*\n\n"
                f"Инвойс: `0`\n"
                f"Попыток: *{count}*\n"
                f"Сумма: *${total:.2f}*\n"
                f"Скидка: {disc_str}\n\n"
                f"Нажмите кнопку ниже для оплаты в USD.\n"
                f"После оплаты нажмите «Я оплатил», попытки зачислятся сразу."
            )
        else:
            text_msg = (
                f"💳 *Choose payment method*\n\n"
                f"Invoice: `0`\n"
                f"Attempts: *{count}*\n"
                f"Amount: *${total:.2f}*\n"
                f"Discount: {disc_str}\n\n"
                f"Tap a button below to pay in USD.\n"
                f"After payment tap «I paid», attempts will be credited immediately."
            )
        method_buttons = []
        if CRYPTO_BOT_TOKEN:
            method_buttons.append(InlineKeyboardButton("🤖 CryptoBot", callback_data=f"paymethod_cb_{count}"))
        if XROCKET_TOKEN:
            method_buttons.append(InlineKeyboardButton("🚀 xRocket",   callback_data=f"paymethod_xr_{count}"))
        if not method_buttons:
            await update.message.reply_text(
                "❌ Способы оплаты недоступны." if lang == "ru" else "❌ Payment methods unavailable."
            )
            return WAITING_MENU
        await update.message.reply_text(
            text_msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                method_buttons,
                [InlineKeyboardButton("◀️ Назад" if lang == "ru" else "◀️ Back", callback_data="buy_attempts")],
            ]),
        )
        return WAITING_PAYMENT

    if waiting == "promo":
        context.user_data["waiting"] = None
        ok, msg_key, atts = await loop.run_in_executor(
            executor, db_use_promo, user_id, text.upper(), username
        )
        if ok:
            total = await loop.run_in_executor(executor, db_get_attempts, user_id)
            await update.message.reply_text(
                t(user_id, "promo_ok", attempts=atts, total=total),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                ]]),
            )
        else:
            key_map = {"not_found": "promo_not_found", "used": "promo_used", "used_by_you": "promo_used_by_you"}
            await update.message.reply_text(
                t(user_id, key_map.get(msg_key, "promo_not_found")),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                ]]),
            )
        return WAITING_MENU

    if user_id in ADMIN_IDS:
        if waiting == "admin_password":
            expected = get_admin_password()
            if text == expected:
                context.user_data["waiting"] = None
                context.user_data["admin_auth"] = True
                await update.message.reply_text("✅ Пароль верный! Добро пожаловать в админ-панель.")
                return await show_admin_panel(update, context)
            else:
                await update.message.reply_text(
                    "❌ *Неверный пароль!*\n\nПопробуйте ввести пароль снова или нажмите «Отмена»:",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
                    ]])
                )
                return WAITING_ADMIN_PASSWORD

        if waiting == "admin_change_password":
            if not context.user_data.get("admin_auth"):
                return WAITING_MENU
            if len(text) < 4:
                await update.message.reply_text("❌ Пароль слишком короткий (минимум 4 символа). Введите снова:")
                return WAITING_ADMIN_CHANGE_PASSWORD
            if save_admin_password(text):
                context.user_data["waiting"] = None
                await update.message.reply_text(
                    f"✅ *Пароль успешно изменён!*\n\nНовый пароль сохранён в `admin_password.txt`:\n`{escape_md(text)}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("◀️ В админку", callback_data="admin")
                    ]])
                )
                return WAITING_ADMIN
            else:
                await update.message.reply_text("❌ Не удалось сохранить пароль в файл.")
                return WAITING_ADMIN

        if waiting == "admin_add_bal_id":
            try:
                context.user_data["target_uid"] = int(text)
                context.user_data["waiting"]    = "admin_add_bal_amt"
                await update.message.reply_text("Введите количество попыток:")
                return WAITING_ADMIN_ADD_BALANCE_AMOUNT
            except ValueError:
                await update.message.reply_text("Ошибка! Числовой ID.")
                return WAITING_ADMIN_ADD_BALANCE

        elif waiting == "admin_add_bal_amt":
            context.user_data["waiting"] = None
            target_uid = context.user_data.get("target_uid")
            try:
                amt = int(text)
                await loop.run_in_executor(
                    executor, db_upsert_user, target_uid, "AdminAdded", amt, 0.0
                )
                await update.message.reply_text(
                    f"✅ +{amt} попыток → `{escape_md(str(target_uid))}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("В админку", callback_data="admin")
                    ]]),
                )
            except ValueError:
                await update.message.reply_text("Ошибка! Введите число.")
            return WAITING_ADMIN

        elif waiting == "admin_rem_bal_id":
            try:
                context.user_data["target_uid"] = int(text)
                context.user_data["waiting"]    = "admin_rem_bal_amt"
                await update.message.reply_text("Введите количество попыток для списания:")
                return WAITING_ADMIN_REMOVE_BALANCE_AMOUNT
            except ValueError:
                await update.message.reply_text("Ошибка! Числовой ID.")
                return WAITING_ADMIN_REMOVE_BALANCE

        elif waiting == "admin_rem_bal_amt":
            context.user_data["waiting"] = None
            target_uid = context.user_data.get("target_uid")
            try:
                amt = int(text)
                cur = await loop.run_in_executor(executor, db_get_attempts, target_uid)
                to_rem = min(amt, cur)
                await loop.run_in_executor(
                    executor, db_upsert_user, target_uid, "AdminRem", -to_rem, 0.0
                )
                await update.message.reply_text(
                    f"✅ Списано {to_rem} попыток у `{escape_md(str(target_uid))}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("В админку", callback_data="admin")
                    ]]),
                )
            except ValueError:
                await update.message.reply_text("Ошибка! Введите число.")
            return WAITING_ADMIN

        elif waiting == "admin_add_sub_id":
            try:
                context.user_data["target_uid"] = int(text)
                context.user_data["waiting"]    = "admin_add_sub_days"
                await update.message.reply_text("Введите дней подписки (0 = навсегда):")
                return WAITING_ADMIN_ADD_SUB
            except ValueError:
                await update.message.reply_text("Ошибка! Числовой ID.")
                return WAITING_ADMIN_ADD_SUB

        elif waiting == "admin_add_sub_days":
            context.user_data["waiting"] = None
            target_uid = context.user_data.get("target_uid")
            try:
                days  = int(text)
                await loop.run_in_executor(executor, db_set_subscription, target_uid, days)
                label = "навсегда" if days == 0 else f"{days} дней"
                await update.message.reply_text(
                    f"✅ Подписка ({label}) → `{escape_md(str(target_uid))}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("В админку", callback_data="admin")
                    ]]),
                )
            except ValueError:
                await update.message.reply_text("Ошибка!")
            return WAITING_ADMIN

        elif waiting == "admin_create_promo_text":
            context.user_data["waiting"] = None
            parts = text.split()
            if len(parts) >= 3:
                try:
                    await loop.run_in_executor(
                        executor, db_create_promo, parts[0].upper(), int(parts[1]), int(parts[2])
                    )
                    await update.message.reply_text(
                        f"✅ Промокод `{escape_md(parts[0].upper())}` создан!",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("В админку", callback_data="admin")
                        ]]),
                    )
                except ValueError:
                    await update.message.reply_text(
                        "Ошибка! Попытки и лимит должны быть числами. Формат: КОД ПОПЫТКИ ЛИМИТ"
                    )
            else:
                await update.message.reply_text("Неверный формат! КОД ПОПЫТКИ ЛИМИТ")
            return WAITING_ADMIN

        elif waiting == "admin_broadcast_text":
            context.user_data["waiting"] = None
            if user_id not in ADMIN_IDS:
                return WAITING_MENU

            all_users = await loop.run_in_executor(executor, db_get_all_users)
            all_ids   = [row[0] for row in all_users]

            sent_ok  = 0
            sent_err = 0

            status_msg = await update.message.reply_text(
                f"📢 Начинаю рассылку для {len(all_ids)} пользователей...",
            )

            for target_id in all_ids:
                try:
                    if update.message.photo:
                        await context.bot.send_photo(
                            chat_id=target_id,
                            photo=update.message.photo[-1].file_id,
                            caption=update.message.caption or "",
                            parse_mode="Markdown",
                        )
                    elif update.message.video:
                        await context.bot.send_video(
                            chat_id=target_id,
                            video=update.message.video.file_id,
                            caption=update.message.caption or "",
                            parse_mode="Markdown",
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=target_id,
                            text=update.message.text or "",
                            parse_mode="Markdown",
                        )
                    sent_ok += 1
                except Exception:
                    sent_err += 1
                await asyncio.sleep(0.05)  # ~20 сообщений/сек — в рамках лимитов Telegram

            await status_msg.edit_text(
                f"✅ Рассылка завершена\n\n"
                f"Отправлено: *{sent_ok}*\n"
                f"Ошибок: *{sent_err}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("В админку", callback_data="admin")
                ]]),
            )
            return WAITING_ADMIN

    return await receive_cookie(update, context)


def _normalize_cookie(raw: str) -> str:
    c = raw.strip().strip('"').strip("'").strip()
    warning_match = re.search(r'(_\|WARNING[^|]*\|_)(.*)', c, re.DOTALL)
    if warning_match:
        full_token  = warning_match.group(0).strip()
        actual_part = warning_match.group(2).strip()
        if actual_part:
            c = full_token
    return c


async def receive_cookie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cookie   = _normalize_cookie(update.message.text)
    method   = context.user_data.get("method")
    has_2fa  = context.user_data.get("has_2fa", False)
    user_id  = update.effective_user.id
    tg       = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    chat_id  = update.effective_chat.id
    loop     = asyncio.get_running_loop()

    if not method:
        await update.message.reply_text("Сначала выберите метод через /start")
        return WAITING_MENU

    try:
        await update.message.delete()
    except Exception:
        pass

    msg = await context.bot.send_message(chat_id=chat_id, text=t(user_id, "checking_cookie"))

    def _validate_cookie(c: str):
        s = _make_roblox_session(c)
        return s.get("https://users.roblox.com/v1/users/authenticated", timeout=10)

    r = await loop.run_in_executor(executor, _validate_cookie, cookie)
    if r.status_code != 200:
        await msg.edit_text(t(user_id, "invalid_cookie"))
        return WAITING_MENU

    user        = r.json()
    method_name = t(user_id, "method_camera") if method == "camera" else t(user_id, "method_id")
    attempt_was_deducted = False

    attempts_display = await loop.run_in_executor(executor, db_get_attempts, user_id)
    account_name     = user.get("name") or user.get("displayName") or "RobloxUser"
    await msg.edit_text(
        t(user_id, "processing", name=account_name, method=method_name, attempts=attempts_display),
        parse_mode="Markdown",
    )

    _start_time = _time.time()

    # ── ЭТАП 1: Быстрая проверка без 2FA (быстро ссылка = нет 2FA) ──
    # Проверяем напрямую: если на аккаунте нет 2FA, Roblox выдаст ссылку за 1-2 сек бесплатно!
    fast_link = await loop.run_in_executor(executor, get_url_via_api, cookie, method)
    elapsed = round(_time.time() - _start_time)

    if fast_link and _is_persona_link(fast_link):
        logging.info("Ссылка получена быстро (на аккаунте нет 2FA) за %s сек — бесплатно", elapsed)
        lang = db_get_lang(user_id) or (tg.language_code[:2] if tg and tg.language_code else "ru")
        btn_continue = get_continue_button_text(method, lang)
        link_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_continue, url=fast_link)],
            [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
        ])
        msg_text = t(user_id, "link_received")
        await msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=link_kb)
        await send_log(context, user_id, username, method, elapsed, has_2fa=False)
        attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{t(user_id, 'main_menu_title')}\n\n"
                f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
                f"{t(user_id, 'main_menu_action')}"
            ),
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id),
        )
        return WAITING_MENU

    # ── ЭТАП 2: Быстрая ссылка не получена -> на аккаунте реально требуется 2FA ──
    if not has_2fa:
        # Пользователь выбрал режим "Без 2FA", но на аккаунте включена 2FA
        logging.info("Пользователь выбрал Без 2FA, но аккаунт требует 2FA")
        await msg.edit_text(
            t(user_id, "need_2fa_mode"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_MENU

    # Режим "С 2FA" — списываем попытку только сейчас, когда убедились, что 2FA реально нужна!
    is_sub_active, _, _ = await loop.run_in_executor(executor, db_get_subscription, user_id)
    if not is_sub_active:
        try:
            spent_ok = await loop.run_in_executor(
                executor, db_deduct_attempt, user_id, username
            )
        except Exception as e:
            logging.error("db_deduct_attempt error: %s", e)
            lang = db_get_lang(user_id) or "ru"
            await msg.edit_text(
                "❌ Ошибка сервера, попробуйте позже."
                if lang == "ru" else
                "❌ Server error, please try later."
            )
            return WAITING_MENU
        if not spent_ok:
            lang = db_get_lang(user_id) or "ru"
            await msg.edit_text(
                "❌ Нет попыток 2FA!" if lang == "ru" else "❌ No 2FA attempts!"
            )
            return WAITING_MENU
        attempt_was_deducted = True

    # Обновляем отображение попыток после списания
    attempts_display = await loop.run_in_executor(executor, db_get_attempts, user_id)
    await msg.edit_text(
        t(user_id, "processing", name=account_name, method=method_name, attempts=attempts_display),
        parse_mode="Markdown",
    )

    # Запускаем 2FA воркер + параллельный апдейт сообщения каждые 15 сек
    _worker_start = _time.time()

    async def _progress_updater():
        dots = ["⏳", "⌛"]
        i = 0
        while True:
            await asyncio.sleep(15)
            elapsed_now = round(_time.time() - _worker_start)
            try:
                await msg.edit_text(
                    t(user_id, "processing", name=account_name, method=method_name, attempts=attempts_display)
                    + f"\n\n{dots[i % 2]} {elapsed_now} сек...",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            i += 1

    _progress_task = asyncio.create_task(_progress_updater())
    try:
        result = await loop.run_in_executor(executor, call_worker_2fa, cookie, method)
    finally:
        _progress_task.cancel()

    elapsed = round(_time.time() - _worker_start)
    logging.info(
        "call_worker_2fa result: elapsed=%ss, result=%s",
        elapsed,
        str(result)[:120] if result else None,
    )

    # ── Challenge от воркера: нужен email-код ──
    if isinstance(result, dict) and result.get("challengeId") and result.get("csrf"):
        lang = db_get_lang(user_id) or "ru"
        _cleanup_pending_2fa()
        async with _pending_2fa_lock:
          # Если запись уже есть (двойной вызов) — возвращаем попытку и выходим
          if user_id in pending_2fa and attempt_was_deducted:
              try:
                  await loop.run_in_executor(executor, db_upsert_user, user_id, username, 1, -PRICE_2FA)
              except Exception as e:
                  logging.error("pending_2fa double-write: не удалось вернуть попытку: %s", e)
              await msg.edit_text(t(user_id, "failed_returned"))
              return WAITING_MENU
          pending_2fa[user_id] = {
              "cookie":               cookie,
              "csrf":                 result["csrf"],
              "challenge_id":         result["challengeId"],
              # innerChallengeId — реальный ID для twostepverification API;
              # если воркер не вернул — используем outer как fallback
              "inner_challenge_id":   result.get("innerChallengeId") or result["challengeId"],
              "user_roblox_id":       user.get("id"),
              "method":               method,         # "camera" or "id"
              "attempt_was_deducted": attempt_was_deducted,
              "chat_id":              chat_id,
              "username":             username,
              "created_at":           _time.time(),
          }
        code_prompt = (
            "📧 *Код отправлен на email*\n\nВведите 6-значный код из письма Roblox:"
            if lang == "ru" else
            "📧 *Code sent to email*\n\nEnter the 6-digit code from Roblox email:"
        )
        await msg.edit_text(
            code_prompt,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_cancel"), callback_data="cancel_2fa")
            ]]),
        )
        return WAITING_2FA_CODE

    # ── Ссылка получена ──
    link_ok = result and isinstance(result, str) and _is_persona_link(result)
    if link_ok:
        lang = db_get_lang(user_id) or (tg.language_code[:2] if tg and tg.language_code else "ru")
        btn_continue = get_continue_button_text(method, lang)
        link_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_continue, url=result)],
            [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
        ])
        
        # Если ссылка получена сразу без ввода 2FA кода — возвращаем списанную попытку (бесплатно)
        if attempt_was_deducted:
            try:
                await loop.run_in_executor(
                    executor, db_upsert_user, user_id, username, 1, -PRICE_2FA
                )
                attempt_was_deducted = False
                has_2fa = False
            except Exception as e:
                logging.error("Не удалось вернуть попытку: %s", e)
            msg_text = t(user_id, "link_received_free_2fa")
        else:
            msg_text = t(user_id, "link_received")

        await msg.edit_text(msg_text, parse_mode="Markdown", reply_markup=link_kb)
        await send_log(context, user_id, username, method, elapsed, has_2fa=has_2fa)
        # Реферальное начисление: только если реально списали попытку (платная 2FA)
        if has_2fa and attempt_was_deducted is False is False:
            # has_2fa может стать False если ссылка пришла без email-кода → бесплатно, пропускаем
            pass
        if has_2fa:
            try:
                referrer_id = await loop.run_in_executor(executor, db_get_referrer, user_id)
                if referrer_id:
                    new_atts = await loop.run_in_executor(
                        executor, db_add_ref_reward, referrer_id, REFERRAL_PERCENT * PRICE_2FA
                    )
                    if new_atts > 0:
                        try:
                            await context.bot.send_message(
                                chat_id=referrer_id,
                                text=(
                                    f"🎉 Ваш реферал получил 2FA-ссылку!\n"
                                    f"Вам начислена *{new_atts}* попытка 2FA."
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception:
                            pass
            except Exception as e:
                logging.error("Referral reward error (cookie): %s", e)
        attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{t(user_id, 'main_menu_title')}\n\n"
                f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
                f"{t(user_id, 'main_menu_action')}"
            ),
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id),
        )
        return WAITING_MENU

    # ── Ошибка — возвращаем попытку ──
    if attempt_was_deducted:
        try:
            await loop.run_in_executor(
                executor, db_upsert_user, user_id, username, 1, -PRICE_2FA
            )
        except Exception as e:
            logging.error("Не удалось вернуть попытку: %s", e)

    await msg.edit_text(t(user_id, "failed_returned"))
    attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"{t(user_id, 'main_menu_title')}\n\n"
            f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
            f"{t(user_id, 'main_menu_action')}"
        ),
        parse_mode="Markdown",
        reply_markup=main_menu_kb(user_id),
    )
    return WAITING_MENU


async def receive_2fa_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Принимает 6-значный код 2FA и верифицирует через Roblox API."""
    user_id  = update.effective_user.id
    tg       = update.effective_user
    username = tg.username or tg.first_name or "Аноним"
    lang     = db_get_lang(user_id) or "ru"
    loop     = asyncio.get_running_loop()

    # Кнопка "Отмена"
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        if query.data == "cancel_2fa":
            ctx = pending_2fa.pop(user_id, None)
            attempt_returned = False
            if ctx and ctx.get("attempt_was_deducted"):
                try:
                    await loop.run_in_executor(
                        executor, db_upsert_user, user_id, username, 1, -PRICE_2FA
                    )
                    attempt_returned = True
                except Exception as e:
                    logging.error("cancel_2fa: не удалось вернуть попытку: %s", e)
            if attempt_returned:
                cancel_msg = (
                    "❌ Верификация отменена. Попытка возвращена."
                    if lang == "ru" else
                    "❌ Verification cancelled. Attempt returned."
                )
            else:
                cancel_msg = (
                    "❌ Верификация отменена."
                    if lang == "ru" else
                    "❌ Verification cancelled."
                )
            await query.edit_message_text(
                cancel_msg,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
                ]]),
            )
            return WAITING_MENU
        return WAITING_2FA_CODE

    code_text = update.message.text.strip()
    ctx       = pending_2fa.get(user_id)

    if not ctx:
        await update.message.reply_text(
            "⚠️ Сессия истекла, начните заново /start"
            if lang == "ru" else
            "⚠️ Session expired, restart /start"
        )
        return WAITING_MENU

    # Валидация формата — ровно 6 цифр
    if not re.match(r'^\d{6}$', code_text):
        prompt = (
            "❌ Код должен содержать 6 цифр. Попробуйте ещё раз:"
            if lang == "ru" else
            "❌ Code must be 6 digits. Try again:"
        )
        await update.message.reply_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_cancel"), callback_data="cancel_2fa")
            ]]),
        )
        return WAITING_2FA_CODE

    chat_id      = update.effective_chat.id
    thinking_msg = await update.message.reply_text(
        "⏳ Проверяю код..." if lang == "ru" else "⏳ Verifying code..."
    )

    try:
        await update.message.delete()
    except Exception:
        pass

    success, link_or_error = await loop.run_in_executor(
        executor,
        verify_2fa_code,
        ctx["cookie"],
        ctx["user_roblox_id"],
        ctx["csrf"],
        ctx["challenge_id"],
        code_text,
        ctx.get("method", "camera"),
        ctx.get("inner_challenge_id"),   # inner ID для email/verify
    )

    # ── Успех ──
    if success and link_or_error and link_or_error.startswith("http"):
        pending_2fa.pop(user_id, None)
        lang = db_get_lang(user_id) or (tg.language_code[:2] if tg and tg.language_code else "ru")
        btn_continue = get_continue_button_text(ctx.get("method"), lang)
        link_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_continue, url=link_or_error)],
            [InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")],
        ])
        await thinking_msg.edit_text(
            t(user_id, "link_received"), parse_mode="Markdown", reply_markup=link_kb
        )
        await send_log(context, user_id, username, ctx["method"], 0, has_2fa=True)
        # Реферальное начисление: 20% рефереру за успешную 2FA
        try:
            referrer_id = await loop.run_in_executor(executor, db_get_referrer, user_id)
            if referrer_id:
                new_atts = await loop.run_in_executor(
                    executor, db_add_ref_reward, referrer_id, REFERRAL_PERCENT * PRICE_2FA
                )
                if new_atts > 0:
                    try:
                        await context.bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                f"🎉 Ваш реферал получил 2FA-ссылку!\n"
                                f"Вам начислена *{new_atts}* попытка 2FA."
                            ),
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                logging.info("Ref reward: referrer=%s earned %.2f (from user=%s)", referrer_id, REFERRAL_PERCENT * PRICE_2FA, user_id)
        except Exception as e:
            logging.error("Referral reward error (2fa_code): %s", e)
        attempts, sub_status = await loop.run_in_executor(executor, _sub_status_text, user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{t(user_id, 'main_menu_title')}\n\n"
                f"{t(user_id, 'main_menu_attempts', attempts=attempts, sub_status=sub_status)}\n\n"
                f"{t(user_id, 'main_menu_action')}"
            ),
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id),
        )
        return WAITING_MENU

    # ── Неверный/исчерпан код — попытка НЕ возвращается, пользователь может попробовать снова ──
    if link_or_error in ("invalid_code", "rate_limited"):
        if link_or_error == "rate_limited":
            err_text = (
                "⏳ Слишком много попыток. Подождите и попробуйте снова."
                if lang == "ru" else
                "⏳ Too many attempts. Wait and try again."
            )
        else:
            err_text = (
                "❌ Неверный код. Попробуйте ещё раз:"
                if lang == "ru" else
                "❌ Invalid code. Try again:"
            )
        await thinking_msg.edit_text(
            err_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_cancel"), callback_data="cancel_2fa")
            ]]),
        )
        return WAITING_2FA_CODE

    # ── Сессия истекла (код устарел) — возвращаем попытку и просим начать заново ──
    if link_or_error == "session_expired":
        ctx_exp = pending_2fa.pop(user_id, None)
        if ctx_exp and ctx_exp.get("attempt_was_deducted"):
            try:
                await loop.run_in_executor(
                    executor, db_upsert_user, user_id, username, 1, -PRICE_2FA
                )
            except Exception as e:
                logging.error("session_expired: не удалось вернуть попытку: %s", e)
        exp_text = (
            "⚠️ Код истёк или сессия устарела. Попытка возвращена — начните заново /start"
            if lang == "ru" else
            "⚠️ Code expired or session invalid. Attempt returned — restart /start"
        )
        await thinking_msg.edit_text(
            exp_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
            ]]),
        )
        return WAITING_MENU

    # ── Серверная ошибка (link_failed / exception / неизвестный код) — возвращаем попытку ──
    ctx_final = pending_2fa.pop(user_id, None)
    if ctx_final and ctx_final.get("attempt_was_deducted"):
        try:
            await loop.run_in_executor(
                executor, db_upsert_user, user_id, username, 1, -PRICE_2FA
            )
        except Exception as e:
            logging.error("receive_2fa_code: не удалось вернуть попытку: %s", e)

    if link_or_error == "link_failed":
        err_text = (
            "❌ Код принят, но ссылку получить не удалось. Попытка возвращена — попробуйте ещё раз."
            if lang == "ru" else
            "❌ Code accepted but failed to get the link. Attempt returned — please try again."
        )
    elif link_or_error == "exception":
        err_text = (
            "❌ Сетевая ошибка при верификации. Попытка возвращена."
            if lang == "ru" else
            "❌ Network error during verification. Attempt returned."
        )
    else:
        err_text = (
            "❌ Ошибка верификации, попытка возвращена."
            if lang == "ru" else
            "❌ Verification error, attempt returned."
        )
    logging.error("receive_2fa_code: terminal error code=%s user=%s", link_or_error, user_id)
    await thinking_msg.edit_text(
        err_text,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(t(user_id, "btn_back"), callback_data="main_menu")
        ]]),
    )
    return WAITING_MENU

# ──────────────────────────── MAIN ────────────────────────────

def main():
    token = get_bot_token()
    if not token:
        raise RuntimeError("BOT_TOKEN не указан!")
    global BOT_TOKEN, ADMIN_IDS, CRYPTO_BOT_TOKEN, LOG_CHANNEL_ID
    BOT_TOKEN = token
    ADMIN_IDS = load_admin_ids()
    CRYPTO_BOT_TOKEN = load_crypto_bot_token()
    LOG_CHANNEL_ID = load_log_channel_id()
    db_init()

    if ADMIN_IDS:
        logging.info("👑 Администраторы загружены (%d): %s", len(ADMIN_IDS), sorted(list(ADMIN_IDS)))
    else:
        logging.warning("⚠️ Внимание: Администраторы не заданы! Замените 00000000 в bot.py или admin_ids.txt на ваш ID.")

    if CRYPTO_BOT_TOKEN:
        logging.info("💳 Оплата через CryptoBot активирована (токен: %s...)", CRYPTO_BOT_TOKEN[:8])
    else:
        logging.info("ℹ️ CryptoBot токен не задан (покупка через @CryptoBot отключена)")
    if LOG_CHANNEL_ID:
        logging.info("📢 Логирование в канал активно: %s", LOG_CHANNEL_ID)
    else:
        logging.info("ℹ️ Канал для логов не задан (замените -1000000000000 в log_channel.txt или bot.py)")

    from telegram.request import HTTPXRequest
    req = HTTPXRequest(
        connection_pool_size=16,
        read_timeout=10,
        write_timeout=10,
        connect_timeout=8,
    )
    upd_req = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=15,
        write_timeout=10,
        connect_timeout=8,
    )
    builder = ApplicationBuilder().token(token).request(req).get_updates_request(upd_req)
    logging.info("Telegram: прямое подключение")

    application = builder.build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("lang",  lang_command),
            CommandHandler("admin", admin_command),
        ],
        states={
            WAITING_LANG: [CallbackQueryHandler(handle_callback)],
            WAITING_MENU: [CallbackQueryHandler(handle_callback)],
            WAITING_COOKIE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_BUY: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_PAYMENT: [CallbackQueryHandler(handle_callback)],
            WAITING_PROMO: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_PASSWORD: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_CHANGE_PASSWORD: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_ADD_BALANCE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_ADD_BALANCE_AMOUNT: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_REMOVE_BALANCE: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_REMOVE_BALANCE_AMOUNT: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_ADD_SUB: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_CREATE_PROMO: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
            WAITING_ADMIN_BROADCAST: [
                CallbackQueryHandler(handle_callback),
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO) & ~filters.COMMAND,
                    receive_text,
                ),
            ],
            WAITING_2FA_CODE: [
                CallbackQueryHandler(receive_2fa_code),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_2fa_code),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("lang",  lang_command),
            CommandHandler("admin", admin_command),
        ],
        per_user=True,
        per_chat=True,
        block=False,
    )
    application.add_handler(conv)
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        poll_interval=0.0,
        timeout=20,
        read_timeout=10,
        write_timeout=10,
        connect_timeout=8,
    )


# ══════════════════════════════════════════════════════════════════════
# 🔧 EMBEDDED WORKER (Flask HTTP-сервер для Railway)
# Запуск: RUN_MODE=worker python bot.py
#   или:  gunicorn "bot:_create_worker_app()" -w 1 -b 0.0.0.0:$PORT --timeout 660
# ══════════════════════════════════════════════════════════════════════

def _create_worker_app():
    """Создаёт и возвращает Flask-приложение воркера."""
    import re as _re
    import json as _json
    import base64 as _base64
    import random as _random
    import threading as _wt
    import concurrent.futures as _wf
    from collections import deque as _deque

    try:
        from flask import Flask as _Flask, request as _wreq, jsonify as _wjsonify
    except ImportError:
        raise RuntimeError("Flask не установлен. Добавьте flask в requirements.txt")

    _wapp = _Flask(__name__)
    _WSECRET = os.environ.get("WORKER_SECRET", "")

    # ── Прокси пул (переиспользуем из бота) ──
    _wproxy_pool: list[str] = list(_proxy_pool) if _proxy_pool else []
    _wproxy_idx  = 0
    _wproxy_lock = _wt.Lock()
    _wproxy_health: dict[str, dict] = {}
    _wproxy_health_lock = _wt.Lock()
    _WPROXY_FAIL_TH  = 6
    _WPROXY_COOLDOWN = 30

    def _wnext_proxy():
        global _wproxy_idx
        if not _wproxy_pool:
            return None
        now = _time.time()
        with _wproxy_lock:
            for _ in range(len(_wproxy_pool)):
                idx = _wproxy_idx % len(_wproxy_pool)
                _wproxy_idx += 1
                url = _wproxy_pool[idx]
                with _wproxy_health_lock:
                    h = _wproxy_health.get(url, {"fails": 0, "last_fail": 0})
                    if h["fails"] >= _WPROXY_FAIL_TH:
                        if (_time.time() - h["last_fail"]) < _WPROXY_COOLDOWN:
                            continue
                        _wproxy_health[url] = {"fails": 0, "last_fail": 0}
                return url
        return _wproxy_pool[0] if _wproxy_pool else None

    def _wproxy_dict(proxy_str):
        return _parse_proxy(proxy_str) if proxy_str else None

    _WDEADLINE   = int(os.environ.get("DEADLINE", 600))
    _WBATCH_SIZE = int(os.environ.get("BATCH_SIZE", max(len(_wproxy_pool), 20)))
    _WPAUSE      = float(os.environ.get("PAUSE_BURST", 0.02))
    _WPAUSE_429  = float(os.environ.get("PAUSE_429", 1.5))
    _WREQ_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 12))
    _WMAX_REQS   = 150000

    _WURL_VERIFY = "https://apis.roblox.com/age-verification-service/v1/persona-id-verification/start-verification"
    _WUA = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    def _wmake_session(cookie, proxy_str=None):
        s = requests.Session()
        s.cookies[".ROBLOSECURITY"] = cookie
        s.headers.update({
            "User-Agent": _random.choice(_WUA),
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://www.roblox.com",
        })
        if proxy_str:
            pd = _wproxy_dict(proxy_str)
            if pd:
                s.proxies.update(pd)
        return s

    def _wbody(method):
        if method == "id":
            return {"generateLink": True, "ageEstimation": False, "parentVerification": False}
        return {"generateLink": True, "ageEstimation": True, "parentVerification": False}

    def _wdecode_meta(cmeta):
        try:
            padded = cmeta + "=" * (-len(cmeta) % 4)
            return _json.loads(_base64.b64decode(padded).decode("utf-8", errors="ignore"))
        except Exception:
            return {}

    def _wone_request(cookie, csrf, method, proxy_str):
        try:
            s = _wmake_session(cookie, proxy_str)
            if csrf:
                s.headers["x-csrf-token"] = csrf
            r = s.post(_WURL_VERIFY, json=_wbody(method), timeout=_WREQ_TIMEOUT)
            new_csrf = r.headers.get("x-csrf-token")
            link = extract_link_from_raw(r.text)
            if link:
                return {"link": link, "status": r.status_code, "newCsrf": new_csrf}
            if r.status_code == 403:
                ctype = r.headers.get("rblx-challenge-type", "")
                cid   = r.headers.get("rblx-challenge-id", "")
                cmeta = r.headers.get("rblx-challenge-metadata", "")
                return {"status": 403, "challengeType": ctype, "challengeId": cid,
                        "challengeMeta": cmeta, "newCsrf": new_csrf}
            return {"status": r.status_code, "newCsrf": new_csrf}
        except Exception as e:
            return {"status": 0, "error": str(e)}

    def _wsend_email(cookie, user_id, csrf, cid, cmeta):
        try:
            meta = _wdecode_meta(cmeta)
            inner = (meta.get("challengeId") or meta.get("twostepVerificationChallengeId")
                     or meta.get("challenge_id") or cid)
            action = meta.get("actionType") or "Generic"
            s = _wmake_session(cookie)
            if csrf:
                s.headers["x-csrf-token"] = csrf
            url = f"https://twostepverification.roblox.com/v1/users/{user_id}/challenges/email/send-code"
            r = s.post(url, json={"actionType": action, "challengeId": inner}, timeout=12)
            if r.status_code == 403 and r.headers.get("x-csrf-token"):
                s.headers["x-csrf-token"] = r.headers["x-csrf-token"]
                r = s.post(url, json={"actionType": action, "challengeId": inner}, timeout=12)
            return r.status_code in (200, 204), inner, s.headers.get("x-csrf-token") or csrf
        except Exception:
            return False, cid, csrf

    def _wget_uid_csrf(cookie, method):
        try:
            s = _wmake_session(cookie)
            r = s.get("https://users.roblox.com/v1/users/authenticated", timeout=10)
            uid = r.json().get("id") if r.status_code == 200 else None
            # get csrf via attempt
            r2 = s.post(_WURL_VERIFY, json=_wbody(method), timeout=12)
            csrf = r2.headers.get("x-csrf-token")
            link = extract_link_from_raw(r2.text)
            if link:
                return uid, f"__LINK__:{link}"
            if csrf and r2.status_code == 403:
                s.headers["x-csrf-token"] = csrf
                r3 = s.post(_WURL_VERIFY, json=_wbody(method), timeout=12)
                link = extract_link_from_raw(r3.text)
                if link:
                    return uid, f"__LINK__:{link}"
                csrf = r3.headers.get("x-csrf-token") or csrf
            return uid, csrf
        except Exception:
            return None, None

    def _wrun(cookie, duration_sec, method):
        uid, tok = _wget_uid_csrf(cookie, method)
        if tok and tok.startswith("__LINK__:"):
            return tok[9:]
        csrf = tok
        if not csrf or not uid:
            return None

        total = 0
        email_sent = False
        active_cid = active_inner = active_csrf = None
        consecutive_429 = 0
        start = _time.time()
        workers = max(len(_wproxy_pool) * 2, 50) if _wproxy_pool else 20
        ex = _wf.ThreadPoolExecutor(max_workers=workers)
        found_link = None

        try:
            while (time.time() - start) < duration_sec and total < _WMAX_REQS:
                proxies = _wproxy_pool if _wproxy_pool else [None]
                futs = [ex.submit(_wone_request, cookie, csrf, method, p) for p in proxies]
                results = []
                for f in _wf.as_completed(futs):
                    r = f.result()
                    results.append(r)
                    total += 1
                    if r.get("link") and not found_link:
                        found_link = r["link"]
                        break
                    if not email_sent and r.get("status") == 403 and r.get("challengeType") and r.get("challengeId"):
                        req_csrf = r.get("newCsrf") or csrf
                        ok, inner, new_csrf = _wsend_email(cookie, uid, req_csrf, r["challengeId"], r.get("challengeMeta", ""))
                        if ok:
                            email_sent = True
                            active_cid   = r["challengeId"]
                            active_inner = inner
                            active_csrf  = new_csrf
                    if r.get("newCsrf"):
                        csrf = r["newCsrf"]
                if found_link:
                    break
                has_429 = any(r.get("status") == 429 for r in results)
                consecutive_429 = (consecutive_429 + 1) if has_429 else 0
                if consecutive_429 >= 3:
                    _time.sleep(min(_WPAUSE_429 * (2 ** (consecutive_429 - 3)), 30.0))
                elif has_429:
                    _time.sleep(_WPAUSE_429)
                else:
                    _time.sleep(_WPAUSE)
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

        if found_link:
            return found_link
        if email_sent and active_cid:
            return {"challengeId": active_cid, "innerChallengeId": active_inner or active_cid,
                    "csrf": active_csrf or csrf, "emailSent": True}
        return None

    _WMAXCON = int(os.environ.get("MAX_CONCURRENT_REQUESTS", 3))
    _wsem    = _wt.Semaphore(_WMAXCON)

    @_wapp.route("/health", methods=["GET"])
    def _whealth():
        return _wjsonify({"status": "ok", "mode": "embedded-worker",
                          "deadline_sec": _WDEADLINE, "proxy_count": len(_wproxy_pool)})

    @_wapp.route("/get-link", methods=["POST"])
    def _wget_link():
        secret = _wreq.headers.get("X-Secret") or (_wreq.json or {}).get("secret", "")
        if _WSECRET and secret != _WSECRET:
            return _wjsonify({"error": "unauthorized"}), 401
        data   = _wreq.json or {}
        cookie = data.get("cookie", "").strip()
        method = data.get("method", "camera").strip()
        if not cookie:
            return _wjsonify({"error": "cookie required"}), 400
        if not _wsem.acquire(blocking=False):
            return _wjsonify({"error": "worker busy"}), 503
        try:
            result = _wrun(cookie, _WDEADLINE, method)
        except Exception as e:
            logging.error("/get-link worker error: %s", e)
            return _wjsonify({"error": "failed"}), 500
        finally:
            _wsem.release()
        if isinstance(result, str):
            return _wjsonify({"link": result})
        if isinstance(result, dict) and result.get("challengeId"):
            return _wjsonify(result)
        return _wjsonify({"error": "не смог получить"}), 500

    return _wapp


def _run_worker():
    """Запускает embedded worker (Flask HTTP-сервер)."""
    _wapp = _create_worker_app()
    port = int(os.environ.get("PORT", 8080))
    logging.info("🔧 Запуск embedded Worker на порту %d", port)
    _wapp.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    run_mode = os.environ.get("RUN_MODE", "bot").lower()
    if run_mode == "worker":
        _run_worker()
    else:
        main()
