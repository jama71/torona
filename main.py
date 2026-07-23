import asyncio
import base64
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)

import json
import time

import aiohttp
import yt_dlp

try:
    from shazamio import Shazam
except ImportError:
    Shazam = None

# ------------------------------------------------------------
# ffmpeg setup
# ------------------------------------------------------------
try:
    import imageio_ffmpeg

    _real_ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = os.path.join(tempfile.gettempdir(), "ffmpeg_bin")
    os.makedirs(_ffmpeg_dir, exist_ok=True)
    FFMPEG_PATH = os.path.join(_ffmpeg_dir, "ffmpeg")
    if not os.path.exists(FFMPEG_PATH):
        try:
            os.symlink(_real_ffmpeg, FFMPEG_PATH)
        except Exception:
            shutil.copy(_real_ffmpeg, FFMPEG_PATH)
        os.chmod(FFMPEG_PATH, 0o755)
    os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
    os.environ.setdefault("FFMPEG_BINARY", FFMPEG_PATH)
except Exception as e:
    logging.getLogger("bot").warning("imageio-ffmpeg setup failed: %s", e)
    FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

try:
    from pydub import AudioSegment

    AudioSegment.converter = FFMPEG_PATH
except Exception:
    pass

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
GENERAL_PROXY = os.getenv("PROXY_URL", "").strip() or None


def _repair_cookie_line(line: str) -> str:
    if line.startswith("#") or not line.strip():
        return line
    if line.count("\t") == 6:
        return line
    parts = line.split()
    if len(parts) == 7:
        return "\t".join(parts)
    return line


def _normalize_cookies_content(content: str) -> str:
    return "\n".join(_repair_cookie_line(l) for l in content.splitlines()) + "\n"


def _count_valid_cookie_lines(content: str) -> tuple[int, int]:
    data_lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
    valid = [l for l in data_lines if l.count("\t") == 6]
    return len(valid), len(data_lines)


DEFAULT_YOUTUBE_COOKIES = """# Netscape HTTP Cookie File
# https://curl.haxx.se/rfc/cookie_spec.html
# This is a generated file! Do not edit.

.youtube.com\tTRUE\t/\tTRUE\t1819078132\tLOGIN_INFO\tAFmmF2swRQIhAPTjGCIxT8jRFvu-UolA342zLgu4Wa_2Zg92vp9En0f1AiAaa9qyD5ILBnLeo6OgnSGLGVRv0IKnIfpUwAlAiijq4A:QUQ3MjNmd05EeHR1Vl9DVlpmcXZpZ1dGRjRVdXdGWHlBNHdDc2Y4TnJsNkNfRmZ1Uk5IOUV2S0N0V1Y0dmZRdVgxWXpXcURnbE1Oa2VhX2JReEdWMVU4WU9kVWVmYTFPV1NOeHlocnlpV0lUQk5idzRWN2o2SWY5SWlWUlhxVkUwMWpCOHdOdXFudmxmWGdWejlTdjZUUGpQWWVfbGs0NWhB
.youtube.com\tTRUE\t/\tTRUE\t1819369279\tPREF\tf4=4000000&f6=40000000&tz=Europe.Moscow&f5=30000&f7=100
.youtube.com\tTRUE\t/\tFALSE\t1819216487\tSID\tg.a000AgnbyEYzMLHllQZOkmYXPdAq3Dj1fAf0VctQAtC_gUXab91kpU9dZecQsB5RRO-E-fmk8QACgYKAYASARESFQHGX2Mi0g05dheS9FYBwFBrRZLvOBoVAUF8yKrYZ_w8cDPohd7oXUydNbfv0076
.youtube.com\tTRUE\t/\tTRUE\t1819216487\t__Secure-1PSID\tg.a000AgnbyEYzMLHllQZOkmYXPdAq3Dj1fAf0VctQAtC_gUXab91kTUFP5u_URqr4RLf1RAc7qQACgYKAUgSARESFQHGX2MiClb-98xP_LHUjxzJnfOixRoVAUF8yKrxoHf6nlzwh5I3ZjzNT_R-0076
.youtube.com\tTRUE\t/\tTRUE\t1819216487\t__Secure-3PSID\tg.a000AgnbyEYzMLHllQZOkmYXPdAq3Dj1fAf0VctQAtC_gUXab91ksdQIslqIrxmyjfnwdSbKAwACgYKAXQSARESFQHGX2MijIy-KUAzMVhqjzBcy9BwzBoVAUF8yKpvZm8xRlFViIpBH21_SFv40076
.youtube.com\tTRUE\t/\tFALSE\t1819216487\tHSID\tA5iCJpeoCJ2ie9azx
.youtube.com\tTRUE\t/\tTRUE\t1819216487\tSSID\tAE0d43TagniTeg68R
.youtube.com\tTRUE\t/\tFALSE\t1819216487\tAPISID\tYj70mCU8-qbvexyA/Awam3JMJ3Wgp0rLds
.youtube.com\tTRUE\t/\tTRUE\t1819216487\tSAPISID\t6u-tLv5mRDPMRq0F/A2ikLOYfuqU5tsRvO
.youtube.com\tTRUE\t/\tTRUE\t1819216487\t__Secure-1PAPISID\t6u-tLv5mRDPMRq0F/A2ikLOYfuqU5tsRvO
.youtube.com\tTRUE\t/\tTRUE\t1819216487\t__Secure-3PAPISID\t6u-tLv5mRDPMRq0F/A2ikLOYfuqU5tsRvO
.youtube.com\tTRUE\t/\tTRUE\t1816345286\t__Secure-1PSIDTS\tsidts-CjEBPWEu2bFKuCrVKitykAS1Yg9RNO8ni3OR_Kp5Pi2ARUSTM3oMEvBYciM5MnETLkTOEAA
.youtube.com\tTRUE\t/\tTRUE\t1816345286\t__Secure-3PSIDTS\tsidts-CjEBPWEu2bFKuCrVKitykAS1Yg9RNO8ni3OR_Kp5Pi2ARUSTM3oMEvBYciM5MnETLkTOEAA
.youtube.com\tTRUE\t/\tFALSE\t1816345290\tSIDCC\tAKEyXzUolKajdTygx5UzGp4pGXctqE_D9byXYjYtllffuKdSe1NjaOizavFKxDszrKeUalQ1
.youtube.com\tTRUE\t/\tTRUE\t1816345290\t__Secure-1PSIDCC\tAKEyXzUI5fQyhfizthBUPsnSn37I3QIDpTVoXYPrT59pGCakSeV7355d1YGYXUCniqL4BzJg5A
.youtube.com\tTRUE\t/\tTRUE\t1816345290\t__Secure-3PSIDCC\tAKEyXzXRFdHWQLJ_kGcioFq7UkJSxCn8WVilfs_pqw0JdI8f3zvOJkGg2cRqTePiEx1_xKeEpQ
.youtube.com\tTRUE\t/\tTRUE\t1800361290\tVISITOR_INFO1_LIVE\twgdW23KNv4Y
.youtube.com\tTRUE\t/\tTRUE\t1800361290\tVISITOR_PRIVACY_METADATA\tCgJVWhIEGgAgUg%3D%3D
.youtube.com\tTRUE\t/\tTRUE\t1800361253\t__Secure-YNID\t20.YT=oKxGC8RBOKMAfttV5lr5ZRDRLG_iIJ7ZRTrVPvNR9fsfB46IlZdCHNK2hg7VEtCDypQ7szpq8_BGj_cSSQ-h9b-eY4o6N-NN1J6jO0xdIpIUoS8fUzauxWqJ50qCTG9Gd8qCYpJ8b5Bwrk2tgCQ2vvjVpTXOjm-lbYyk1yNyxCKcs08Iu_ustbrx2gEV4aTGMPCh4cIB8Pm6PrsuTl6jbT_10zse0cF83aerHzi-TNtGEl2ZRfrr78tLkJhALIFBR9ENFyoDpPzlfvb4BUKbeqvTi15fo2Sbf2nlYow_7lUCKg0IjyvmpDxa_6zrlW5p4Qxf7Kkp-H0V_2QHwENS0A
.youtube.com\tTRUE\t/\tTRUE\t0\tYSC\tGZn2TispEpo
.youtube.com\tTRUE\t/\tTRUE\t1800361253\t__Secure-ROLLOUT_TOKEN\tCPrC1NDb5MzydhDs2-2IqOCVAxjL36TiyeiVAw%3D%3D
"""


