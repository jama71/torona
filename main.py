import asyncio
import base64
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile

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
# ffmpeg: bundled via imageio-ffmpeg so it works on ANY host
# (Railway/Docker/etc.) without relying on apt packages being
# installed by the build system. This fixes:
#   "No such file or directory: 'ffmpeg'"
#
# IMPORTANT: imageio-ffmpeg's binary is NOT named "ffmpeg" (e.g.
# "ffmpeg-linux64-v4.2.2"), but shazamio/pydub always shell out to the
# literal command name "ffmpeg". Just adding its folder to PATH is not
# enough - we create a symlink (or copy) literally called "ffmpeg" in a
# directory we control and put THAT directory on PATH.
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
    # some libraries (pydub, etc.) look at this instead of PATH
    from pydub import AudioSegment

    AudioSegment.converter = FFMPEG_PATH
except Exception:
    pass

# ============================================================
# CONFIG  (only these need to be set in Railway env variables)
# ============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
# Optional. Never used for TikTok on purpose (see requirements).
GENERAL_PROXY = os.getenv("PROXY_URL", "").strip() or None

# Song search (text search + Shazam recognition) uses SoundCloud first, then
# VK Music as a fallback for tracks that are copyright-blocked or missing on
# SoundCloud. YouTube is NOT used for song search anymore - only for
# downloading video when the user pastes an actual YouTube link.
SOUNDCLOUD_COOKIES_FILE = os.getenv("SOUNDCLOUD_COOKIES_FILE", "").strip() or None
VK_LOGIN = os.getenv("VK_LOGIN", "").strip()
VK_PASSWORD = os.getenv("VK_PASSWORD", "").strip()


def _repair_cookie_line(line: str) -> str:
    """Netscape cookie lines are tab-separated (7 fields). Some copy-paste
    paths (chat UIs, some env-var editors) can collapse tabs into spaces -
    if that happened but the 7 fields are still intact, rejoin them with
    real tabs so yt-dlp's cookiejar parser accepts the line."""
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


# Fallback YouTube cookies baked directly into the code, so the bot works
# out of the box without requiring any env var setup. If YOUTUBE_COOKIES /
# YOUTUBE_COOKIES_B64 / COOKIES_FILE are set in the environment, those take
# priority over this default. When these cookies eventually expire, either
# set one of those env vars with a fresh export, or just replace the string
# below with a newly exported cookies.txt content.
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
        logger.warning(
            "%s does not look like a valid cookies.txt (found %d/%d usable cookie lines) - "
            "skipping it and trying the next available source.",
            source, valid, total,
        )
        return None
    if valid < total:
        logger.warning(
            "%s: only %d/%d cookie lines look valid - the value may be truncated, using it anyway.",
            source, valid, total,
        )
    path = _write_cookies_file(content)
    logger.info("YouTube cookies loaded from %s (%d cookie lines).", source, valid)
    return path


def _setup_youtube_cookies() -> str | None:
    """
    YouTube increasingly blocks cloud/datacenter IPs (Railway included) with
    "Sign in to confirm you're not a bot" REGARDLESS of which player_client
    yt-dlp uses. The only reliable fix is real browser cookies.

    Tries each source in order and FALLS THROUGH to the next one if a
    source is set but turns out invalid/empty/truncated - a broken or
    stale env var (e.g. left over from earlier troubleshooting) must never
    block the built-in default from being used:
      1. YOUTUBE_COOKIES_B64     -> base64-encoded cookies.txt content (env var)
      2. YOUTUBE_COOKIES         -> raw cookies.txt content (env var, Netscape format)
      3. COOKIES_FILE            -> path to an already-mounted cookies.txt file (env var)
      4. DEFAULT_YOUTUBE_COOKIES -> baked into the code above, used if nothing
         else worked, so the bot works without any extra setup.
    """
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
        except Exception as e:
            logger.warning(
                "YOUTUBE_COOKIES_B64 could not be decoded (%s) - trying the next available source.",
                e,
            )

    if raw:
        result = _try_load_cookie_candidate("YOUTUBE_COOKIES", raw, logger)
        if result:
            return result

    if path_env and os.path.exists(path_env):
        logger.info("YouTube cookies loaded from COOKIES_FILE (%s).", path_env)
        return path_env

    result = _try_load_cookie_candidate(
        "the built-in default (edit DEFAULT_YOUTUBE_COOKIES in main.py to update)",
        DEFAULT_YOUTUBE_COOKIES,
        logger,
    )
    if result:
        return result

    logger.warning("No usable YouTube cookies found anywhere - relying on player_client fallback only.")
    return None


# Path to a cookies.txt (Netscape format) that helps yt-dlp bypass YouTube's
# "Sign in to confirm you're not a bot" checks. See _setup_youtube_cookies().
COOKIES_FILE = _setup_youtube_cookies()


def _check_cookies_expiry(path):
    """Startup da cookie muddatini tekshiradi va ogohlantiradi."""
    if not path or not os.path.exists(path):
        return
    now = int(time.time())
    expired = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.strip().split("\t")
                if len(parts) >= 5:
                    try:
                        exp = int(parts[4])
                        if 0 < exp < now:
                            expired += 1
                    except ValueError:
                        pass
    except Exception:
        return
    if expired:
        log.warning(
            "⚠️  %d YouTube cookie(s) MUDDATI O\'TGAN! "
            "Bot 'Sign in to confirm' xatosini beradi. "
            "Yangi cookies eksport qilib YOUTUBE_COOKIES_B64 ga qo\'ying.",
            expired,
        )
    else:
        log.info("✅ YouTube cookies amal qilmoqda.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bot")

_check_cookies_expiry(COOKIES_FILE)

# A realistic desktop User-Agent + an Android/iOS "player_client" combo is
# currently the most reliable way to dodge YouTube's bot-check without cookies.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DOWNLOAD_ROOT = tempfile.gettempdir()
CACHE_TTL_SECONDS = 300  # free-tier disk friendly: auto-clean unused files after 5 min

URL_RE = re.compile(r"(https?://\S+)")
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com"),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "tiktok": re.compile(r"tiktok\.com"),
    "pinterest": re.compile(r"(pinterest\.com|pin\.it)"),
    "snapchat": re.compile(r"snapchat\.com"),
}

# token -> {"filepath": str, "source_url": str}, used for the "detect music" button
FILE_CACHE: dict[str, dict] = {}

# token -> {"query": str, "results": [...], "page": int}, used for the
# text-based music search feature (search -> pick from list -> mp3)
SEARCH_CACHE: dict[str, dict] = {}
SEARCH_RESULTS_PER_PAGE = 8
SEARCH_FETCH_LIMIT = 40  # fetched once per query, paginated locally
SEARCH_CACHE_TTL_SECONDS = 600

# token -> artist name, used for the "🔍 search by artist" button shown
# after a song is recognized (callback_data has a 64-byte limit, so the
# artist name itself can't always go directly in the callback data)
ARTIST_SEARCH_CACHE: dict[str, str] = {}

# bot's own display name, auto-detected from the token at startup
BOT_DISPLAY_NAME = "Bot"
BOT_USERNAME = ""

pool: asyncpg.Pool | None = None


# ============================================================
# TRANSLATIONS
# ============================================================
TEXTS = {
    "uz": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": (
            "Assalomu alaykum! 👋\n\n<b>{bot_name}</b> ga xush kelibsiz!\n\n"
            "Menga Instagram, YouTube, TikTok, Pinterest yoki Snapchat havolasini "
            "yuboring — men videoni/mediani yuklab beraman. Video ostidagi tugma orqali "
            "esa undagi musiqani aniqlab, MP3 shaklda yuborib bera olaman 🎵"
        ),
        "send_link": "🔗 Havolani yuboring (Instagram / YouTube / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Yuklanmoqda, biroz kuting...",
        "caption": "✅ Botimizdan foydalanganingiz uchun rahmat!",
        "detect_music_btn": "🎵 Musiqani aniqlash",
        "recognizing": "🎧 Musiqa aniqlanmoqda...",
        "not_recognized": "😔 Kechirasiz, bu videodagi musiqani aniqlab bo'lmadi.",
        "found_song": "🎶 Topildi: {title} — {artist}\n⏳ Yuklab olinmoqda...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Rassom bo'yicha qidirish",
        "btn_youtube_link": "🔍 SoundCloud'da ochish",
        "lyrics_notice": "📜 Qo'shiq matnini mualliflik huquqi tufayli to'liq ko'rsata olmayman, lekin quyidagi havoladan uni topishingiz mumkin:",
        "tiktok_unavailable": "⚠️ Kechirasiz, hozircha TikTok xizmatlari ishlamayapti. Birozdan so'ng qayta urinib ko'ring.",
        "unsupported_link": "❌ Bu havola qo'llab-quvvatlanmaydi. Instagram, YouTube, TikTok, Pinterest yoki Snapchat havolasini yuboring.",
        "error": "❌ Xatolik yuz berdi, qaytadan urinib ko'ring.",
        "no_link": "❗️ Iltimos, media havolasini yuboring.",
        "admin_only": "⛔ Bu buyruq faqat administratorlar uchun.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Statistika:\n\n👤 Jami foydalanuvchilar: {count}",
        "broadcast_ask": "✍️ Yuboriladigan xabar matnini yuboring:",
        "broadcast_done": "✅ Xabar {count} ta foydalanuvchiga yuborildi.",
        "file_expired": "⏱ Vaqt tugadi, iltimos havolani qayta yuboring.",
        "lang_set": "✅ Til o'zbekcha etib o'rnatildi.",
        "searching": "🔍 Qidirilmoqda...",
        "search_no_results": "😔 Hech narsa topilmadi. Boshqa nom bilan urinib ko'ring.",
        "search_results_range": "Natijalar {start}-{end} / {total}",
        "help": (
            "ℹ️ <b>Yordam</b>\n\n"
            "1) Instagram, YouTube, TikTok, Pinterest yoki Snapchat havolasini yuboring.\n"
            "2) Bot mediani yuklab beradi.\n"
            "3) Video ostidagi 🎵 tugmasini bosing — bot videodagi musiqani aniqlab, "
            "MP3 shaklida yuboradi.\n"
            "4) Yoki shunchaki qo'shiq/ijrochi nomini yozib yuboring — bot YouTube'dan "
            "qidirib, ro'yxatdan tanlaganingizni MP3 shaklida yuboradi.\n\n"
            "Tilni o'zgartirish uchun pastdagi \"🌐 Til\" tugmasidan foydalaning."
        ),
        "btn_lang": "🌐 Til",
        "btn_help": "❓ Yordam",
        "btn_admin": "🛠 Admin panel",
        "btn_back": "⬅️ Orqaga",
        "btn_stats": "📊 Statistika",
        "btn_broadcast": "📢 Xabar yuborish",
        "btn_add_channel": "➕ Majburiy obuna qo'shish",
        "btn_list_channels": "📋 Majburiy obunalar",
        "btn_clear_cache": "🗑 Cache tozalash",
        "btn_db_export": "📤 DB export",
        "btn_db_import": "📥 DB import",
        "cache_cleared": "✅ Vaqtinchalik cache tozalandi ({count} ta yozuv).",
        "db_export_empty": "📭 Bazada foydalanuvchilar topilmadi.",
        "db_export_caption": "📦 DB export — {count} ta foydalanuvchi (.vk fayllar zip ichida).",
        "db_export_fail": "❌ DB export qilishda xatolik yuz berdi.",
        "db_import_ask": "📥 Import qilish uchun avval yuborilgan .zip yoki .vk fayl(lar)ni yuboring.",
        "db_import_done": "✅ Import tugadi: {added} ta qo'shildi, {updated} ta yangilandi, {failed} ta xato.",
        "db_import_fail": "❌ Faylni o'qib bo'lmadi. To'g'ri .zip yoki .vk fayl yuboring.",
        "db_import_no_file": "❗️ Iltimos, .zip yoki .vk fayl yuboring.",
        "ask_channel": (
            "📎 Kanal/gurux qo'shish uchun:\n\n"
            "1) Botni o'sha kanal/guruhga <b>administrator</b> qilib qo'ying.\n"
            "2) Shu yerga o'sha kanal/guruhdagi istalgan xabarni forward qiling, "
            "yoki uning @username'ini, yoki chat_id sini yuboring."
        ),
        "channel_added": "✅ \"{title}\" majburiy obunalar ro'yxatiga qo'shildi.",
        "channel_add_fail_not_admin": "❌ Botni avval o'sha kanal/guruhga administrator qiling, keyin qaytadan urinib ko'ring.",
        "channel_add_fail": "❌ Kanal/guruhni aniqlab bo'lmadi. Forward yoki @username/ID yuboring.",
        "channel_list_empty": "📭 Hozircha majburiy obunalar yo'q.",
        "channel_list_title": "📋 Majburiy obunalar ro'yxati:",
        "channel_removed": "🗑 Kanal ro'yxatdan olib tashlandi.",
        "subscribe_required": "⚠️ Botdan foydalanish uchun quyidagi kanal(lar)ga a'zo bo'ling:",
        "check_sub_btn": "✅ Tekshirish",
        "still_not_subscribed": "❌ Siz hali barcha kanallarga a'zo bo'lmadingiz.",
        "now_subscribed": "✅ Rahmat! Endi botdan foydalanishingiz mumkin, havolani yuboring.",
        "channel_type_public": "ochiq kanal/gurux",
        "channel_subs_label": "{count} a'zo",
        "channel_type_private": "yopiq kanal/gurux",
    },
    "ru": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": (
            "Привет! 👋\n\nДобро пожаловать в <b>{bot_name}</b>!\n\n"
            "Отправьте мне ссылку с Instagram, YouTube, TikTok, Pinterest или Snapchat — "
            "я скачаю видео. А кнопкой под видео можно распознать музыку и получить её в MP3 🎵"
        ),
        "send_link": "🔗 Отправьте ссылку (Instagram / YouTube / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Загружается, подождите...",
        "caption": "✅ Спасибо, что пользуетесь ботом!",
        "detect_music_btn": "🎵 Распознать музыку",
        "recognizing": "🎧 Распознаём музыку...",
        "not_recognized": "😔 Не удалось распознать музыку в этом видео.",
        "found_song": "🎶 Найдено: {title} — {artist}\n⏳ Загружается...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Текст песни",
        "btn_artist_search": "🔍 Поиск по исполнителю",
        "btn_youtube_link": "🔍 Открыть в SoundCloud",
        "lyrics_notice": "📜 Не могу показать полный текст песни из-за авторских прав, но вы можете найти его по ссылке ниже:",
        "tiktok_unavailable": "⚠️ Извините, сервисы TikTok сейчас не работают. Попробуйте позже.",
        "unsupported_link": "❌ Эта ссылка не поддерживается. Отправьте ссылку с Instagram, YouTube, TikTok, Pinterest или Snapchat.",
        "error": "❌ Произошла ошибка, попробуйте ещё раз.",
        "no_link": "❗️ Пожалуйста, отправьте ссылку на медиа.",
        "admin_only": "⛔ Эта команда только для администраторов.",
        "admin_panel": "🛠 Админ-панель",
        "stats": "📊 Статистика:\n\n👤 Всего пользователей: {count}",
        "broadcast_ask": "✍️ Отправьте текст рассылки:",
        "broadcast_done": "✅ Сообщение отправлено {count} пользователям.",
        "file_expired": "⏱ Время истекло, отправьте ссылку заново.",
        "lang_set": "✅ Язык установлен: русский.",
        "searching": "🔍 Ищем...",
        "search_no_results": "😔 Ничего не найдено. Попробуйте другой запрос.",
        "search_results_range": "Результаты {start}-{end} / {total}",
        "help": (
            "ℹ️ <b>Помощь</b>\n\n"
            "1) Отправьте ссылку с Instagram, YouTube, TikTok, Pinterest или Snapchat.\n"
            "2) Бот скачает медиа.\n"
            "3) Нажмите кнопку 🎵 под видео — бот распознает музыку и пришлёт её в MP3.\n"
            "4) Или просто напишите название песни/исполнителя — бот найдёт на YouTube "
            "и пришлёт выбранный трек в MP3.\n\n"
            "Чтобы сменить язык, используйте кнопку \"🌐 Язык\" внизу."
        ),
        "btn_lang": "🌐 Язык",
        "btn_help": "❓ Помощь",
        "btn_admin": "🛠 Админ-панель",
        "btn_back": "⬅️ Назад",
        "btn_stats": "📊 Статистика",
        "btn_broadcast": "📢 Рассылка",
        "btn_add_channel": "➕ Добавить обяз. подписку",
        "btn_list_channels": "📋 Список подписок",
        "btn_clear_cache": "🗑 Очистить кэш",
        "btn_db_export": "📤 Экспорт БД",
        "btn_db_import": "📥 Импорт БД",
        "cache_cleared": "✅ Временный кэш очищен ({count} записей).",
        "db_export_empty": "📭 Пользователи в базе не найдены.",
        "db_export_caption": "📦 Экспорт БД — {count} пользователей (.vk файлы в zip).",
        "db_export_fail": "❌ Ошибка при экспорте БД.",
        "db_import_ask": "📥 Для импорта отправьте ранее выгруженный .zip или .vk файл(ы).",
        "db_import_done": "✅ Импорт завершён: добавлено {added}, обновлено {updated}, ошибок {failed}.",
        "db_import_fail": "❌ Не удалось прочитать файл. Отправьте корректный .zip или .vk файл.",
        "db_import_no_file": "❗️ Пожалуйста, отправьте .zip или .vk файл.",
        "ask_channel": (
            "📎 Чтобы добавить канал/группу:\n\n"
            "1) Сделайте бота <b>администратором</b> в этом канале/группе.\n"
            "2) Перешлите сюда любое сообщение из него, либо отправьте его @username "
            "или chat_id."
        ),
        "channel_added": "✅ \"{title}\" добавлен в обязательные подписки.",
        "channel_add_fail_not_admin": "❌ Сначала сделайте бота администратором канала/группы, затем попробуйте снова.",
        "channel_add_fail": "❌ Не удалось определить канал/группу. Перешлите сообщение или отправьте @username/ID.",
        "channel_list_empty": "📭 Обязательных подписок пока нет.",
        "channel_list_title": "📋 Список обязательных подписок:",
        "channel_removed": "🗑 Канал удалён из списка.",
        "subscribe_required": "⚠️ Чтобы пользоваться ботом, подпишитесь на следующие канал(ы):",
        "check_sub_btn": "✅ Проверить",
        "still_not_subscribed": "❌ Вы ещё не подписаны на все каналы.",
        "now_subscribed": "✅ Спасибо! Теперь вы можете пользоваться ботом, отправьте ссылку.",
        "channel_type_public": "открытый канал/группа",
        "channel_subs_label": "{count} подписчиков",
        "channel_type_private": "закрытый канал/группа",
    },
    "en": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": (
            "Hello! 👋\n\nWelcome to <b>{bot_name}</b>!\n\n"
            "Send me a link from Instagram, YouTube, TikTok, Pinterest or Snapchat — "
            "I'll download the media for you. Use the button under the video to recognize "
            "the music in it and get it as an MP3 🎵"
        ),
        "send_link": "🔗 Send a link (Instagram / YouTube / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Downloading, please wait...",
        "caption": "✅ Thanks for using our bot!",
        "detect_music_btn": "🎵 Recognize music",
        "recognizing": "🎧 Recognizing the music...",
        "not_recognized": "😔 Sorry, couldn't recognize the music in this video.",
        "found_song": "🎶 Found: {title} — {artist}\n⏳ Downloading...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Search by artist",
        "btn_youtube_link": "🔍 Open on SoundCloud",
        "lyrics_notice": "📜 I can't display full lyrics due to copyright, but you can find them via the link below:",
        "tiktok_unavailable": "⚠️ Sorry, TikTok services aren't working right now. Please try again later.",
        "unsupported_link": "❌ This link isn't supported. Please send a link from Instagram, YouTube, TikTok, Pinterest or Snapchat.",
        "error": "❌ Something went wrong, please try again.",
        "no_link": "❗️ Please send a media link.",
        "admin_only": "⛔ This command is for admins only.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Stats:\n\n👤 Total users: {count}",
        "broadcast_ask": "✍️ Send the broadcast text:",
        "broadcast_done": "✅ Message sent to {count} users.",
        "file_expired": "⏱ Session expired, please send the link again.",
        "lang_set": "✅ Language set to English.",
        "searching": "🔍 Searching...",
        "search_no_results": "😔 Nothing found. Try a different search term.",
        "search_results_range": "Results {start}-{end} / {total}",
        "help": (
            "ℹ️ <b>Help</b>\n\n"
            "1) Send a link from Instagram, YouTube, TikTok, Pinterest or Snapchat.\n"
            "2) The bot downloads the media.\n"
            "3) Tap the 🎵 button under the video — the bot recognizes the music and sends it as MP3.\n"
            "4) Or just type a song/artist name — the bot will search YouTube and send the "
            "track you pick as MP3.\n\n"
            "To change language, use the \"🌐 Language\" button below."
        ),
        "btn_lang": "🌐 Language",
        "btn_help": "❓ Help",
        "btn_admin": "🛠 Admin panel",
        "btn_back": "⬅️ Back",
        "btn_stats": "📊 Stats",
        "btn_broadcast": "📢 Broadcast",
        "btn_add_channel": "➕ Add mandatory sub",
        "btn_list_channels": "📋 Mandatory subs",
        "btn_clear_cache": "🗑 Clear cache",
        "btn_db_export": "📤 DB export",
        "btn_db_import": "📥 DB import",
        "cache_cleared": "✅ Temporary cache cleared ({count} entries).",
        "db_export_empty": "📭 No users found in the database.",
        "db_export_caption": "📦 DB export — {count} users (.vk files inside a zip).",
        "db_export_fail": "❌ DB export failed.",
        "db_import_ask": "📥 To import, send a previously exported .zip or .vk file(s).",
        "db_import_done": "✅ Import finished: {added} added, {updated} updated, {failed} failed.",
        "db_import_fail": "❌ Could not read the file. Send a valid .zip or .vk file.",
        "db_import_no_file": "❗️ Please send a .zip or .vk file.",
        "ask_channel": (
            "📎 To add a channel/group:\n\n"
            "1) Make the bot an <b>administrator</b> there.\n"
            "2) Forward any message from it here, or send its @username or chat_id."
        ),
        "channel_added": "✅ \"{title}\" added to mandatory subscriptions.",
        "channel_add_fail_not_admin": "❌ Make the bot an administrator of that channel/group first, then try again.",
        "channel_add_fail": "❌ Couldn't detect the channel/group. Forward a message, or send its @username/ID.",
        "channel_list_empty": "📭 No mandatory subscriptions yet.",
        "channel_list_title": "📋 Mandatory subscriptions:",
        "channel_removed": "🗑 Channel removed from the list.",
        "subscribe_required": "⚠️ To use the bot, please subscribe to the following channel(s):",
        "check_sub_btn": "✅ Check",
        "still_not_subscribed": "❌ You haven't subscribed to all channels yet.",
        "now_subscribed": "✅ Thanks! You can use the bot now, send a link.",
        "channel_type_public": "public channel/group",
        "channel_subs_label": "{count} subs",
        "channel_type_private": "private channel/group",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "uz"
    text = TEXTS[lang].get(key, TEXTS["uz"][key])
    return text.format(**kwargs) if kwargs else text