def _write_cookies_file(content: str) -> str:
    cookies_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(content)
    return cookies_path


def _try_load_cookie_candidate(source: str, content: str, logger) -> str | None:
    content = _normalize_cookies_content(content)
    valid, total = _count_valid_cookie_lines(content)
    if total == 0 or valid == 0:
        return None
    path = _write_cookies_file(content)
    return path


def _setup_youtube_cookies() -> str | None:
    logger = logging.getLogger("bot")
    b64 = os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    path_env = os.getenv("COOKIES_FILE", "").strip()

    if b64:
        cleaned = "".join(b64.split())
        padding = len(cleaned) % 4
        if padding:
            cleaned += "=" * (4 - padding)
        try:
            decoded = base64.b64decode(cleaned).decode("utf-8", errors="ignore")
            result = _try_load_cookie_candidate("YOUTUBE_COOKIES_B64", decoded, logger)
            if result:
                return result
        except Exception:
            pass

    if raw:
        result = _try_load_cookie_candidate("YOUTUBE_COOKIES", raw, logger)
        if result:
            return result

    if path_env and os.path.exists(path_env):
        return path_env

    return _try_load_cookie_candidate("default", DEFAULT_YOUTUBE_COOKIES, logger)


COOKIES_FILE = _setup_youtube_cookies()

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DOWNLOAD_ROOT = tempfile.gettempdir()
CACHE_TTL_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

URL_RE = re.compile(r"(https?://\S+)")
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com"),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "tiktok": re.compile(r"tiktok\.com"),
    "pinterest": re.compile(r"(pinterest\.com|pin\.it)"),
    "snapchat": re.compile(r"snapchat\.com"),
}

FILE_CACHE: dict[str, str] = {}
SEARCH_CACHE: dict[str, dict] = {}
SEARCH_RESULTS_PER_PAGE = 8
SEARCH_FETCH_LIMIT = 40
SEARCH_CACHE_TTL_SECONDS = 600

BOT_DISPLAY_NAME = "Bot"
pool: asyncpg.Pool | None = None