# ============================================================
# DATABASE (PostgreSQL via asyncpg)
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


async def get_channel(channel_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM mandatory_channels WHERE id=$1", channel_id)


async def log_join_request(chat_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO join_requests (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            chat_id, user_id,
        )


async def has_join_request(chat_id: int, user_id: int) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM join_requests WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
        )
        return row is not None


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


async def get_all_users_export() -> list[dict]:
    """One record per user, including which mandatory channels they're
    subscribed to - used to build the .vk export files."""
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, lang, joined_at FROM users ORDER BY user_id")
        subs = await conn.fetch("SELECT chat_id, user_id FROM channel_subscribers")
    by_user: dict[int, list[int]] = {}
    for r in subs:
        by_user.setdefault(r["user_id"], []).append(r["chat_id"])
    records = []
    for u in users:
        records.append(
            {
                "user_id": u["user_id"],
                "lang": u["lang"],
                "joined_at": u["joined_at"].isoformat() if u["joined_at"] else None,
                "subscribed_channels": by_user.get(u["user_id"], []),
            }
        )
    return records


async def upsert_user_export(record: dict) -> bool:
    """Insert or update a single user record from a .vk import file.
    Returns True if this was a brand-new row, False if it updated an
    existing one."""
    user_id = int(record["user_id"])
    lang = record.get("lang") or "uz"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO users (user_id, lang) VALUES ($1, $2)
               ON CONFLICT (user_id) DO UPDATE SET lang = EXCLUDED.lang
               RETURNING (xmax = 0) AS inserted""",
            user_id, lang,
        )
        inserted = bool(row["inserted"]) if row else False
        for chat_id in record.get("subscribed_channels") or []:
            await conn.execute(
                "INSERT INTO channel_subscribers (chat_id, user_id) VALUES ($1, $2) "
                "ON CONFLICT DO NOTHING",
                int(chat_id), user_id,
            )
    return inserted


# ============================================================
# BOT / DISPATCHER
# ============================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


class EnsureUserRegisteredMiddleware(BaseMiddleware):
    """
    Registers a user in the DB the moment they interact with the bot in ANY
    way (any message text, any button press) - not only via /start. This
    matters for people who were already using an earlier version of this
    bot: they won't need to press /start again, whatever they send just
    gets them added to the users table if they're not there yet.
    """

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
    waiting_db_import = State()


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


def build_media_caption(source_url: str) -> str:
    """"BotName | Shazam | source" line shown under a downloaded video/photo
    (see reference screenshot), before music recognition is triggered."""
    bot_link = f"https://t.me/{BOT_USERNAME}?start=video" if BOT_USERNAME else ""
    bot_part = f'<a href="{bot_link}">{BOT_DISPLAY_NAME}</a>' if bot_link else BOT_DISPLAY_NAME
    shazam_part = '<a href="https://www.shazam.com">Shazam</a>'
    source_part = f'<a href="{source_url}">source</a>' if source_url else "source"
    return f"{bot_part} | {shazam_part} | {source_part}"


def build_recognized_caption(title: str, artist: str, source_url: str) -> str:
    """Caption the video is edited to once music recognition succeeds
    (see reference screenshot): song title line, blank line, then the
    same "BotName | Shazam | source" row as before."""
    header = f"{title} — {artist}".strip(" —") or title
    return f"{header}\n\n{build_media_caption(source_url)}"


def recognized_song_kb(lang: str, title: str, artist: str, artist_token: str) -> InlineKeyboardMarkup:
    """Buttons shown on the video after recognition (see reference
    screenshot): a row of quick-search links, plus a "search by artist"
    button that re-runs our own song search using the recognized artist."""
    query = urllib.parse.quote(f"{artist} {title}".strip())
    rows = [
        [
            InlineKeyboardButton(text="Google", url=f"https://www.google.com/search?q={query}"),
            InlineKeyboardButton(text="YouTube Music", url=f"https://music.youtube.com/search?q={query}"),
            InlineKeyboardButton(text="Spotify", url=f"https://open.spotify.com/search/{query}"),
        ],
        [InlineKeyboardButton(text=t(lang, "btn_artist_search"), callback_data=f"asearch:{artist_token}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_song_caption(song_link: str | None) -> str:
    """"@botusername | info" line shown under the sent MP3 (see reference
    screenshot). Tapping @botusername opens the bot and triggers /start
    via the deep-link start parameter; tapping "info" opens the song's
    source page (SoundCloud), when we have one."""
    bot_link = f"https://t.me/{BOT_USERNAME}?start=song" if BOT_USERNAME else ""
    bot_part = f'<a href="{bot_link}">@{BOT_USERNAME}</a>' if bot_link else f"@{BOT_USERNAME}"
    if song_link:
        return f'{bot_part} | <a href="{song_link}">info</a>'
    return bot_part


def song_result_kb(lang: str, title: str, artist: str, song_link: str | None) -> InlineKeyboardMarkup:
    """
    Single row under the sent MP3 (see reference screenshot):
    - wide "Lyrics" button links out to a lyrics search page instead of
      reproducing the full copyrighted lyrics text inside the bot.
    - narrow 🔍 icon-only button opens the song's source page.
    """
    query = f"{artist} {title}".strip() or title
    lyrics_url = "https://genius.com/search?q=" + urllib.parse.quote(query)
    row = [InlineKeyboardButton(text=t(lang, "btn_lyrics"), url=lyrics_url)]
    if song_link:
        row.append(InlineKeyboardButton(text="🔍", url=song_link))
    return InlineKeyboardMarkup(inline_keyboard=[row])


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
            [KeyboardButton(text=t(lang, "btn_clear_cache")), KeyboardButton(text=t(lang, "btn_db_export"))],
            [KeyboardButton(text=t(lang, "btn_db_import"))],
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
    await message.answer(f"<b>{BOT_DISPLAY_NAME}</b>")
    await message.answer(t("uz", "choose_lang"), reply_markup=lang_inline_kb())


@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    lang = call.data.split(":", 1)[1]
    await set_lang(call.from_user.id, lang)
    await call.message.edit_text(t(lang, "lang_set"))
    await call.message.answer(t(lang, "welcome", bot_name=BOT_DISPLAY_NAME))
    await call.message.answer(
        t(lang, "send_link"),
        reply_markup=user_reply_kb(lang, is_admin(call.from_user.id)),
    )
    await call.answer()


# ============================================================
# HANDLERS: persistent reply-keyboard buttons (user side)
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


# ============================================================
# HANDLERS: admin - clear in-memory cache
# ============================================================
@router.message(F.text.in_({v["btn_clear_cache"] for v in TEXTS.values()}))
async def btn_clear_cache(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    count = len(SEARCH_CACHE) + len(FILE_CACHE)
    SEARCH_CACHE.clear()
    FILE_CACHE.clear()
    _vk_token_cache.clear()
    # best-effort: remove any leftover temp download folders from this bot
    try:
        for name in os.listdir(DOWNLOAD_ROOT):
            path = os.path.join(DOWNLOAD_ROOT, name)
            if os.path.isdir(path) and (name.startswith("tmp") or "torona" in name.lower()):
                shutil.rmtree(path, ignore_errors=True)
    except Exception as e:
        log.warning("cache dir cleanup skipped: %s", e)
    await message.answer(t(lang, "cache_cleared", count=count), reply_markup=admin_reply_kb(lang))


# ============================================================
# HANDLERS: admin - DB export (zip of one .vk file per user)
# ============================================================
@router.message(F.text.in_({v["btn_db_export"] for v in TEXTS.values()}))
async def btn_db_export(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    try:
        records = await get_all_users_export()
    except Exception as e:
        log.warning("DB export failed: %s", e)
        await message.answer(t(lang, "db_export_fail"), reply_markup=admin_reply_kb(lang))
        return
    if not records:
        await message.answer(t(lang, "db_export_empty"), reply_markup=admin_reply_kb(lang))
        return

    export_dir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    zip_path = os.path.join(export_dir, f"db_export_{int(time.time())}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for rec in records:
                zf.writestr(f"{rec['user_id']}.vk", json.dumps(rec, ensure_ascii=False, indent=2))
        await message.answer_document(
            FSInputFile(zip_path, filename=os.path.basename(zip_path)),
            caption=t(lang, "db_export_caption", count=len(records)),
            reply_markup=admin_reply_kb(lang),
        )
    except Exception as e:
        log.warning("DB export zip/send failed: %s", e)
        await message.answer(t(lang, "db_export_fail"), reply_markup=admin_reply_kb(lang))
    finally:
        shutil.rmtree(export_dir, ignore_errors=True)


# ============================================================
# HANDLERS: admin - DB import (admin resends exported .zip / .vk files)
# ============================================================
@router.message(F.text.in_({v["btn_db_import"] for v in TEXTS.values()}))
async def btn_db_import_ask(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "db_import_ask"))
    await state.set_state(AdminStates.waiting_db_import)


def _parse_vk_records_from_bytes(filename: str, raw: bytes) -> list[dict]:
    """Returns a list of user record dicts found in one uploaded file.
    Supports a .zip full of .vk files, or a single .vk/.json file."""
    records = []
    if filename.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith((".vk", ".json")):
                    continue
                try:
                    records.append(json.loads(zf.read(name).decode("utf-8")))
                except Exception as e:
                    log.warning("skip bad entry %s in zip: %s", name, e)
    else:
        records.append(json.loads(raw.decode("utf-8")))
    return records


@router.message(AdminStates.waiting_db_import, F.document)
async def do_db_import(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    doc = message.document
    filename = doc.file_name or "upload.vk"
    if not filename.lower().endswith((".zip", ".vk", ".json")):
        await message.answer(t(lang, "db_import_no_file"))
        return

    added = updated = failed = 0
    try:
        file = await bot.get_file(doc.file_id)
        buf = await bot.download_file(file.file_path)
        raw = buf.read()
        records = _parse_vk_records_from_bytes(filename, raw)
        for rec in records:
            try:
                is_new = await upsert_user_export(rec)
                if is_new:
                    added += 1
                else:
                    updated += 1
            except Exception as e:
                log.warning("import: bad record skipped: %s", e)
                failed += 1
    except Exception as e:
        log.warning("DB import failed: %s", e)
        await message.answer(t(lang, "db_import_fail"), reply_markup=admin_reply_kb(lang))
        await state.clear()
        return

    await message.answer(
        t(lang, "db_import_done", added=added, updated=updated, failed=failed),
        reply_markup=admin_reply_kb(lang),
    )
    # stay in waiting_db_import state so the admin can send more files in a
    # row (e.g. several .vk files one after another); "⬅️ Orqaga" exits it
    # via the generic btn_back_to_user handler registered earlier.


@router.message(AdminStates.waiting_db_import)
async def db_import_wrong_content(message: Message):
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "db_import_no_file"))


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
# HANDLERS: admin - mandatory subscriptions (add / list / remove)
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
# HANDLER: join requests for private mandatory channels
#
# IMPORTANT: this only LOGS the request. It never calls
# bot.approve_chat_join_request() or decline_chat_join_request() - the
# channel owner/admin must approve requests manually in Telegram.
# The bot treats a user as "subscribed" only after Telegram reports them
# as an actual channel member (see get_unsubscribed_channels below),
# never just because they sent a join request.
# ============================================================
@router.chat_join_request()
async def on_join_request(update: ChatJoinRequest):
    await log_join_request(update.chat.id, update.from_user.id)


# ============================================================
# HANDLER: track join/leave events for mandatory channels
# (requires the bot to be an admin there - Telegram then sends
# chat_member updates for every member change in that chat)
# ============================================================
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
# ============================================================
# MANDATORY SUBSCRIPTION CHECK
# ============================================================
async def get_unsubscribed_channels(user_id: int):
    """
    A user only counts as subscribed once they are an ACTUAL member of the
    channel/group (status member/administrator/creator). For private
    channels this means the channel owner/admin must approve their join
    request first - merely sending a join request is NOT enough on its own.
    """
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


# Fallback order of YouTube "player_client" configs. Some of these dodge the
# "Sign in to confirm you're not a bot" check better than others depending on
# the datacenter IP the bot is hosted on, so we try them one by one.
PLAYER_CLIENT_FALLBACKS = [
    ["android", "web", "ios"],
    ["ios"],
    ["tv_embedded", "web"],
    ["mweb"],
]


def _is_bot_check_error(exc: Exception) -> bool:
    """Detect bot-check / auth errors worth retrying with another InnerTube client."""
    msg = str(exc).lower()
    return any(p in msg for p in (
        "sign in to confirm",
        "not a bot",
        "cookies",           # "use --cookies-from-browser"
        "http error 429",    # Too Many Requests
        "too many requests",
        "preconditionfailed",# InnerTube 412
        "error code: 152",   # client context rejected
        "video unavailable", # sometimes a masked bot-check
    ))


_cookie_hint_logged = False


def _raise_ytdlp_failure(last_exc):
    global _cookie_hint_logged
    if last_exc is not None and _is_bot_check_error(last_exc) and not _cookie_hint_logged:
        _cookie_hint_logged = True
        log.error(
            "YouTube is blocking all requests from this IP (Railway datacenter). "
            "Fix: export fresh cookies from your browser and set YOUTUBE_COOKIES_B64 "
            "in Railway environment variables. See instructions below the code."
        )
    if last_exc is None:
        raise RuntimeError("yt-dlp: all InnerTube client fallbacks exhausted.")
    raise last_exc


def _build_ydl_opts_base(outdir, player_clients):
    """Shared yt-dlp options for all download functions.

    - skip_webpage : talk directly to InnerTube API, skip the watch page
    - player_skip  : for clients with pre-signed URLs, skip JS player fetch
    With valid cookies these settings make requests pass bot-check on Railway.
    """
    _no_sig = {"android_testsuite", "android_creator", "tv_embedded"}
    needs_player = not all(c in _no_sig for c in player_clients)

    extractor_args = {
        "player_client": player_clients,
        "skip_webpage": ["1"],
    }
    if not needs_player:
        extractor_args["player_skip"] = ["webpage", "configs", "js"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {"youtube": extractor_args},
        "http_headers": {"User-Agent": DEFAULT_UA},
        "geo_bypass": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if outdir:
        opts["outtmpl"] = os.path.join(outdir, "%(id)s.%(ext)s")
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts

def _download_instagram_photo_fallback(url: str, outdir: str):
    """Instagram photo-only posts have no video formats, so yt-dlp's normal
    download raises 'No video formats found!'. This grabs the highest-res
    display image directly instead."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if "entries" in info:
        info = info["entries"][0]
    thumbs = info.get("thumbnails") or []
    img_url = None
    if thumbs:
        img_url = max(thumbs, key=lambda th: (th.get("width") or 0) * (th.get("height") or 0)).get("url")
    if not img_url:
        img_url = info.get("thumbnail")
    if not img_url:
        return None
    filepath = os.path.join(outdir, f"{info.get('id') or 'photo'}.jpg")
    req = urllib.request.Request(img_url, headers={"User-Agent": DEFAULT_UA})
    with urllib.request.urlopen(req, timeout=30) as resp, open(filepath, "wb") as f:
        f.write(resp.read())
    return filepath, info


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
            if "no video formats found" in str(e).lower() and "instagram" in url.lower():
                result = _download_instagram_photo_fallback(url, outdir)
                if result:
                    return result
                raise
            if "youtube" not in url and "youtu.be" not in url:
                raise
            if not _is_bot_check_error(e):
                raise
            log.warning(
                "InnerTube client %s (attempt %d) blocked — trying next client",
                player_clients, attempt + 1,
            )
            continue
    _raise_ytdlp_failure(last_exc)


async def download_media(url: str, outdir: str, platform: str):
    loop = asyncio.get_running_loop()
    use_proxy = platform != "tiktok"  # TikTok is never proxied, per requirements
    return await loop.run_in_executor(None, _run_ytdlp_download, url, outdir, use_proxy)


# ============================================================
# SONG SEARCH: SoundCloud (primary) + VK Music (fallback)
#
# YouTube is deliberately NOT used here - Railway's datacenter IP gets
# permanently bot-checked by YouTube regardless of client/cookies tricks.
# SoundCloud has no such bot-check for public tracks. VK Music is used only
# when a track is missing/copyright-blocked on SoundCloud, and requires a
# real VK account (VK_LOGIN + VK_PASSWORD env vars) to authorize search.
# ============================================================

def _run_soundcloud_search_download(query: str, outdir: str) -> tuple[str, str] | None:
    """Search+download the first SoundCloud result. Returns (mp3_path, webpage_url) or None."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    if SOUNDCLOUD_COOKIES_FILE and os.path.exists(SOUNDCLOUD_COOKIES_FILE):
        ydl_opts["cookiefile"] = SOUNDCLOUD_COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch1:{query}", download=True)
            if not info:
                return None
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    return None
                info = entries[0]
            filename = ydl.prepare_filename(info)
            mp3_path = os.path.splitext(filename)[0] + ".mp3"
            if not os.path.exists(mp3_path):
                return None
            webpage_url = info.get("webpage_url") or info.get("url") or ""
            return mp3_path, webpage_url
    except Exception as e:
        log.warning("SoundCloud search failed for '%s': %s", query, e)
        return None


# --- VK Music (fallback) ---------------------------------------------------
_VK_API_VERSION = "5.131"
_VK_CLIENT_ID = "2685278"          # Kate Mobile public client id
_VK_CLIENT_SECRET = "lxhD8OD7dMsqtXIm5IUY"  # Kate Mobile public client secret
_vk_token_cache: dict = {}


async def _vk_get_token() -> str | None:
    if _vk_token_cache.get("token"):
        return _vk_token_cache["token"]
    if not VK_LOGIN or not VK_PASSWORD:
        log.warning("VK fallback skipped: VK_LOGIN/VK_PASSWORD not set in environment")
        return None
    params = {
        "grant_type": "password",
        "client_id": _VK_CLIENT_ID,
        "client_secret": _VK_CLIENT_SECRET,
        "username": VK_LOGIN,
        "password": VK_PASSWORD,
        "v": _VK_API_VERSION,
        "2fa_supported": 1,
        "scope": "audio,offline",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://oauth.vk.com/token", params=params) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("VK auth request failed: %s", e)
        return None
    token = data.get("access_token")
    if not token:
        log.warning("VK auth failed: %s", data.get("error_description") or data)
        return None
    _vk_token_cache["token"] = token
    return token


async def _vk_search_tracks(query: str, count: int = 1) -> list[dict]:
    token = await _vk_get_token()
    if not token:
        return []
    params = {"q": query, "count": count, "access_token": token, "v": _VK_API_VERSION}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.vk.com/method/audio.search", params=params) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        log.warning("VK search request failed: %s", e)
        return []
    if "error" in data:
        log.warning("VK search error: %s", data["error"].get("error_msg"))
        return []
    return (data.get("response") or {}).get("items") or []


async def _vk_download_track(track: dict, outdir: str) -> tuple[str, str | None] | None:
    url = track.get("url")
    if not url:
        return None
    filename = os.path.join(outdir, f"vk_{uuid.uuid4().hex}.mp3")
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                with open(filename, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
    except Exception as e:
        log.warning("VK track download failed: %s", e)
        return None
    # No public webpage link to show (VK audio requires login to view) - return None.
    return filename, None


async def vk_search_and_download(query: str, outdir: str) -> tuple[str, str] | None:
    tracks = await _vk_search_tracks(query, count=1)
    if not tracks:
        return None
    return await _vk_download_track(tracks[0], outdir)


async def search_and_download_song(query: str, outdir: str) -> tuple[str, str]:
    """Search order: SoundCloud -> VK Music. Raises RuntimeError if both fail."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run_soundcloud_search_download, query, outdir)
    if result:
        return result
    log.info("SoundCloud had no usable result for '%s' - trying VK Music", query)
    result = await vk_search_and_download(query, outdir)
    if result:
        return result
    raise RuntimeError(f"'{query}' uchun SoundCloud yoki VK Music'da hech narsa topilmadi")


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


def _run_soundcloud_list_search(query: str, limit: int) -> list[dict]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "socket_timeout": 30,
    }
    if SOUNDCLOUD_COOKIES_FILE and os.path.exists(SOUNDCLOUD_COOKIES_FILE):
        ydl_opts["cookiefile"] = SOUNDCLOUD_COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
            entries = [e for e in (info.get("entries") or []) if e]
    except Exception as e:
        log.warning("SoundCloud list search failed for '%s': %s", query, e)
        entries = []

    results = []
    for e in entries:
        results.append(
            {
                "id": e.get("id"),
                "title": e.get("title") or "Unknown",
                "uploader": e.get("uploader") or "",
                "duration": e.get("duration"),
                "view_count": e.get("view_count") or e.get("play_count"),
                "url": e.get("url") or e.get("webpage_url"),
                "source": "soundcloud",
            }
        )
    return results


async def _vk_list_search(query: str, limit: int) -> list[dict]:
    tracks = await _vk_search_tracks(query, count=min(limit, 20))
    results = []
    for tr in tracks:
        artist = tr.get("artist", "")
        title = tr.get("title", "")
        results.append(
            {
                "id": f"{tr.get('owner_id')}_{tr.get('id')}",
                "title": title or "Unknown",
                "uploader": artist,
                "duration": tr.get("duration"),
                "view_count": None,
                "url": tr.get("url"),  # direct mp3 link, stored for download
                "source": "vk",
            }
        )
    return results


async def text_search_songs(query: str, limit: int = SEARCH_FETCH_LIMIT) -> list[dict]:
    """Search order: SoundCloud first; if empty, also try VK Music."""
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _run_soundcloud_list_search, query, limit)
    if not results:
        log.info("SoundCloud list search empty for '%s' - trying VK Music", query)
        results = await _vk_list_search(query, limit)
    return results


# Kept for backward-compat with existing handler code.
text_search_youtube = text_search_songs


def _run_soundcloud_download_by_url(url: str, outdir: str) -> str:
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "socket_timeout": 30,
        "retries": 3,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    if SOUNDCLOUD_COOKIES_FILE and os.path.exists(SOUNDCLOUD_COOKIES_FILE):
        ydl_opts["cookiefile"] = SOUNDCLOUD_COOKIES_FILE
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return os.path.splitext(filename)[0] + ".mp3"