# ============================================================
# TRANSLATIONS (Minimalistik o'zgargan matnlar)
# ============================================================
TEXTS = {
    "uz": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": "Texnik ishlar tugadi, /start ni bosing.",
        "send_link": "🔗 Havola yuboring.",
        "downloading": "⏳",
        "caption": "✅ @{bot_username}",
        "detect_music_btn": "🎵 Musiqa",
        "recognizing": "🎧...",
        "not_recognized": "😔 Musiqa topilmadi.",
        "found_song": "🎶 {title} — {artist}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_youtube_link": "🔍 YouTube",
        "lyrics_notice": "📜 Lyrics:",
        "tiktok_unavailable": "⚠️ TikTok ishlamayapti.",
        "unsupported_link": "❌ Yaroqsiz havola.",
        "error": "❌ Xatolik.",
        "no_link": "❗️ Havola yuboring.",
        "admin_only": "⛔ Admin uchun.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Foydalanuvchilar: {count}",
        "broadcast_ask": "✍️ Xabar matnini yuboring:",
        "broadcast_done": "✅ Yuborildi: {count}",
        "file_expired": "⏱ Vaqt tugadi.",
        "lang_set": "✅ O'zbekcha",
        "searching": "🔍...",
        "search_no_results": "😔 Topilmadi.",
        "search_results_range": "{start}-{end} / {total}",
        "help": "ℹ️ Havola yuboring yoki qo'shiq nomini yozing.",
        "btn_lang": "🌐 Til",
        "btn_help": "❓ Yordam",
        "btn_admin": "🛠 Admin",
        "btn_back": "⬅️ Orqaga",
        "btn_stats": "📊 Stats",
        "btn_broadcast": "📢 Broadcast",
        "btn_add_channel": "➕ Kanal qo'shish",
        "btn_list_channels": "📋 Kanallar",
        "ask_channel": "📎 Forward xabar yoki username/ID yuboring:",
        "channel_added": "✅ Qo'shildi: {title}",
        "channel_add_fail_not_admin": "❌ Botni admin qiling.",
        "channel_add_fail": "❌ Aniqlanmadi.",
        "channel_list_empty": "📭 Kanallar yo'q.",
        "channel_list_title": "📋 Kanallar:",
        "channel_removed": "🗑 O'chirildi.",
        "subscribe_required": "⚠️ Kanallarga a'zo bo'ling:",
        "check_sub_btn": "✅ Tekshirish",
        "still_not_subscribed": "❌ A'zo bo'lmadingiz.",
        "now_subscribed": "✅ Rahmat!",
        "channel_type_public": "Ochiq",
        "channel_subs_label": "{count} a'zo",
        "channel_type_private": "Yopiq",
    },
    "ru": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": "Технические работы завершены, нажмите /start.",
        "send_link": "🔗 Отправьте ссылку.",
        "downloading": "⏳",
        "caption": "✅ @{bot_username}",
        "detect_music_btn": "🎵 Музыка",
        "recognizing": "🎧...",
        "not_recognized": "😔 Музыка не найдена.",
        "found_song": "🎶 {title} — {artist}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Текст",
        "btn_youtube_link": "🔍 YouTube",
        "lyrics_notice": "📜 Текст:",
        "tiktok_unavailable": "⚠️ TikTok недоступен.",
        "unsupported_link": "❌ Неподдерживаемая ссылка.",
        "error": "❌ Ошибка.",
        "no_link": "❗️ Отправьте ссылку.",
        "admin_only": "⛔ Только для админов.",
        "admin_panel": "🛠 Админ-панель",
        "stats": "📊 Пользователей: {count}",
        "broadcast_ask": "✍️ Отправьте текст:",
        "broadcast_done": "✅ Отправлено: {count}",
        "file_expired": "⏱ Время истекло.",
        "lang_set": "✅ Русский",
        "searching": "🔍...",
        "search_no_results": "😔 Ничего не найдено.",
        "search_results_range": "{start}-{end} / {total}",
        "help": "ℹ️ Отправьте ссылку или название песни.",
        "btn_lang": "🌐 Язык",
        "btn_help": "❓ Помощь",
        "btn_admin": "🛠 Админ",
        "btn_back": "⬅️ Назад",
        "btn_stats": "📊 Стат",
        "btn_broadcast": "📢 Рассылка",
        "btn_add_channel": "➕ Добавить канал",
        "btn_list_channels": "📋 Каналы",
        "ask_channel": "📎 Перешлите сообщение или отправьте username/ID:",
        "channel_added": "✅ Добавлено: {title}",
        "channel_add_fail_not_admin": "❌ Сделайте бота админом.",
        "channel_add_fail": "❌ Не удалось определить.",
        "channel_list_empty": "📭 Каналов нет.",
        "channel_list_title": "📋 Каналы:",
        "channel_removed": "🗑 Удалено.",
        "subscribe_required": "⚠️ Подпишитесь на каналы:",
        "check_sub_btn": "✅ Проверить",
        "still_not_subscribed": "❌ Вы не подписались.",
        "now_subscribed": "✅ Спасибо!",
        "channel_type_public": "Открытый",
        "channel_subs_label": "{count} subs",
        "channel_type_private": "Закрытый",
    },
    "en": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": "Technical work is complete, press /start.",
        "send_link": "🔗 Send a link.",
        "downloading": "⏳",
        "caption": "✅ @{bot_username}",
        "detect_music_btn": "🎵 Music",
        "recognizing": "🎧...",
        "not_recognized": "😔 Music not found.",
        "found_song": "🎶 {title} — {artist}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_youtube_link": "🔍 YouTube",
        "lyrics_notice": "📜 Lyrics:",
        "tiktok_unavailable": "⚠️ TikTok unavailable.",
        "unsupported_link": "❌ Unsupported link.",
        "error": "❌ Error.",
        "no_link": "❗️ Send a link.",
        "admin_only": "⛔ Admin only.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Users: {count}",
        "broadcast_ask": "✍️ Send text:",
        "broadcast_done": "✅ Sent: {count}",
        "file_expired": "⏱ Expired.",
        "lang_set": "✅ English",
        "searching": "🔍...",
        "search_no_results": "😔 Not found.",
        "search_results_range": "{start}-{end} / {total}",
        "help": "ℹ️ Send link or song title.",
        "btn_lang": "🌐 Language",
        "btn_help": "❓ Help",
        "btn_admin": "🛠 Admin",
        "btn_back": "⬅️ Back",
        "btn_stats": "📊 Stats",
        "btn_broadcast": "📢 Broadcast",
        "btn_add_channel": "➕ Add channel",
        "btn_list_channels": "📋 Channels",
        "ask_channel": "📎 Forward message or send username/ID:",
        "channel_added": "✅ Added: {title}",
        "channel_add_fail_not_admin": "❌ Make bot an admin.",
        "channel_add_fail": "❌ Couldn't detect.",
        "channel_list_empty": "📭 No channels.",
        "channel_list_title": "📋 Channels:",
        "channel_removed": "🗑 Removed.",
        "subscribe_required": "⚠️ Subscribe to channels:",
        "check_sub_btn": "✅ Check",
        "still_not_subscribed": "❌ Not subscribed.",
        "now_subscribed": "✅ Thanks!",
        "channel_type_public": "Public",
        "channel_subs_label": "{count} subs",
        "channel_type_private": "Private",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "uz"
    text = TEXTS[lang].get(key, TEXTS["uz"][key])
    return text.format(**kwargs) if kwargs else text