async def download_song_by_url(url: str, outdir: str, source: str = "soundcloud") -> str:
    """Downloads a track picked from the search list. `source` tells us
    whether `url` is a SoundCloud page URL (needs yt-dlp) or a direct VK
    mp3 link (plain HTTP GET)."""
    if source == "vk":
        filename = os.path.join(outdir, f"vk_{uuid.uuid4().hex}.mp3")
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"VK download HTTP {resp.status}")
                with open(filename, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        f.write(chunk)
        return filename

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_soundcloud_download_by_url, url, outdir)


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
        # No audio track in the source (e.g. a silent GIF/video) - this is a
        # normal case, not a real error: just tell the user music wasn't found.
        if "does not contain any stream" in stderr_text or "Output file does not contain any stream" in stderr_text:
            return None
        raise RuntimeError(f"ffmpeg failed: {stderr_text[-500:]}")
    return audio_path


async def recognize_song(audio_path: str) -> dict | None:
    if Shazam is None:
        log.warning("shazamio not installed — music recognition unavailable")
        return None
    if not audio_path or not os.path.exists(audio_path):
        log.warning("recognize_song: audio file not found: %s", audio_path)
        return None
    try:
        shazam = Shazam()
        result = await shazam.recognize(audio_path)
    except Exception as e:
        log.warning("shazamio error: %s", e)
        return None
    track = result.get("track")
    if not track:
        return None
    return {"title": track.get("title", "Unknown"), "artist": track.get("subtitle", "Unknown")}


# ============================================================
# HANDLERS: media link
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
    caption = build_media_caption(url)
    keyboard = music_inline_kb(lang, token)

    try:
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            await message.answer_photo(FSInputFile(filepath), caption=caption, reply_markup=keyboard)
        else:
            await message.answer_video(FSInputFile(filepath), caption=caption, reply_markup=keyboard)
        FILE_CACHE[token] = {"filepath": filepath, "source_url": url}
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

    # otherwise `action` is the 1..8 button - the user picked a song
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
        source = entry.get("source", "soundcloud")
        mp3_path = await download_song_by_url(entry["url"], work_dir, source=source)
        title = entry.get("title") or "Unknown"
        performer = entry.get("uploader") or ""
        # VK's stored "url" is a raw direct-stream link, not a shareable page -
        # don't show it as a "source" button, only SoundCloud page links make sense there.
        link_for_button = entry.get("url") if source == "soundcloud" else None
        await call.message.answer_audio(
            FSInputFile(mp3_path),
            title=title,
            performer=performer,
            caption=build_song_caption(link_for_button),
            reply_markup=song_result_kb(lang, title, performer, link_for_button),
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
    entry = FILE_CACHE.get(token)
    video_path = entry.get("filepath") if entry else None
    source_url = entry.get("source_url", "") if entry else ""
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

        # edit the video's own caption/keyboard to show the recognized song
        # (see reference screenshot) before downloading the mp3 itself
        artist_token = uuid.uuid4().hex[:10]
        ARTIST_SEARCH_CACHE[artist_token] = song["artist"]
        asyncio.create_task(_expire_artist_cache(artist_token, delay=SEARCH_CACHE_TTL_SECONDS))
        try:
            await call.message.edit_caption(
                caption=build_recognized_caption(song["title"], song["artist"], source_url),
                reply_markup=recognized_song_kb(lang, song["title"], song["artist"], artist_token),
            )
        except Exception as e:
            log.warning("could not edit video caption: %s", e)

        query = f"{song['artist']} {song['title']}"
        mp3_path, song_link = await search_and_download_song(query, work_dir)
        await call.message.answer_audio(
            FSInputFile(mp3_path),
            title=song["title"],
            performer=song["artist"],
            caption=build_song_caption(song_link),
            reply_markup=song_result_kb(lang, song["title"], song["artist"], song_link),
        )
        # the mp3 itself is the result now - clear the "recognizing.../found..." status line
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
        # clean everything for this request right away - free-tier disk friendly
        shutil.rmtree(work_dir, ignore_errors=True)
        shutil.rmtree(video_outdir, ignore_errors=True)
        FILE_CACHE.pop(token, None)


async def _expire_artist_cache(token: str, delay: int):
    await asyncio.sleep(delay)
    ARTIST_SEARCH_CACHE.pop(token, None)


@router.callback_query(F.data.startswith("asearch:"))
async def cb_artist_search(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    token = call.data.split(":", 1)[1]
    artist = ARTIST_SEARCH_CACHE.get(token)
    if not artist:
        await call.answer(t(lang, "file_expired"), show_alert=True)
        return

    await call.answer()
    status = await call.message.answer(t(lang, "searching"))
    try:
        results = await text_search_songs(artist)
    except Exception as e:
        log.warning("artist search failed: %s", e)
        await status.edit_text(t(lang, "error"))
        return
    if not results:
        await status.edit_text(t(lang, "search_no_results"))
        return

    search_token = uuid.uuid4().hex[:12]
    SEARCH_CACHE[search_token] = {"query": artist, "results": results, "page": 0}
    asyncio.create_task(_expire_search_cache(search_token, delay=SEARCH_CACHE_TTL_SECONDS))
    text, kb = render_search_page(lang, search_token)
    await status.edit_text(text, reply_markup=kb)


# ============================================================
# ENTRYPOINT
# ============================================================
async def main():
    global BOT_DISPLAY_NAME, BOT_USERNAME

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set!")

    await init_db()

    me = await bot.get_me()
    BOT_DISPLAY_NAME = me.first_name or me.username or "Bot"
    BOT_USERNAME = me.username or ""
    log.info("Bot started as @%s (%s)", me.username, BOT_DISPLAY_NAME)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