# ============================================================
# DATABASE
# ============================================================
async def init_db():
    global pool
    pool = await asyncpg.create_pool(dsn=DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as conn:
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                lang TEXT DEFAULT 'uz',
                joined_at TIMESTAMP DEFAULT now()
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS mandatory_channels (
                id SERIAL PRIMARY KEY,
                chat_id BIGINT UNIQUE NOT NULL,
                title TEXT,
                username TEXT,
                is_private BOOLEAN DEFAULT FALSE,
                invite_link TEXT
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS join_requests (
                chat_id BIGINT,
                user_id BIGINT,
                PRIMARY KEY (chat_id, user_id)
            )"""
        )
        await conn.execute(
            """CREATE TABLE IF NOT EXISTS channel_subscribers (
                chat_id BIGINT,
                user_id BIGINT,
                joined_at TIMESTAMP DEFAULT now(),
                PRIMARY KEY (chat_id, user_id)
            )"""
        )


async def add_user_if_missing(user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
        )


async def set_lang(user_id: int, lang: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, lang) VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET lang = EXCLUDED.lang""",
            user_id, lang,
        )


async def get_lang(user_id: int) -> str | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT lang FROM users WHERE user_id=$1", user_id)
        return row["lang"] if row else None


async def get_all_user_ids() -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]


async def count_users() -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def add_channel(chat_id: int, title: str, username: str | None, is_private: bool, invite_link: str | None):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO mandatory_channels (chat_id, title, username, is_private, invite_link)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (chat_id) DO UPDATE SET
                 title=EXCLUDED.title, username=EXCLUDED.username,
                 is_private=EXCLUDED.is_private, invite_link=EXCLUDED.invite_link""",
            chat_id, title, username, is_private, invite_link,
        )


async def list_channels():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM mandatory_channels ORDER BY id")


async def remove_channel(channel_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mandatory_channels WHERE id=$1", channel_id)


async def log_join_request(chat_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO join_requests (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            chat_id, user_id,
        )


async def get_channel_by_chat_id(chat_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM mandatory_channels WHERE chat_id=$1", chat_id)


async def mark_channel_subscriber(chat_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO channel_subscribers (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            chat_id, user_id,
        )


async def unmark_channel_subscriber(chat_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM channel_subscribers WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
        )


async def count_channel_subscribers(chat_id: int) -> int:
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM channel_subscribers WHERE chat_id=$1", chat_id
        )


# ============================================================
# BOT / DISPATCHER
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class EnsureUserRegisteredMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None:
            try:
                await add_user_if_missing(user.id)
            except Exception:
                pass
        return await handler(event, data)


dp.message.outer_middleware(EnsureUserRegisteredMiddleware())
dp.callback_query.outer_middleware(EnsureUserRegisteredMiddleware())


class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_channel = State()


def lang_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )


def music_inline_kb(lang: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "detect_music_btn"), callback_data=f"music:{token}")]
        ]
    )


def song_result_kb(lang: str, title: str, artist: str, youtube_url: str | None) -> InlineKeyboardMarkup:
    query = f"{artist} {title}".strip() or title
    lyrics_url = "https://genius.com/search?q=" + urllib.parse.quote(query)
    rows = [[InlineKeyboardButton(text=t(lang, "btn_lyrics"), url=lyrics_url)]]
    if youtube_url:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_youtube_link"), url=youtube_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_reply_kb(lang: str, is_admin: bool) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text=t(lang, "btn_lang")), KeyboardButton(text=t(lang, "btn_help"))]
    keyboard = [row]
    if is_admin:
        keyboard.append([KeyboardButton(text=t(lang, "btn_admin"))])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_reply_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=t(lang, "btn_stats")), KeyboardButton(text=t(lang, "btn_broadcast"))],
            [KeyboardButton(text=t(lang, "btn_add_channel"))],
            [KeyboardButton(text=t(lang, "btn_list_channels"))],
            [KeyboardButton(text=t(lang, "btn_back"))],
        ],
        resize_keyboard=True,
    )


async def get_user_lang(user_id: int) -> str:
    lang = await get_lang(user_id)
    return lang or "uz"


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================================
# HANDLERS: /start & language
# ============================================================
@router.message(CommandStart())
async def cmd_start(message: Message):
    await add_user_if_missing(message.from_user.id)
    await message.answer(t("uz", "choose_lang"), reply_markup=lang_inline_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    await set_lang(call.from_user.id, lang)
    await call.message.edit_text(t(lang, "lang_set"))
    me = await bot.get_me()
    bot_username = me.username or "bot"
    await call.message.answer(t(lang, "welcome", bot_name=BOT_DISPLAY_NAME, bot_username=bot_username))
    await call.message.answer(
        t(lang, "send_link"),
        reply_markup=user_reply_kb(lang, is_admin(call.from_user.id)),
    )
    await call.answer()


# ============================================================
# HANDLERS: persistent reply-keyboard buttons
# ============================================================
@router.message(F.text.in_({v["btn_lang"] for v in TEXTS.values()}))
async def btn_change_lang(message: Message):
    await message.answer(t("uz", "choose_lang"), reply_markup=lang_inline_kb())


@router.message(F.text.in_({v["btn_help"] for v in TEXTS.values()}))
async def btn_help(message: Message):
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "help"))


@router.message(F.text.in_({v["btn_admin"] for v in TEXTS.values()}))
async def btn_admin_panel(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "admin_panel"), reply_markup=admin_reply_kb(lang))


@router.message(F.text.in_({v["btn_back"] for v in TEXTS.values()}))
async def btn_back_to_user(message: Message, state: FSMContext):
    await state.clear()
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "send_link"), reply_markup=user_reply_kb(lang, is_admin(message.from_user.id)))


# ============================================================
# HANDLERS: admin - stats / broadcast
# ============================================================
@router.message(F.text.in_({v["btn_stats"] for v in TEXTS.values()}))
async def btn_stats(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "stats", count=await count_users()))


@router.message(F.text.in_({v["btn_broadcast"] for v in TEXTS.values()}))
async def btn_broadcast_ask(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "broadcast_ask"))
    await state.set_state(AdminStates.waiting_broadcast)


@router.message(AdminStates.waiting_broadcast)
async def do_broadcast(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    count = 0
    for uid in await get_all_user_ids():
        try:
            await bot.copy_message(uid, message.chat.id, message.message_id)
            count += 1
        except Exception:
            pass
    await message.answer(t(lang, "broadcast_done", count=count), reply_markup=admin_reply_kb(lang))


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await message.answer(t(lang, "admin_only"))
        return
    await message.answer(t(lang, "stats", count=await count_users()))


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await message.answer(t(lang, "admin_only"))
        return
    await message.answer(t(lang, "admin_panel"), reply_markup=admin_reply_kb(lang))


# ============================================================
# HANDLERS: admin - mandatory subscriptions
# ============================================================
@router.message(F.text.in_({v["btn_add_channel"] for v in TEXTS.values()}))
async def btn_add_channel_ask(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "ask_channel"))
    await state.set_state(AdminStates.waiting_channel)


async def _resolve_chat(message: Message):
    if message.forward_from_chat:
        return message.forward_from_chat
    text = (message.text or "").strip()
    if not text:
        return None
    try:
        if text.lstrip("-").isdigit():
            return await bot.get_chat(int(text))
        return await bot.get_chat(text if text.startswith("@") else f"@{text}")
    except Exception:
        return None


@router.message(AdminStates.waiting_channel)
async def do_add_channel(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()

    chat = await _resolve_chat(message)
    if not chat:
        await message.answer(t(lang, "channel_add_fail"), reply_markup=admin_reply_kb(lang))
        return

    try:
        member = await bot.get_chat_member(chat.id, bot.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            raise PermissionError
    except Exception:
        await message.answer(t(lang, "channel_add_fail_not_admin"), reply_markup=admin_reply_kb(lang))
        return

    is_private = chat.username is None
    invite_link = None
    if is_private:
        try:
            link_obj = await bot.create_chat_invite_link(
                chat.id, name="Majburiy obuna", creates_join_request=True
            )
            invite_link = link_obj.invite_link
        except Exception as e:
            log.warning("could not create invite link: %s", e)
            invite_link = None
    else:
        invite_link = f"https://t.me/{chat.username}"

    await add_channel(chat.id, chat.title or chat.username or str(chat.id), chat.username, is_private, invite_link)
    await message.answer(
        t(lang, "channel_added", title=chat.title or chat.username or str(chat.id)),
        reply_markup=admin_reply_kb(lang),
    )


async def _build_channel_rows(lang: str, channels) -> list:
    rows = []
    for c in channels:
        kind = t(lang, "channel_type_private") if c["is_private"] else t(lang, "channel_type_public")
        count = await count_channel_subscribers(c["chat_id"])
        subs_label = t(lang, "channel_subs_label", count=count)
        rows.append(
            [InlineKeyboardButton(
                text=f"❌ {c['title']} ({kind}, {subs_label})",
                callback_data=f"delchan:{c['id']}",
            )]
        )
    return rows


@router.message(F.text.in_({v["btn_list_channels"] for v in TEXTS.values()}))
async def btn_list_channels(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    channels = await list_channels()
    if not channels:
        await message.answer(t(lang, "channel_list_empty"))
        return
    rows = await _build_channel_rows(lang, channels)
    await message.answer(t(lang, "channel_list_title"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("delchan:"))
async def cb_delete_channel(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer()
        return
    lang = await get_user_lang(call.from_user.id)
    channel_id = int(call.data.split(":", 1)[1])
    await remove_channel(channel_id)
    channels = await list_channels()
    if not channels:
        await call.message.edit_text(t(lang, "channel_list_empty"))
    else:
        rows = await _build_channel_rows(lang, channels)
        await call.message.edit_text(t(lang, "channel_list_title"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer(t(lang, "channel_removed"))


# ============================================================
# HANDLER: join requests & chat_member updates
# ============================================================
@router.chat_join_request()
async def on_join_request(update: ChatJoinRequest):
    await log_join_request(update.chat.id, update.from_user.id)


@router.chat_member()
async def on_chat_member_update(update: ChatMemberUpdated):
    channel = await get_channel_by_chat_id(update.chat.id)
    if not channel:
        return

    joined_statuses = (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    is_member_now = update.new_chat_member.status in joined_statuses
    was_member_before = update.old_chat_member.status in joined_statuses
    user_id = update.new_chat_member.user.id

    if is_member_now and not was_member_before:
        await mark_channel_subscriber(update.chat.id, user_id)
    elif not is_member_now and was_member_before:
        await unmark_channel_subscriber(update.chat.id, user_id)


# ============================================================
# MANDATORY SUBSCRIPTION CHECK
# ============================================================
async def get_unsubscribed_channels(user_id: int):
    channels = await list_channels()
    missing = []
    for c in channels:
        subscribed = False
        try:
            member = await bot.get_chat_member(c["chat_id"], user_id)
            if member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ):
                subscribed = True
        except Exception:
            subscribed = False
        if not subscribed:
            missing.append(c)
    return missing


def subscribe_kb(lang: str, channels) -> InlineKeyboardMarkup:
    rows = []
    for c in channels:
        url = c["invite_link"] or (f"https://t.me/{c['username']}" if c["username"] else None)
        if url:
            rows.append([InlineKeyboardButton(text=f"➕ {c['title']}", url=url)])
    rows.append([InlineKeyboardButton(text=t(lang, "check_sub_btn"), callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "checksub")
async def cb_check_sub(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    missing = await get_unsubscribed_channels(call.from_user.id)
    if missing:
        await call.answer(t(lang, "still_not_subscribed"), show_alert=True)
        return
    await call.message.edit_text(t(lang, "now_subscribed"))
    await call.answer()


# ============================================================
# DOWNLOAD HELPERS
# ============================================================
def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


PLAYER_CLIENT_FALLBACKS = [
    ["android_testsuite"],
    ["tv_embedded"],
    ["web"],
    ["ios"],
    ["android_creator"],
    ["mweb"],
]

_POT_CACHE: dict = {}
_POT_TTL = 21600
_POT_LOCK: asyncio.Lock | None = None


def _get_pot_lock() -> asyncio.Lock:
    global _POT_LOCK
    if _POT_LOCK is None:
        _POT_LOCK = asyncio.Lock()
    return _POT_LOCK


_BGUTILS_URL = "https://bgutils.kz/token"


async def _fetch_po_token() -> tuple[str, str] | tuple[None, None]:
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(_BGUTILS_URL) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json(content_type=None)
                vd = data.get("visitorData") or data.get("visitor_data")
                pot = data.get("poToken") or data.get("po_token")
                if vd and pot:
                    return vd, pot
                return None, None
    except Exception:
        return None, None


async def get_po_token() -> tuple[str, str] | tuple[None, None]:
    async with _get_pot_lock():
        now = time.time()
        if _POT_CACHE and now - _POT_CACHE.get("ts", 0) < _POT_TTL:
            return _POT_CACHE["visitor_data"], _POT_CACHE["po_token"]
        vd, pot = await _fetch_po_token()
        if vd and pot:
            _POT_CACHE.update({"visitor_data": vd, "po_token": pot, "ts": now})
            return vd, pot
        return None, None


def get_po_token_sync() -> tuple[str, str] | tuple[None, None]:
    if _POT_CACHE and time.time() - _POT_CACHE.get("ts", 0) < _POT_TTL:
        return _POT_CACHE["visitor_data"], _POT_CACHE["po_token"]
    return None, None


def _is_bot_check_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        phrase in msg for phrase in (
            "sign in to confirm",
            "not a bot",
            "cookies",
            "http error 429",
            "too many requests",
            "preconditionfailed",
            "vpn or proxy",
            "error code: 152",
            "this video is unavailable",
            "video unavailable",
        )
    )


_cookie_hint_logged = False


def _raise_ytdlp_failure(last_exc: Exception | None):
    global _cookie_hint_logged
    if last_exc is not None and _is_bot_check_error(last_exc) and not _cookie_hint_logged:
        _cookie_hint_logged = True
    if last_exc is None:
        raise RuntimeError("yt-dlp error")
    raise last_exc


def _build_ydl_opts_base(outdir: str | None, player_clients: list) -> dict:
    _no_sig_clients = {"android_testsuite", "android_creator", "tv_embedded"}
    needs_player = not all(c in _no_sig_clients for c in player_clients)

    _web_clients = {"web", "mweb", "web_creator", "web_embedded"}
    needs_pot = any(c in _web_clients for c in player_clients)

    extractor_args: dict = {
        "player_client": player_clients,
        "skip_webpage": ["1"],
    }
    if not needs_player:
        extractor_args["player_skip"] = ["webpage", "configs", "js"]

    if needs_pot:
        visitor_data, po_token = get_po_token_sync()
        if visitor_data and po_token:
            extractor_args["visitor_data"] = [visitor_data]
            extractor_args["po_token"] = [po_token]

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {"youtube": extractor_args},
        "http_headers": {"User-Agent": DEFAULT_UA},
        "geo_bypass": True,
        "retries": 3,
        "sleep_interval": 1,
        "max_sleep_interval": 3,
        "socket_timeout": 30,
    }
    if outdir:
        opts["outtmpl"] = os.path.join(outdir, "%(id)s.%(ext)s")
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _run_ytdlp_download(url: str, outdir: str, use_proxy: bool):
    last_exc = None
    for attempt, player_clients in enumerate(PLAYER_CLIENT_FALLBACKS):
        ydl_opts = _build_ydl_opts_base(outdir, player_clients)
        ydl_opts["format"] = "best[ext=mp4]/best"
        if use_proxy and GENERAL_PROXY:
            ydl_opts["proxy"] = GENERAL_PROXY
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info:
                    info = info["entries"][0]
                filename = ydl.prepare_filename(info)
                return filename, info
        except Exception as e:
            last_exc = e
            if "youtube" not in url and "youtu.be" not in url:
                raise
            if not _is_bot_check_error(e):
                raise
            continue
    _raise_ytdlp_failure(last_exc)


async def download_media(url: str, outdir: str, platform: str):
    loop = asyncio.get_running_loop()
    use_proxy = platform != "tiktok"
    await get_po_token()
    return await loop.run_in_executor(None, _run_ytdlp_download, url, outdir, use_proxy)


def _run_ytdlp_audio_search_download(query: str, outdir: str) -> tuple[str, str]:
    last_exc = None
    for attempt, player_clients in enumerate(PLAYER_CLIENT_FALLBACKS):
        ydl_opts = _build_ydl_opts_base(outdir, player_clients)
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch1:{query}", download=True)
                if not info:
                    raise RuntimeError("No info")
                if "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    if not entries:
                        raise RuntimeError("Empty entries")
                    info = entries[0]
                filename = ydl.prepare_filename(info)
                video_id = info.get("id")
                video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else info.get("webpage_url", "")
                return os.path.splitext(filename)[0] + ".mp3", video_url
        except Exception as e:
            last_exc = e
            if not _is_bot_check_error(e):
                raise
            continue
    _raise_ytdlp_failure(last_exc)


async def search_and_download_song(query: str, outdir: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    await get_po_token()
    return await loop.run_in_executor(None, _run_ytdlp_audio_search_download, query, outdir)


def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def format_count(n) -> str:
    if not n:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1000:.0f}k"
    return str(n)


def _run_ytdlp_text_search(query: str, limit: int) -> list[dict]:
    last_exc = None
    for player_clients in PLAYER_CLIENT_FALLBACKS:
        ydl_opts = _build_ydl_opts_base(None, player_clients)
        ydl_opts["extract_flat"] = "in_playlist"
        ydl_opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                entries = [e for e in (info.get("entries") or []) if e]
                results = []
                for e in entries:
                    vid = e.get("id")
                    results.append(
                        {
                            "id": vid,
                            "title": e.get("title") or "Unknown",
                            "uploader": e.get("uploader") or e.get("channel") or "",
                            "duration": e.get("duration"),
                            "view_count": e.get("view_count"),
                            "url": f"https://www.youtube.com/watch?v={vid}" if vid else e.get("url"),
                        }
                    )
                return results
        except Exception as e:
            last_exc = e
            if not _is_bot_check_error(e):
                raise
            continue
    _raise_ytdlp_failure(last_exc)


async def text_search_youtube(query: str, limit: int = SEARCH_FETCH_LIMIT) -> list[dict]:
    loop = asyncio.get_running_loop()
    await get_po_token()
    return await loop.run_in_executor(None, _run_ytdlp_text_search, query, limit)


def _run_ytdlp_download_audio_url(url: str, outdir: str) -> str:
    last_exc = None
    for player_clients in PLAYER_CLIENT_FALLBACKS:
        ydl_opts = _build_ydl_opts_base(outdir, player_clients)
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                return os.path.splitext(filename)[0] + ".mp3"
        except Exception as e:
            last_exc = e
            if not _is_bot_check_error(e):
                raise
            continue
    _raise_ytdlp_failure(last_exc)


async def download_song_by_url(url: str, outdir: str) -> str:
    loop = asyncio.get_running_loop()
    await get_po_token()
    return await loop.run_in_executor(None, _run_ytdlp_download_audio_url, url, outdir)


def render_search_page(lang: str, token: str):
    data = SEARCH_CACHE[token]
    results = data["results"]
    page = data["page"]
    start = page * SEARCH_RESULTS_PER_PAGE
    end = min(start + SEARCH_RESULTS_PER_PAGE, len(results))
    page_items = results[start:end]

    lines = [
        f"🔍 {data['query']}",
        t(lang, "search_results_range", start=start + 1, end=end, total=len(results)),
        "",
    ]
    for i, item in enumerate(page_items, start=1):
        dur = format_duration(item.get("duration"))
        views = format_count(item.get("view_count"))
        lines.append(f"{i}. {item['title']} — {dur} · 👁 {views}")
    text = "\n".join(lines)

    number_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(1, len(page_items) + 1):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"srch:{token}:{i}"))
        if len(row) == 4:
            number_rows.append(row)
            row = []
    if row:
        number_rows.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"srch:{token}:prev"))
    nav_row.append(InlineKeyboardButton(text="❌", callback_data=f"srch:{token}:cancel"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"srch:{token}:next"))

    return text, InlineKeyboardMarkup(inline_keyboard=number_rows + [nav_row])


async def _expire_search_cache(token: str, delay: int):
    await asyncio.sleep(delay)
    SEARCH_CACHE.pop(token, None)


def extract_audio_for_recognition(video_path: str, outdir: str) -> str | None:
    audio_path = os.path.join(outdir, "sample.mp3")
    result = subprocess.run(
        [FFMPEG_PATH, "-y", "-i", video_path, "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k", audio_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_text = result.stderr.decode(errors="ignore")
    if result.returncode != 0 or not os.path.exists(audio_path):
        if "does not contain any stream" in stderr_text or "Output file does not contain any stream" in stderr_text:
            return None
        raise RuntimeError(f"ffmpeg failed: {stderr_text[-500:]}")
    return audio_path


async def recognize_song(audio_path: str) -> dict | None:
    if Shazam is None:
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None
    try:
        shazam = Shazam()
        result = await shazam.recognize(audio_path)
    except Exception:
        return None
    track = result.get("track")
    if not track:
        return None
    return {"title": track.get("title", "Unknown"), "artist": track.get("subtitle", "Unknown")}


# ============================================================
# HANDLERS: media link & search
# ============================================================
@router.message(F.text.regexp(URL_RE.pattern))
async def handle_link(message: Message):
    lang = await get_user_lang(message.from_user.id)

    missing = await get_unsubscribed_channels(message.from_user.id)
    if missing:
        await message.answer(t(lang, "subscribe_required"), reply_markup=subscribe_kb(lang, missing))
        return

    match = URL_RE.search(message.text)
    if not match:
        await message.answer(t(lang, "no_link"))
        return
    url = match.group(1)

    platform = detect_platform(url)
    if not platform:
        await message.answer(t(lang, "unsupported_link"))
        return

    status = await message.answer(t(lang, "downloading"))
    outdir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    try:
        filepath, info = await download_media(url, outdir, platform)
    except Exception as e:
        shutil.rmtree(outdir, ignore_errors=True)
        if platform == "tiktok":
            await status.edit_text(t(lang, "tiktok_unavailable"))
        else:
            log.warning("download failed: %s", e)
            await status.edit_text(t(lang, "error"))
        return

    try:
        await status.delete()
    except Exception:
        pass

    token = uuid.uuid4().hex[:12]
    ext = os.path.splitext(filepath)[1].lower()
    
    me = await bot.get_me()
    bot_username = me.username or "bot"
    caption = t(lang, "caption", bot_username=bot_username)
    keyboard = music_inline_kb(lang, token)

    try:
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            await message.answer_photo(FSInputFile(filepath), caption=caption, reply_markup=keyboard)
        else:
            await message.answer_video(FSInputFile(filepath), caption=caption, reply_markup=keyboard)
        FILE_CACHE[token] = filepath
        asyncio.create_task(_expire_cache(token, outdir, delay=CACHE_TTL_SECONDS))
    except Exception as e:
        log.warning("send failed: %s", e)
        await message.answer(t(lang, "error"))
        shutil.rmtree(outdir, ignore_errors=True)


async def _expire_cache(token: str, outdir: str, delay: int):
    await asyncio.sleep(delay)
    FILE_CACHE.pop(token, None)
    shutil.rmtree(outdir, ignore_errors=True)


@router.message(F.text & ~F.text.regexp(URL_RE.pattern) & ~F.text.startswith("/"))
async def handle_text_search(message: Message):
    lang = await get_user_lang(message.from_user.id)

    missing = await get_unsubscribed_channels(message.from_user.id)
    if missing:
        await message.answer(t(lang, "subscribe_required"), reply_markup=subscribe_kb(lang, missing))
        return

    query = message.text.strip()
    if not query:
        return

    status = await message.answer(t(lang, "searching"))
    try:
        results = await text_search_youtube(query)
    except Exception as e:
        log.warning("text search failed: %s", e)
        await status.edit_text(t(lang, "error"))
        return

    if not results:
        await status.edit_text(t(lang, "search_no_results"))
        return

    token = uuid.uuid4().hex[:12]
    SEARCH_CACHE[token] = {"query": query, "results": results, "page": 0}
    asyncio.create_task(_expire_search_cache(token, delay=SEARCH_CACHE_TTL_SECONDS))

    text, kb = render_search_page(lang, token)
    await status.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("srch:"))
async def cb_search_action(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    _, token, action = call.data.split(":", 2)
    data = SEARCH_CACHE.get(token)
    if not data:
        await call.answer(t(lang, "file_expired"), show_alert=True)
        return

    if action == "cancel":
        SEARCH_CACHE.pop(token, None)
        try:
            await call.message.delete()
        except Exception:
            pass
        await call.answer()
        return

    if action == "prev":
        if data["page"] > 0:
            data["page"] -= 1
        text, kb = render_search_page(lang, token)
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    if action == "next":
        max_page = (len(data["results"]) - 1) // SEARCH_RESULTS_PER_PAGE
        if data["page"] < max_page:
            data["page"] += 1
        text, kb = render_search_page(lang, token)
        await call.message.edit_text(text, reply_markup=kb)
        await call.answer()
        return

    if not action.isdigit():
        await call.answer()
        return
    start = data["page"] * SEARCH_RESULTS_PER_PAGE
    real_idx = start + int(action) - 1
    if real_idx >= len(data["results"]):
        await call.answer()
        return
    entry = data["results"][real_idx]

    await call.answer()
    status = await call.message.answer(t(lang, "downloading"))
    work_dir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    try:
        mp3_path = await download_song_by_url(entry["url"], work_dir)
        title = entry.get("title") or "Unknown"
        performer = entry.get("uploader") or ""
        await call.message.answer_audio(
            FSInputFile(mp3_path),
            title=title,
            performer=performer,
            caption=t(lang, "song_caption", title=title, artist=performer),
            reply_markup=song_result_kb(lang, title, performer, entry.get("url")),
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.warning("song download (text search) failed: %s", e)
        try:
            await status.edit_text(t(lang, "error"))
        except Exception:
            pass
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ============================================================
# HANDLER: music recognition button
# ============================================================
@router.callback_query(F.data.startswith("music:"))
async def cb_recognize_music(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    token = call.data.split(":", 1)[1]
    video_path = FILE_CACHE.get(token)
    if not video_path or not os.path.exists(video_path):
        await call.answer(t(lang, "file_expired"), show_alert=True)
        return

    await call.answer()
    status = await call.message.answer(t(lang, "recognizing"))
    work_dir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    video_outdir = os.path.dirname(video_path)
    try:
        loop = asyncio.get_running_loop()
        audio_sample = await loop.run_in_executor(
            None, extract_audio_for_recognition, video_path, work_dir
        )
        if not audio_sample:
            await status.edit_text(t(lang, "not_recognized"))
            return
        song = await recognize_song(audio_sample)
        if not song:
            await status.edit_text(t(lang, "not_recognized"))
            return

        await status.edit_text(t(lang, "found_song", title=song["title"], artist=song["artist"]))
        query = f"{song['artist']} {song['title']}"
        mp3_path, youtube_url = await search_and_download_song(query, work_dir)
        await call.message.answer_audio(
            FSInputFile(mp3_path),
            title=song["title"],
            performer=song["artist"],
            caption=t(lang, "song_caption", title=song["title"], artist=song["artist"]),
            reply_markup=song_result_kb(lang, song["title"], song["artist"], youtube_url),
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.warning("music recognition failed: %s", e)
        try:
            await status.edit_text(t(lang, "error"))
        except Exception:
            await call.message.answer(t(lang, "error"))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(video_outdir, ignore_errors=True)
        FILE_CACHE.pop(token, None)


# ============================================================
# ENTRYPOINT
# ============================================================
async def main():
    global BOT_DISPLAY_NAME

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set!")

    await init_db()

    me = await bot.get_me()
    BOT_DISPLAY_NAME = me.first_name or me.username or "Bot"
    log.info("Bot started as @%s (%s)", me.username, BOT_DISPLAY_NAME)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
