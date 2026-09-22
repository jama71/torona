import asyncio
import importlib.util
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
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
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest
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
    _system_ffprobe = shutil.which("ffprobe")
    _ffprobe_link = os.path.join(_ffmpeg_dir, "ffprobe")
    if _system_ffprobe and not os.path.exists(_ffprobe_link):
        try:
            os.symlink(_system_ffprobe, _ffprobe_link)
        except Exception:
            pass
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
# Optional, YouTube-specific proxy override (falls back to GENERAL_PROXY,
# then no proxy, if unset). See _pick_youtube_proxy() below.
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "").strip() or None
YOUTUBE_PROXY_LIST = [p.strip() for p in os.getenv("YOUTUBE_PROXY_LIST", "").split(",") if p.strip()]

# Song search (text search + Shazam recognition) uses SoundCloud first, then
# YouTube (guarded by a circuit breaker, see YOUTUBE_BREAKER) and finally VK
# Music, but only if VK credentials are configured.
SOUNDCLOUD_COOKIES_FILE = os.getenv("SOUNDCLOUD_COOKIES_FILE", "").strip() or None
VK_LOGIN = os.getenv("VK_LOGIN", "").strip()
VK_PASSWORD = os.getenv("VK_PASSWORD", "").strip()
# Preferred: a long-lived token obtained once via the Kate Mobile OAuth
# implicit flow (oauth.vk.com/authorize?...&response_type=token). When set,
# this is used directly and VK_LOGIN/VK_PASSWORD password-grant auth (which
# can trigger VK security checks / captcha and lock out for 30 min) is
# skipped entirely.
VK_ACCESS_TOKEN = os.getenv("VK_ACCESS_TOKEN", "").strip() or None


def _repair_cookie_line(line: str) -> str | None:
    """Netscape cookie lines are tab-separated (7 fields). Some copy-paste
    paths (chat UIs, some env-var editors) can collapse tabs into spaces,
    or mangle a comment line (e.g. drop its leading '#') - if that
    happened but the 7 fields are still intact, rejoin them with real
    tabs. Comment/blank lines pass through untouched. Anything else that
    still can't be parsed as a cookie line is dropped (returns None)
    instead of being handed to yt-dlp's cookiejar parser broken, which
    would otherwise just print its own warning and skip it anyway."""
    if line.startswith("#") or not line.strip():
        return line
    if line.count("\t") == 6:
        return line
    parts = line.split()
    if len(parts) == 7:
        return "\t".join(parts)
    return None


def _normalize_cookies_content(content: str) -> str:
    repaired = (_repair_cookie_line(l) for l in content.splitlines())
    return "\n".join(l for l in repaired if l is not None) + "\n"


def _count_valid_cookie_lines(content: str) -> tuple[int, int]:
    data_lines = [l for l in content.splitlines() if l.strip() and not l.startswith("#")]
    valid = [l for l in data_lines if l.count("\t") == 6]
    return len(valid), len(data_lines)


# SECURITY NOTE (fixed): this used to contain a real, live YouTube account's
# session cookies (SID/PSID/APISID/etc.) hardcoded directly in the source.
# That is a full account-takeover credential leak the moment this file is
# committed anywhere semi-public (GitHub, a shared Railway project, etc.) -
# anyone with those cookie values can act as that Google account. It has
# been removed. If you pasted this file into a public repo, rotate that
# Google account's password now and revoke its sessions.
#
# Cookies are no longer baked into the code and there is no base64 layer
# either. Each platform reads exactly one env var (Railway → Variables),
# whose value is the raw, plain-text content of a cookies.txt file
# (Netscape format - what "Get cookies.txt LOCALLY" exports):
#   YOUTUBE_COOKIES, INSTAGRAM_COOKIES, FACEBOOK_COOKIES
DEFAULT_YOUTUBE_COOKIES = ""


def _write_cookies_file(content: str, filename: str = "yt_cookies.txt") -> str:
    # yt-dlp's cookiejar loader only checks the *very first line* of the
    # file for the literal "# Netscape HTTP Cookie File" (or "# HTTP Cookie
    # File") header - if that line is missing or malformed it rejects the
    # whole file with "does not look like a Netscape format cookies file",
    # even if every actual cookie line below is perfectly fine. This has
    # been observed to happen when a value is pasted into some env-var UIs
    # (e.g. Railway's) that strip/mangle lines starting with "#" - the
    # header comment silently disappears in transit. So: always force the
    # canonical header onto the first line of the file we write, regardless
    # of whether the incoming content already had one (if it did, this
    # replaces it with a known-good copy; if it didn't, this adds it).
    lines = content.splitlines()
    if lines and lines[0].strip().lstrip("#").strip().lower().startswith(("netscape http cookie file", "http cookie file")):
        lines = lines[1:]
    content = "# Netscape HTTP Cookie File\n" + "\n".join(lines) + ("\n" if lines else "")
    cookies_path = os.path.join(tempfile.gettempdir(), filename)
    with open(cookies_path, "w", encoding="utf-8") as f:
        f.write(content)
    return cookies_path


def _try_load_cookie_candidate(
    source: str, content: str, logger, filename: str = "yt_cookies.txt", label: str = "YouTube"
) -> str | None:
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
    path = _write_cookies_file(content, filename)
    logger.info("%s cookies loaded from %s (%d cookie lines).", label, source, valid)
    return path


def _setup_youtube_cookies() -> str | None:
    """
    YouTube increasingly blocks cloud/datacenter IPs (Railway included) with
    "Sign in to confirm you're not a bot" REGARDLESS of which player_client
    yt-dlp uses. The only reliable fix is real browser cookies.

    Reads exactly ONE env var: YOUTUBE_COOKIES. Its value is the raw,
    plain-text content of a cookies.txt file (Netscape format) - e.g. what
    the "Get cookies.txt LOCALLY" browser extension exports. Not base64.
    """
    logger = logging.getLogger("bot")
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()

    if not raw:
        logger.warning(
            "No usable YouTube cookies found (YOUTUBE_COOKIES env var is unset) - relying on "
            "player_client fallback only, which YouTube may still bot-check."
        )
        return None

    result = _try_load_cookie_candidate("YOUTUBE_COOKIES", raw, logger)
    if not result:
        logger.warning(
            "YOUTUBE_COOKIES is set but doesn't look like a valid cookies.txt - re-export it with "
            "'Get cookies.txt LOCALLY' while logged into youtube.com and paste the full file content."
        )
    return result


def _setup_instagram_cookies() -> str | None:
    """
    Instagram rate-limits/blocks datacenter IPs too ("Please wait a few
    minutes" / login-wall errors), same story as YouTube. Real browser
    cookies from a logged-in Instagram account fix this. Fully optional,
    set only if the errors show up.

    Reads exactly ONE env var: INSTAGRAM_COOKIES. Same plain-text
    cookies.txt format as YOUTUBE_COOKIES above, not base64.
    """
    logger = logging.getLogger("bot")
    raw = os.getenv("INSTAGRAM_COOKIES", "").strip()

    if not raw:
        logger.info("No Instagram cookies configured (INSTAGRAM_COOKIES unset) - using defaults "
                     "unless rate-limit errors show up.")
        return None

    result = _try_load_cookie_candidate(
        "INSTAGRAM_COOKIES", raw, logger, filename="instagram_cookies.txt", label="Instagram"
    )
    if not result:
        logger.warning(
            "INSTAGRAM_COOKIES is set but doesn't look like a valid cookies.txt - re-export it with "
            "'Get cookies.txt LOCALLY' while logged into instagram.com and paste the full file content."
        )
    return result


def _setup_facebook_cookies() -> str | None:
    """Facebook login-walls most videos/reels for logged-out (datacenter IP)
    requests. Optional, same pattern as above.

    Reads exactly ONE env var: FACEBOOK_COOKIES. Plain-text cookies.txt
    content, not base64.
    """
    logger = logging.getLogger("bot")
    raw = os.getenv("FACEBOOK_COOKIES", "").strip()

    if not raw:
        logger.info("No Facebook cookies configured (FACEBOOK_COOKIES unset) - many Facebook "
                     "videos are login-walled and will fail without them.")
        return None

    result = _try_load_cookie_candidate(
        "FACEBOOK_COOKIES", raw, logger, filename="facebook_cookies.txt", label="Facebook"
    )
    if not result:
        logger.warning(
            "FACEBOOK_COOKIES is set but doesn't look like a valid cookies.txt - re-export it with "
            "'Get cookies.txt LOCALLY' while logged into facebook.com and paste the full file content."
        )
    return result


# Path to a cookies.txt (Netscape format) that helps yt-dlp bypass YouTube's
# "Sign in to confirm you're not a bot" checks. See _setup_youtube_cookies().
COOKIES_FILE = _setup_youtube_cookies()

# Optional: same idea but for Instagram's rate-limit/login-wall errors.
# See _setup_instagram_cookies().
INSTAGRAM_COOKIES_FILE = _setup_instagram_cookies()

# Optional: Facebook login-wall cookies. See _setup_facebook_cookies().
FACEBOOK_COOKIES_FILE = _setup_facebook_cookies()


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
            "Yangi cookies eksport qilib YOUTUBE_COOKIES ga qo\'ying.",
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
CACHE_TTL_SECONDS = 60  # free-tier disk friendly: auto-clean unused files after 1 min
                        # (was 5 min - shortened since Railway's 1GB /tmp fills up fast
                        # under concurrent downloads)

# The iOS-normalize pass only pays for a REAL re-encode when Instagram's
# codec isn't already iOS-safe (otherwise it's a near-free remux, see
# _ffmpeg_normalize_for_ios), but that real re-encode is still the single
# most CPU/memory-hungry thing this bot does (~15-20s, 200-400MB). Set
# SKIP_IOS_NORMALIZE=true in Railway env vars to disable it completely if
# the free-tier plan is still under memory pressure - Instagram videos will
# just be sent as-is (Android/most players handle them fine either way).
SKIP_IOS_NORMALIZE = os.getenv("SKIP_IOS_NORMALIZE", "false").strip().lower() in ("1", "true", "yes")

# Hard cap on how many entries each in-memory cache dict can hold, independent
# of TTL - bounds worst-case memory even if a lot of distinct queries/files
# come in faster than their TTL would naturally clear them. Oldest entry is
# evicted first (dicts preserve insertion order in Python 3.7+).
FILE_CACHE_MAX_SIZE = 20
SEARCH_CACHE_MAX_SIZE = 50
ARTIST_SEARCH_CACHE_MAX_SIZE = 30


def _cache_put(cache: dict, key, value, max_size: int):
    """Insert into a bounded FIFO cache dict, evicting the oldest entry(ies)
    first if this would push it over max_size."""
    cache[key] = value
    while len(cache) > max_size:
        oldest_key = next(iter(cache))
        cache.pop(oldest_key, None)


# Memory watermark: reject new heavy jobs (downloads/re-encodes) while the
# process is already using a lot of RAM, instead of letting Railway's OOM
# killer take down the whole bot mid-request. Purely a safety valve - on a
# healthy 1GB plan this basically never triggers.
MEMORY_WATERMARK_MB = int(os.getenv("MEMORY_WATERMARK_MB", "750"))


def _current_rss_mb() -> float | None:
    """Current process resident memory in MB, read straight from
    /proc/self/status (Linux-only, which Railway containers always are) -
    no extra dependency like psutil needed. Returns None if unavailable
    (e.g. running locally on a non-Linux OS) so callers can just skip the
    check rather than crash."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024
    except Exception:
        return None
    return None

URL_RE = re.compile(r"(https?://\S+)")
PLATFORM_PATTERNS = {
    "instagram": re.compile(r"instagram\.com"),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)"),
    "tiktok": re.compile(r"tiktok\.com"),
    "pinterest": re.compile(r"(pinterest\.com|pin\.it)"),
    "snapchat": re.compile(r"(snapchat\.com|snap\.com)"),
    "facebook": re.compile(r"(facebook\.com|fb\.watch)"),
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
# CONCURRENCY LIMIT — tuned for the current Railway plan
# (2 vCPU / 1 GB RAM, see Replica Limits in the Railway dashboard).
#
# Every video/audio job below spawns a yt-dlp + ffmpeg subprocess.
# A single one of those can use 150-400MB RAM (more for an Instagram
# HEVC re-encode). Running more than ~2 of them truly in parallel on
# a 1GB box risks the host OOM-killing the whole service.
#
# Rather than lowering quality (smaller resolution, more compression)
# to let more requests run at once, extra requests are QUEUED: they
# wait their turn for a free slot instead of running underpowered or
# crashing the process. Users see a short "navbatda" message while
# they wait, then the normal status text once their job starts.
# If you upgrade the Railway plan, raise HEAVY_JOB_SLOTS accordingly
# (roughly: 1 slot per 400-500MB of available RAM).
# ============================================================
HEAVY_JOB_SLOTS = 2
HEAVY_JOB_SEMAPHORE = asyncio.Semaphore(HEAVY_JOB_SLOTS)


class HeavyJobSlot:
    """Async context manager that serializes heavy (download/encode/
    recognize) work behind HEAVY_JOB_SEMAPHORE.

    If both slots are busy when a new job arrives, it edits `status`
    to a "queued" message so the user understands the wait, then swaps
    it to `busy_text_key`'s text once a slot actually frees up and the
    job starts running.
    """

    __slots__ = ("status", "lang", "busy_text_key")

    def __init__(self, status, lang: str, busy_text_key: str):
        self.status = status
        self.lang = lang
        self.busy_text_key = busy_text_key

    async def __aenter__(self):
        # Reject new heavy work outright while the process is already under
        # memory pressure, rather than letting it pile on top and risk an
        # OOM kill of the whole bot. Checked before even queueing, since a
        # free semaphore slot doesn't mean memory is actually free (other
        # things - caches, a job that just finished - can still be holding it).
        rss = _current_rss_mb()
        if rss is not None and rss > MEMORY_WATERMARK_MB:
            log.warning(
                "MEMORY_WATERMARK_EXCEEDED: RSS=%.0fMB > %dMB limit, rejecting new heavy job",
                rss, MEMORY_WATERMARK_MB,
            )
            raise RuntimeError(f"MEMORY_WATERMARK_EXCEEDED: server is using {rss:.0f}MB RAM right now")

        was_queued = HEAVY_JOB_SEMAPHORE.locked()
        if was_queued:
            try:
                await self.status.edit_text(t(self.lang, "queued"))
            except Exception:
                pass
        await HEAVY_JOB_SEMAPHORE.acquire()
        # Only touch the status text again if we actually showed the
        # "queued" message above - otherwise leave whatever the caller
        # had already put there (e.g. a "found: <song>" message).
        if was_queued:
            try:
                await self.status.edit_text(t(self.lang, self.busy_text_key))
            except Exception:
                pass
        return self

    async def __aexit__(self, exc_type, exc, tb):
        HEAVY_JOB_SEMAPHORE.release()
        return False


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
        "queued": "⏳ Hozir band, navbatingiz kelishi bilan boshlanadi...",
        "caption": "✅ Botimizdan foydalanganingiz uchun rahmat!",
        "detect_music_btn": "🎵 Musiqani aniqlash",
        "recognizing": "🎧 Musiqa aniqlanmoqda...",
        "not_recognized": "😔 Kechirasiz, bu videodagi musiqani aniqlab bo'lmadi.",
        "found_song": "🎶 Topildi: {title} — {artist}\n⏳ Yuklab olinmoqda...",
        "download_failed_yt_link": "❌ Musiqani yuklab bo'lmadi.\nQuyidagi havola orqali topishingiz mumkin:\n{link}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Rassom bo'yicha qidirish",
        "btn_youtube_link": "🔍 Manbada ochish",
        "lyrics_notice": "📜 Qo'shiq matnini mualliflik huquqi tufayli to'liq ko'rsata olmayman, lekin quyidagi havoladan uni topishingiz mumkin:",
        "tiktok_unavailable": "⚠️ Kechirasiz, hozircha TikTok xizmatlari ishlamayapti. Birozdan so'ng qayta urinib ko'ring.",
        "link_not_found": "❌ Bu post topilmadi — o'chirilgan, yopiq (private) yoki linkda xatolik bo'lishi mumkin.",
        "err_private": "🔐 Bu post yopiq (private) yoki yuklab olish cheklangan.\nUni ilova ichidan ulashib ko'ring yoki ochiq (public) qilishni so'rang.",
        "err_expired": "⏰ Bu kontent muddati tugagan (masalan, Snapchat story faqat 24 soat ochiq turadi) va endi mavjud emas.",
        "err_stale_cookie": "⚠️ Instagram hozircha bu postni bermayapti. Birozdan so'ng qayta urinib ko'ring yoki havolani tekshiring.",
        "err_youtube_blocked": "⏳ YouTube hozircha vaqtincha ishlamayapti, keyinroq urinib ko'ring.",
        "err_media_too_large": "⚠️ Fayl juda katta yoki uzun (Telegram bot limiti ~50 MB, treklar uchun ~20 daqiqa). Boshqa variantni tanlang.",
        "err_file_too_big_input": "⚠️ Yuborilgan fayl juda katta (bot 20 MB gacha fayllarni o'qiy oladi). Qisqaroq video yuboring.",
        "err_busy": "⏳ Bot hozir band (band xotira), birozdan keyin qayta urinib ko'ring.",
        "err_pinterest_video": "🎬 Bu Pinterest videosini hozircha yuklab bo'lmadi.",
        "err_facebook_parse": "❌ Bu Facebook video'sini yuklab bo'lmadi, ehtimol u shaxsiy (private) yoki cheklangan.",
        "unsupported_link": "❌ Bu havola qo'llab-quvvatlanmaydi. Instagram, YouTube, TikTok, Pinterest, Facebook yoki Snapchat havolasini yuboring.",
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
            "yoki uning @username'ini, yoki chat_id sini yuboring.\n\n"
            "⚠️ Yopiq (private) kanal bo'lsa, botga <b>'Invite Users via Link'</b> huquqini ham bering.\n"
            "ℹ️ Taklif havolasi (https://t.me/+...) yuborish ishlamaydi — forward qiling."
        ),
        "channel_added": "✅ \"{title}\" majburiy obunalar ro'yxatiga qo'shildi.",
        "channel_add_fail_not_admin": "❌ Botni avval o'sha kanal/guruhga administrator qiling, keyin qaytadan urinib ko'ring.",
        "channel_add_fail_link": "❌ Kanal uchun taklif havolasi (invite link) yaratib bo'lmadi, shuning uchun kanal saqlanmadi. Botning o'sha kanalda 'Invite Users via Link' huquqi borligini tekshirib, qaytadan urinib ko'ring.",
        "channel_add_fail_not_found": "❌ Bunday kanal/guruh topilmadi. Username'ni tekshiring (@kanal), yoki eng ishonchlisi — o'sha kanaldan istalgan xabarni menga forward qiling.",
        "channel_add_fail_invite_link": "❌ Taklif havolasi (https://t.me/+...) orqali kanalni qo'shib bo'lmaydi — Telegram bunga ruxsat bermaydi.\n\nBuning o'rniga: o'sha kanaldan istalgan xabarni menga <b>forward</b> qiling, yoki ochiq kanal bo'lsa @username yuboring.",
        "channel_add_fail_wrong_type": "❌ Faqat kanal yoki guruhni majburiy obuna sifatida qo'shish mumkin.",
        "channel_add_fail_no_invite_perm": "❌ Botda bu kanalda taklif havolasi yaratish huquqi yo'q.\n\nKanal sozlamalari → Administratorlar → Bot → <b>'Invite Users via Link'</b> huquqini yoqing va qayta urinib ko'ring.",
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
        "queued": "⏳ Сейчас все занято, начнём, как только освободится место в очереди...",
        "caption": "✅ Спасибо, что пользуетесь ботом!",
        "detect_music_btn": "🎵 Распознать музыку",
        "recognizing": "🎧 Распознаём музыку...",
        "not_recognized": "😔 Не удалось распознать музыку в этом видео.",
        "found_song": "🎶 Найдено: {title} — {artist}\n⏳ Загружается...",
        "download_failed_yt_link": "❌ Не удалось скачать музыку.\nВы можете найти её по этой ссылке:\n{link}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Текст песни",
        "btn_artist_search": "🔍 Поиск по исполнителю",
        "btn_youtube_link": "🔍 Открыть источник",
        "lyrics_notice": "📜 Не могу показать полный текст песни из-за авторских прав, но вы можете найти его по ссылке ниже:",
        "tiktok_unavailable": "⚠️ Извините, сервисы TikTok сейчас не работают. Попробуйте позже.",
        "link_not_found": "❌ Пост не найден — он мог быть удалён, закрыт (private) или ссылка неверна.",
        "err_private": "🔐 Этот пост закрыт (private) или загрузка ограничена владельцем.\nПопробуйте поделиться им из самого приложения или попросите сделать его публичным.",
        "err_expired": "⏰ Срок действия этого контента истёк (например, Snapchat-истории доступны только 24 часа) и он больше не доступен.",
        "err_stale_cookie": "⚠️ Instagram сейчас не отдаёт этот пост. Попробуйте ещё раз чуть позже или проверьте ссылку.",
        "err_youtube_blocked": "⏳ YouTube сейчас временно не работает, попробуйте позже.",
        "err_media_too_large": "⚠️ Файл слишком большой или длинный (лимит бота Telegram ~50 МБ, для треков ~20 минут). Выберите другой вариант.",
        "err_file_too_big_input": "⚠️ Присланный файл слишком большой (бот читает файлы до 20 МБ). Отправьте видео покороче.",
        "err_busy": "⏳ Бот сейчас перегружен, попробуйте ещё раз через некоторое время.",
        "err_pinterest_video": "🎬 Это видео с Pinterest сейчас не удалось скачать.",
        "err_facebook_parse": "❌ Не удалось скачать это видео с Facebook, возможно оно приватное или ограничено.",
        "unsupported_link": "❌ Эта ссылка не поддерживается. Отправьте ссылку с Instagram, YouTube, TikTok, Pinterest, Facebook или Snapchat.",
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
        "channel_add_fail_link": "❌ Не удалось создать пригласительную ссылку для канала, поэтому канал не сохранён. Проверьте, что у бота есть право «Invite Users via Link» в этом канале, и попробуйте снова.",
        "channel_add_fail_not_found": "❌ Такой канал/группа не найдены. Проверьте username (@канал) или, что надёжнее, перешлите мне любое сообщение из этого канала.",
        "channel_add_fail_invite_link": "❌ Добавить канал по пригласительной ссылке (https://t.me/+...) нельзя — Telegram этого не поддерживает.\n\nВместо этого: <b>перешлите</b> мне любое сообщение из канала, либо отправьте @username, если канал публичный.",
        "channel_add_fail_wrong_type": "❌ В качестве обязательной подписки можно добавить только канал или группу.",
        "channel_add_fail_no_invite_perm": "❌ У бота нет права создавать пригласительные ссылки в этом канале.\n\nНастройки канала → Администраторы → Бот → включите <b>«Invite Users via Link»</b> и попробуйте снова.",
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
        "queued": "⏳ All slots are busy right now, this will start as soon as one frees up...",
        "caption": "✅ Thanks for using our bot!",
        "detect_music_btn": "🎵 Recognize music",
        "recognizing": "🎧 Recognizing the music...",
        "not_recognized": "😔 Sorry, couldn't recognize the music in this video.",
        "found_song": "🎶 Found: {title} — {artist}\n⏳ Downloading...",
        "download_failed_yt_link": "❌ Could not download the music.\nYou can find it via this link:\n{link}",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Search by artist",
        "btn_youtube_link": "🔍 Open source",
        "lyrics_notice": "📜 I can't display full lyrics due to copyright, but you can find them via the link below:",
        "tiktok_unavailable": "⚠️ Sorry, TikTok services aren't working right now. Please try again later.",
        "link_not_found": "❌ Post not found — it may have been deleted, made private, or the link is wrong.",
        "err_private": "🔐 This post is private or downloads are restricted by the owner.\nTry sharing it from within the app itself, or ask for it to be made public.",
        "err_expired": "⏰ This content has expired (e.g. Snapchat stories only stay up for 24 hours) and is no longer available.",
        "err_stale_cookie": "⚠️ Instagram is not serving this post right now. Please try again a bit later or check the link.",
        "err_youtube_blocked": "⏳ YouTube is temporarily unavailable, please try again later.",
        "err_media_too_large": "⚠️ The file is too big or too long (Telegram bot limit is ~50 MB, ~20 min for tracks). Please pick another option.",
        "err_file_too_big_input": "⚠️ The file you sent is too large (the bot can read files up to 20 MB). Please send a shorter video.",
        "err_busy": "⏳ The bot is under heavy load right now, please try again in a bit.",
        "err_pinterest_video": "🎬 This Pinterest video couldn't be downloaded right now.",
        "err_facebook_parse": "❌ This Facebook video couldn't be downloaded, it may be private or restricted.",
        "unsupported_link": "❌ This link isn't supported. Please send a link from Instagram, YouTube, TikTok, Pinterest, Facebook or Snapchat.",
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
        "channel_add_fail_link": "❌ Couldn't create an invite link for the channel, so it wasn't saved. Check the bot has the \"Invite Users via Link\" permission there, then try again.",
        "channel_add_fail_not_found": "❌ No such channel/group found. Check the username (@channel), or more reliably, forward me any message from that channel.",
        "channel_add_fail_invite_link": "❌ A channel can't be added via an invite link (https://t.me/+...) — Telegram doesn't allow it.\n\nInstead: <b>forward</b> me any message from the channel, or send @username if it's public.",
        "channel_add_fail_wrong_type": "❌ Only a channel or a group can be added as a mandatory subscription.",
        "channel_add_fail_no_invite_perm": "❌ The bot doesn't have permission to create invite links in that channel.\n\nChannel settings → Administrators → the bot → enable <b>\"Invite Users via Link\"</b>, then try again.",
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
    pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=5,  # fail fast instead of hanging a whole request if the DB is slow/unreachable
    )
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


async def clear_join_request(chat_id: int, user_id: int):
    """Drop a logged join request.

    Needed because a pending request counts as satisfying the mandatory
    subscription. If the user later leaves the channel (or an admin removes
    them), the stale row would otherwise keep them marked as subscribed
    forever, letting them use the bot without being in the channel at all.
    Clearing it means they have to request again.
    """
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM join_requests WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
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


_MAIN_EVENT_LOOP: asyncio.AbstractEventLoop | None = None
_admin_alert_last: dict[str, float] = {}
_ADMIN_ALERT_COOLDOWN = int(os.getenv("ADMIN_ALERT_COOLDOWN", "3600"))  # 1 hour per alert key


async def _send_admin_alert_async(text: str) -> None:
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as e:
            log.warning("could not deliver admin alert to %s: %s", admin_id, _short(e))


def notify_admins(key: str, text: str, cooldown: int | None = None) -> None:
    """Fire-and-forget admin alert, safe to call from a worker THREAD (yt-dlp
    runs in an executor) or from async code. Throttled per `key` so a burst
    of identical failures (e.g. every Instagram request for an hour) sends
    ONE Telegram message, not one per request. Silently does nothing if no
    ADMIN_IDS are configured or the event loop isn't up yet (e.g. during
    startup diagnostics) - the log line is always written by the caller too.
    """
    if not ADMIN_IDS:
        return
    now = time.time()
    last = _admin_alert_last.get(key, 0.0)
    if now - last < (cooldown if cooldown is not None else _ADMIN_ALERT_COOLDOWN):
        return
    _admin_alert_last[key] = now
    if _MAIN_EVENT_LOOP is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(_send_admin_alert_async(text), _MAIN_EVENT_LOOP)
    except Exception as e:
        log.warning("could not schedule admin alert: %s", e)



router = Router()
dp.include_router(router)


async def _safe_cb_answer(call: CallbackQuery, *args, **kwargs) -> None:
    """call.answer() that never raises.

    A Telegram callback query is only answerable for a short window (~15s).
    Any handler that does real work first - a DB read, a message edit, or
    worst of all waiting on a HeavyJobSlot queue - can easily blow past that,
    and then answer() raises "query is too old and response timeout expired
    or query ID is invalid". That exception used to propagate all the way up
    and get logged as an unhandled error, even though it is completely
    harmless: the user already got their result, only the little spinner
    acknowledgement was late.

    Swallowing it here keeps the logs honest about real failures.
    """
    try:
        await call.answer(*args, **kwargs)
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            log.debug("callback answer arrived too late (harmless): %s", e)
            return
        raise


@dp.errors()
async def handle_dispatcher_errors(event) -> bool:
    """Global safety net for the whole dispatcher. Telegram's 429
    ("Too Many Requests") comes back as TelegramRetryAfter with the exact
    number of seconds it wants us to wait - respecting that (instead of
    hammering the API again immediately) is what keeps a burst of activity
    from getting the whole bot rate-limited/blocked. Anything else gets
    logged instead of silently swallowed, so a bug in one handler can't take
    the whole polling loop down without at least leaving a trace."""
    exc = getattr(event, "exception", None)
    if isinstance(exc, TelegramRetryAfter):
        log.warning("TELEGRAM_RATE_LIMITED: sleeping %.1fs as instructed by Telegram", exc.retry_after)
        await asyncio.sleep(exc.retry_after)
        return True
    log.exception("Unhandled exception while processing an update: %s", exc)
    return True


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
    await _safe_cb_answer(call)


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
    """Work out which chat the admin means, from any of the usual inputs.

    Returns (chat, error_key). Exactly one of the two is non-None, so the
    caller can tell the admin WHY it failed instead of one vague message -
    the old version swallowed every failure into a single "couldn't add"
    which made real problems (wrong username vs bot not in the chat vs a
    private-invite link the API simply cannot resolve) impossible to tell
    apart.

    Accepted inputs:
      - a forwarded message from the channel/group  (most reliable)
      - @username  /  username
      - https://t.me/username  /  t.me/username
      - a numeric chat id like -1001234567890
      - a private invite link (https://t.me/+XXXX or /joinchat/XXXX) is
        explicitly rejected with its own message, because Telegram's API
        cannot look a chat up from an invite hash - admins try this
        constantly, so it gets a real explanation instead of "failed".
    """
    if message.forward_from_chat:
        return message.forward_from_chat, None

    text = (message.text or "").strip()
    if not text:
        return None, "channel_add_fail"

    # strip a t.me/telegram.me prefix if present
    cleaned = text
    for prefix in ("https://", "http://"):
        if cleaned.lower().startswith(prefix):
            cleaned = cleaned[len(prefix):]
    for host in ("t.me/", "telegram.me/", "telegram.dog/"):
        if cleaned.lower().startswith(host):
            cleaned = cleaned[len(host):]
            break

    # private invite links can't be resolved via the Bot API at all
    if cleaned.startswith("+") or cleaned.lower().startswith("joinchat/"):
        return None, "channel_add_fail_invite_link"

    cleaned = cleaned.split("?")[0].rstrip("/")
    if not cleaned:
        return None, "channel_add_fail"

    try:
        if cleaned.lstrip("-").isdigit():
            chat = await bot.get_chat(int(cleaned))
        else:
            chat = await bot.get_chat(cleaned if cleaned.startswith("@") else f"@{cleaned}")
        return chat, None
    except Exception as e:
        msg = str(e).lower()
        log.info("MANDATORY_CHANNEL_RESOLVE_FAILED: input=%r error=%s", text, e)
        if "chat not found" in msg:
            return None, "channel_add_fail_not_found"
        return None, "channel_add_fail"


async def _refresh_invite_link(chat_id: int, existing_link: str | None) -> tuple[str | None, Exception | None]:
    """Revoke any previously-issued link for this chat, then mint a brand
    new one. Returns (new_link, last_error).

    A fresh link is created EVERY time a channel is added - including when
    re-adding a channel that was added before - so a link that leaked while
    the channel was previously configured can't keep working. Revoking is
    best-effort: an already-invalid/expired old link is not a failure, it
    just means there's nothing left to revoke.
    """
    if existing_link and "t.me/" in existing_link and ("+" in existing_link or "joinchat" in existing_link):
        try:
            await bot.revoke_chat_invite_link(chat_id, existing_link)
            log.info("revoked previous invite link for chat %s", chat_id)
        except Exception as e:
            log.info("old invite link for chat %s could not be revoked (already invalid?): %s", chat_id, e)

    last_err = None
    for attempt in range(3):
        try:
            link_obj = await bot.create_chat_invite_link(
                chat_id,
                name=f"Majburiy obuna {int(time.time())}",
                creates_join_request=True,
            )
            return link_obj.invite_link, None
        except TelegramRetryAfter as e:
            last_err = e
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            last_err = e
            if attempt < 2:
                await asyncio.sleep(1 + attempt)
    return None, last_err


@router.message(AdminStates.waiting_channel)
async def do_add_channel(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()

    try:
        chat, err_key = await _resolve_chat(message)
        if not chat:
            await message.answer(t(lang, err_key or "channel_add_fail"), reply_markup=admin_reply_kb(lang))
            return

        # Only channels/groups can be used as a mandatory subscription -
        # pointing this at a private chat or at the bot itself would create
        # a requirement that can never be satisfied.
        if chat.type not in (ChatType.CHANNEL, ChatType.GROUP, ChatType.SUPERGROUP):
            await message.answer(t(lang, "channel_add_fail_wrong_type"), reply_markup=admin_reply_kb(lang))
            return

        # Check the bot's own membership/permissions there.
        try:
            me_member = await bot.get_chat_member(chat.id, bot.id)
        except Exception as e:
            log.info("MANDATORY_CHANNEL_MEMBER_CHECK_FAILED: chat=%s error=%s", chat.id, e)
            await message.answer(t(lang, "channel_add_fail_not_admin"), reply_markup=admin_reply_kb(lang))
            return

        if me_member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.answer(t(lang, "channel_add_fail_not_admin"), reply_markup=admin_reply_kb(lang))
            return

        is_private = not chat.username
        invite_link = None

        if is_private:
            # Creating an invite link needs an explicit permission that is
            # OFF by default even for admins. Checking it up front turns a
            # confusing API error into a precise instruction.
            can_invite = getattr(me_member, "can_invite_users", None)
            if me_member.status == ChatMemberStatus.ADMINISTRATOR and can_invite is False:
                await message.answer(t(lang, "channel_add_fail_no_invite_perm"), reply_markup=admin_reply_kb(lang))
                return

            existing = await get_channel_by_chat_id(chat.id)
            invite_link, link_err = await _refresh_invite_link(
                chat.id, existing["invite_link"] if existing else None
            )
            if not invite_link:
                # Never save a private channel without a working link: that
                # would be a requirement no user could ever satisfy, silently
                # locking everyone out of the whole bot.
                log.error("MANDATORY_CHANNEL_LINK_FAILED: chat=%s error=%s", chat.id, link_err)
                await message.answer(t(lang, "channel_add_fail_link"), reply_markup=admin_reply_kb(lang))
                return
        else:
            invite_link = f"https://t.me/{chat.username}"

        title = chat.title or chat.username or str(chat.id)
        await add_channel(chat.id, title, chat.username, is_private, invite_link)
        # A newly (re-)added channel gets a clean slate in the broken-channel
        # cache, so a previous outage doesn't keep it excluded from checks.
        _BROKEN_MANDATORY_CHANNELS.pop(chat.id, None)
        log.info("MANDATORY_CHANNEL_ADDED: %s (chat_id=%s, private=%s)", title, chat.id, is_private)
        await message.answer(
            t(lang, "channel_added", title=title),
            reply_markup=admin_reply_kb(lang),
        )
    except Exception as e:
        log.exception("MANDATORY_CHANNEL_ADD_FAILED: unexpected error: %s", e)
        await message.answer(t(lang, "channel_add_fail"), reply_markup=admin_reply_kb(lang))


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
        await _safe_cb_answer(call)
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
    await _safe_cb_answer(call, t(lang, "channel_removed"))



# ============================================================
# HANDLER: join requests for private mandatory channels
#
# IMPORTANT: this only LOGS the request. It never calls
# bot.approve_chat_join_request() or decline_chat_join_request() - whether
# to actually let someone in stays entirely the channel admin's decision.
#
# The logged request is what lets get_unsubscribed_channels() treat the user
# as having satisfied a private channel while approval is still pending:
# tapping a join-request link is the most a user can do on their own, so
# making them wait on a human admin before the bot works would lock them out
# for no reason. If they later leave the channel, on_chat_member_update()
# clears the row again.
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
        # They left (or were removed). Any join request we logged for them is
        # now stale - without clearing it, that row would keep counting as
        # "subscribed" and let them keep using the bot from outside the
        # channel. Clearing forces a fresh request.
        await clear_join_request(update.chat.id, user_id)


# ============================================================
# MANDATORY SUBSCRIPTION CHECK
# ============================================================
# If the bot loses admin rights in a mandatory channel, or the channel gets
# deleted, bot.get_chat_member() starts failing for EVERY user - which,
# without this safeguard, would silently turn into "nobody can use the bot
# at all" (every user permanently stuck on a channel requirement nobody can
# satisfy). Instead, a channel confirmed broken this way is excluded from
# the requirement for _BROKEN_CHANNEL_RECHECK_SECONDS, logged loudly once,
# and automatically re-tried afterwards (self-heals once an admin fixes the
# bot's permissions there, with no restart needed).
_BROKEN_MANDATORY_CHANNELS: dict[int, float] = {}  # chat_id -> time.time() when marked broken
_BROKEN_CHANNEL_RECHECK_SECONDS = 300
_BROKEN_CHANNEL_ERROR_PATTERNS = (
    "chat not found",
    "kicked",
    "bot is not a member",
    "have no rights",
    "forbidden",
    "channel_private",
    "chat_admin_required",
)


async def get_unsubscribed_channels(user_id: int):
    """
    Returns the mandatory channels this user still has to act on.

    A user counts as satisfying a channel when EITHER:
      - they are an actual member (status member/administrator/creator), or
      - the channel is private and they have sent a join request to it.

    That second case matters: private channels are added with
    `creates_join_request=True` links, so a user who taps the link can only
    ever reach "pending approval" until a human admin approves them. Gating
    the bot on approval would mean the user is told "you haven't subscribed"
    even though they did everything they could, and stays locked out until
    an admin happens to look - which is exactly the complaint here. Sending
    the request is the most the user can do, so that's what the bot asks of
    them; whether to actually approve stays entirely up to the admin.
    """
    channels = await list_channels()
    missing = []
    now = time.time()
    for c in channels:
        broken_at = _BROKEN_MANDATORY_CHANNELS.get(c["chat_id"])
        if broken_at is not None and (now - broken_at) < _BROKEN_CHANNEL_RECHECK_SECONDS:
            continue  # confirmed broken recently - don't block every user on it, skip silently
        subscribed = False
        try:
            member = await bot.get_chat_member(c["chat_id"], user_id)
            if member.status in (
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ):
                subscribed = True
            _BROKEN_MANDATORY_CHANNELS.pop(c["chat_id"], None)  # confirmed reachable again
        except Exception as e:
            msg = str(e).lower()
            if any(p in msg for p in _BROKEN_CHANNEL_ERROR_PATTERNS):
                if c["chat_id"] not in _BROKEN_MANDATORY_CHANNELS:
                    log.error(
                        "MANDATORY_CHANNEL_BROKEN: bot can no longer access channel '%s' (chat_id=%s): %s "
                        "- excluding it from the mandatory-subscription check for %ds so it doesn't block "
                        "every user. Fix the bot's admin rights there (or remove/re-add the channel).",
                        c["title"], c["chat_id"], e, _BROKEN_CHANNEL_RECHECK_SECONDS,
                    )
                _BROKEN_MANDATORY_CHANNELS[c["chat_id"]] = now
                continue
            subscribed = False

        # Not a member (yet). For a private channel, a pending join request
        # counts - see the docstring. Telegram gives the bot no API to query
        # pending requests, so this relies on the chat_join_request update
        # we logged in on_join_request().
        if not subscribed and c["is_private"]:
            try:
                if await has_join_request(c["chat_id"], user_id):
                    subscribed = True
            except Exception as e:
                log.warning("join-request lookup failed for chat=%s user=%s: %s", c["chat_id"], user_id, e)

        if not subscribed:
            missing.append(c)
    return missing


def subscribe_kb(lang: str, channels) -> InlineKeyboardMarkup:
    rows = []
    for c in channels:
        url = c["invite_link"] or (f"https://t.me/{c['username']}" if c["username"] else None)
        if url:
            # Telegram rejects the whole keyboard if any button text is empty,
            # and silently truncates very long ones - guard both.
            label = (c["title"] or "Kanal").strip()[:40] or "Kanal"
            rows.append([InlineKeyboardButton(text=f"➕ {label}", url=url)])
        else:
            # A channel with no usable link can't be joined by the user, so
            # showing it as a requirement would trap them with no way out.
            # do_add_channel now refuses to save such a row, but an older row
            # from before that fix could still exist in the database.
            log.error(
                "MANDATORY_CHANNEL_NO_LINK: channel '%s' (chat_id=%s) has no invite link or username "
                "- users cannot join it. Remove and re-add it from the admin panel.",
                c["title"], c["chat_id"],
            )
    rows.append([InlineKeyboardButton(text=t(lang, "check_sub_btn"), callback_data="checksub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "checksub")
async def cb_check_sub(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    try:
        missing = await get_unsubscribed_channels(call.from_user.id)
    except Exception as e:
        log.error("MANDATORY_SUB_CHECK_FAILED: user=%s error=%s", call.from_user.id, e)
        await _safe_cb_answer(call, t(lang, "error"), show_alert=True)
        return

    if missing:
        await _safe_cb_answer(call, t(lang, "still_not_subscribed"), show_alert=True)
        return

    try:
        await call.message.edit_text(t(lang, "now_subscribed"))
    except TelegramBadRequest as e:
        # "message is not modified" happens when the user taps the button
        # twice - harmless, the text is already what we want it to be.
        if "message is not modified" not in str(e).lower():
            log.info("could not edit subscription message: %s", e)
    await _safe_cb_answer(call)


# ============================================================
# DOWNLOAD HELPERS
# ============================================================
def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


# ============================================================
# YT-DLP SHARED HELPERS
#
# Added after analysing the production logs:
#   * _YDL()            - one place that silences yt-dlp's progress bars and
#                         raw stderr. They were ~69% of all log bytes on
#                         Railway (which only keeps the last ~1000 lines), so
#                         the real errors scrolled out of view within hours.
#   * JS runtime        - current yt-dlp needs Deno (or Node) to solve
#                         YouTube's JS challenges; without it many formats are
#                         simply missing ("Requested format is not available").
#   * circuit breaker   - when YouTube fails for everything, stop paying
#                         ~13 s per request to rediscover that.
#   * size/duration caps- a 77 MB / 404-fragment track was downloaded for
#                         2m12s and then rejected by Telegram (50 MB limit).
# ============================================================
MAX_TRACK_SECONDS = int(os.getenv("MAX_TRACK_SECONDS", "1200"))  # 20 min
# Bot API limits: 50 MB for uploads, 20 MB for getFile downloads.
TELEGRAM_UPLOAD_LIMIT_BYTES = 49 * 1024 * 1024
TELEGRAM_GETFILE_LIMIT_BYTES = 20 * 1024 * 1024


class MediaTooLargeError(RuntimeError):
    """The media is too long / too big to be sent through the Bot API."""


def _ensure_within_upload_limit(path: str) -> None:
    """Raise MediaTooLargeError BEFORE we try to upload something Telegram
    will reject with 'Request Entity Too Large'."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    if size > TELEGRAM_UPLOAD_LIMIT_BYTES:
        raise MediaTooLargeError(
            f"file is {size / 1048576:.1f} MB, Telegram bot upload limit is 50 MB"
        )


def _short(value, limit: int = 220) -> str:
    """Single-line, length-capped text for log messages."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _YDLLogger:
    """yt-dlp logger: drops progress/debug/info/error noise (the exception a
    caller gets already carries the error text and the bot logs it once) but
    keeps each *distinct* yt-dlp warning once - e.g. 'No supported JavaScript
    runtime could be found' - which is exactly the kind of hint that was
    invisible before."""

    _seen: set = set()

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def error(self, msg):
        pass

    def warning(self, msg):
        text = str(msg)
        key = re.sub(r"(\[[^\]]+\])\s+[^\s:\]]+:", r"\1 <id>:", text)[:120]
        if key in self._seen or len(self._seen) > 200:
            return
        self._seen.add(key)
        log.warning("yt-dlp: %s", _short(text, 400))


_YDL_LOGGER = _YDLLogger()

_JS_RUNTIME_OPTS: dict | None = None


def _js_runtime_opts() -> dict:
    """yt-dlp enables only `deno` by default. If Deno isn't installed but Node
    (or Bun) is, opt in explicitly. Returns {} when nothing needs to be set
    (Deno present, or no runtime at all - startup logs a warning for that)."""
    global _JS_RUNTIME_OPTS
    if _JS_RUNTIME_OPTS is None:
        opts: dict = {}
        if not shutil.which("deno"):
            for name in ("node", "bun"):
                path = shutil.which(name)
                if path:
                    opts = {name: {"path": path}}
                    break
        _JS_RUNTIME_OPTS = opts
    return _JS_RUNTIME_OPTS


def _YDL(opts: dict):
    """Factory for every yt_dlp.YoutubeDL in this file (single place to keep
    logging quiet and the JS runtime configured)."""
    opts.setdefault("noprogress", True)
    opts.setdefault("logger", _YDL_LOGGER)
    opts["no_warnings"] = False  # let warnings reach _YDLLogger (deduplicated)
    js = _js_runtime_opts()
    if js:
        opts.setdefault("js_runtimes", js)
    return yt_dlp.YoutubeDL(opts)


def _duration_match_filter():
    """yt-dlp match_filter: skip live streams and anything longer than
    MAX_TRACK_SECONDS (unknown duration passes). A rejected video makes
    extract_info() return None."""
    from yt_dlp.utils import match_filter_func

    return match_filter_func(f"duration <=? {MAX_TRACK_SECONDS} & !is_live")


class _FailureBreaker:
    """Tiny thread-safe circuit breaker (yt-dlp runs in executor threads).

    Opens after `threshold` failures on at least `min_distinct` DIFFERENT keys
    inside `window` seconds (so one deleted video retried by three users does
    not trip it). While open, calls fail fast; one probe is let through every
    `probe_interval` seconds, and any success closes the breaker again."""

    def __init__(self, name: str, threshold: int = 3, min_distinct: int = 2,
                 window: int = 600, cooldown: int = 300, probe_interval: int = 60):
        self.name = name
        self.threshold = threshold
        self.min_distinct = min_distinct
        self.window = window
        self.cooldown = cooldown
        self.probe_interval = probe_interval
        self._fails: list[tuple[float, str]] = []
        self._open_until = 0.0
        self._next_probe = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        return time.monotonic() < self._open_until

    def allow(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if now >= self._open_until:
                return True
            if now >= self._next_probe:
                self._next_probe = now + self.probe_interval
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._fails.clear()
            self._open_until = 0.0

    def failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._fails = [(t0, k) for t0, k in self._fails if now - t0 <= self.window]
            self._fails.append((now, key))
            if (
                len(self._fails) >= self.threshold
                and len({k for _, k in self._fails}) >= self.min_distinct
            ):
                if now >= self._open_until:
                    log.error(
                        "%s_CIRCUIT_OPEN: %d failures on %d different targets within %ds - "
                        "failing fast for %ds (one probe every %ds)",
                        self.name.upper(), len(self._fails),
                        len({k for _, k in self._fails}), self.window,
                        self.cooldown, self.probe_interval,
                    )
                    notify_admins(
                        f"{self.name}_circuit_open",
                        f"🚨 {self.name.capitalize()} downloads muvaffaqiyatsiz bo'lyapti "
                        f"({len(self._fails)} marta, {len({k for _, k in self._fails})} xil manba).\n"
                        "Sabab ehtimol: JS runtime (deno/node) o'rnatilmagan, cookie eskirgan yoki IP bloklangan.\n"
                        "Loglarda YTDLP_ENV va YOUTUBE_COOKIES_HARMFUL qatorlarini tekshiring.",
                        cooldown=1800,
                    )
                self._open_until = now + self.cooldown
                self._next_probe = now + self.probe_interval


YOUTUBE_BREAKER = _FailureBreaker(
    "youtube",
    threshold=int(os.getenv("YT_BREAKER_THRESHOLD", "3")),
    cooldown=int(os.getenv("YT_BREAKER_COOLDOWN", "300")),
)


def _parse_client_groups(raw: str) -> list[list[str]]:
    """'default,tv,web_safari+mweb' -> [['default'], ['tv'], ['web_safari', 'mweb']]"""
    groups = []
    for part in (raw or "").split(","):
        names = [c.strip() for c in part.split("+") if c.strip()]
        if names:
            groups.append(names)
    return groups


# InnerTube "player_client" groups, tried in order. The old hard-coded list
# (android_testsuite / tv_embedded / android_creator ...) was tuned for a
# 2024-era yt-dlp and produced a deterministic 3x "Error code: 152" +
# 3x "Requested format is not available" on EVERY request. Client names change
# often - verify with `yt-dlp -v <url>` and override via YT_PLAYER_CLIENTS
# (comma = next group, plus = several clients in one group). "default" means
# "whatever this yt-dlp version considers its default clients", which is the
# safest first choice.
PLAYER_CLIENT_FALLBACKS = _parse_client_groups(
    os.getenv("YT_PLAYER_CLIENTS", "default,tv,web_safari,mweb,android_vr")
) or [["default"]]
# Upper bound on how many client groups one request may burn through.
YT_MAX_CLIENT_ATTEMPTS = max(1, int(os.getenv("YT_MAX_CLIENT_ATTEMPTS", "3")))
# After every client failed WITH cookies, try once more WITHOUT them: exported YouTube
# cookies rotate/expire within hours and a stale session can break otherwise-working requests.
YT_COOKIELESS_RETRY = os.getenv("YT_COOKIELESS_RETRY", "true").strip().lower() in ("1", "true", "yes")
_cookieless_hint_logged = False

# Real bot-check / rate-limit signals (this is what the old code *claimed*
# it was seeing; the logs never contained any of these strings).
_BOT_CHECK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "http error 429",
    "too many requests",
    "preconditionfailed",   # InnerTube 412
)
# Errors where another player_client may still succeed.
_RETRYABLE_MARKERS = _BOT_CHECK_MARKERS + (
    "cookies",                            # "use --cookies-from-browser"
    "error code: 152",                    # embedded-player rejection
    "video unavailable",
    "video is unavailable",
    "requested format is not available",  # this client returned no usable formats
    "no video formats found",
    "http error 403",                     # googlevideo URL rejected (missing PO token)
    "page needs to be reloaded",          # seen in prod: every candidate, every request
)

# Answers that are about THIS video, not about YouTube being unreachable.
_DEFINITIVE_MARKERS = (
    "private video", "video is private", "has been removed", "has been terminated",
    "members-only", "members only", "not available in your country",
    "blocked it in your country", "copyright",
)


def _is_definitive_youtube_answer(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(p in msg for p in _DEFINITIVE_MARKERS)


def _is_bot_check_error(exc: Exception) -> bool:
    """True only for genuine bot-check / rate-limit responses."""
    msg = str(exc).lower()
    return any(p in msg for p in _BOT_CHECK_MARKERS)


def _is_retryable_youtube_error(exc: Exception) -> bool:
    """True if trying a different InnerTube client is worthwhile."""
    msg = str(exc).lower()
    return any(p in msg for p in _RETRYABLE_MARKERS)


# Tracks how many times each InnerTube player_client has failed in this
# process's lifetime. Clients that keep failing are tried LAST on later
# requests - pure ordering optimisation.
_YT_CLIENT_FAILURE_COUNTS: dict[str, int] = {}


def _ordered_player_clients() -> list[list[str]]:
    ordered = sorted(PLAYER_CLIENT_FALLBACKS, key=lambda pc: _YT_CLIENT_FAILURE_COUNTS.get(pc[0], 0))
    return ordered[:YT_MAX_CLIENT_ATTEMPTS]


def _pick_youtube_proxy(attempt_index: int) -> str | None:
    """Fully optional proxy selection for YouTube specifically. Priority:
      1. YOUTUBE_PROXY_LIST - comma-separated list, cycled through one per
         attempt (so each retry/client naturally tries a different exit IP).
      2. YOUTUBE_PROXY      - a single proxy URL, used for every attempt.
      3. PROXY_URL          - the bot-wide general proxy (GENERAL_PROXY).
      4. None               - no proxy, today's default behavior.
    Never raises - a malformed/unreachable proxy just means yt-dlp's own
    request fails and the normal client-fallback loop moves on, exactly
    like any other failed attempt (fail-safe, no special handling needed).
    """
    if YOUTUBE_PROXY_LIST:
        return YOUTUBE_PROXY_LIST[attempt_index % len(YOUTUBE_PROXY_LIST)]
    if YOUTUBE_PROXY:
        return YOUTUBE_PROXY
    return GENERAL_PROXY


_ip_block_hint_logged = False
_no_formats_hint_logged = False


def _raise_ytdlp_failure(last_exc, saw_bot_check: bool = False):
    """Raise a tagged error after every client group failed.

    * YOUTUBE_IP_BLOCKED  - only when a REAL bot-check/429 was seen.
    * YOUTUBE_NO_FORMATS  - every client answered, but none returned usable
      formats. That is an environment problem (outdated yt-dlp, missing JS
      runtime / PO token, unavailable video), not proof of an IP block.
    Both are mapped to ERROR_YOUTUBE_BLOCKED for the user by
    classify_download_error()."""
    global _ip_block_hint_logged, _no_formats_hint_logged
    if last_exc is None:
        raise RuntimeError("yt-dlp: all InnerTube client fallbacks exhausted.")
    if not _is_retryable_youtube_error(last_exc):
        raise last_exc
    if saw_bot_check:
        if not _ip_block_hint_logged:
            _ip_block_hint_logged = True
            log.error(
                "YOUTUBE_IP_BLOCKED: YouTube returned a bot-check / rate-limit response for every "
                "player_client. Fix: configure YOUTUBE_PROXY or YOUTUBE_PROXY_LIST (residential "
                "proxy), refresh YOUTUBE_COOKIES, or wait for the block to lift."
            )
        raise RuntimeError(f"YOUTUBE_IP_BLOCKED: all InnerTube clients blocked - {last_exc}") from last_exc
    if not _no_formats_hint_logged:
        _no_formats_hint_logged = True
        log.error(
            "YOUTUBE_NO_FORMATS: no player_client returned downloadable formats and NO bot-check "
            "message was seen, so this is probably not a plain IP block. Check in this order: "
            "(1) yt-dlp is current (pip install -U 'yt-dlp[default]'), (2) a JS runtime (deno or "
            "node) is installed, (3) a PO-token provider (bgutil-ytdlp-pot-provider), "
            "(4) YOUTUBE_COOKIES may be stale/rotated, (5) YT_PLAYER_CLIENTS, (6) a proxy. Run `yt-dlp -v -F <url>` on the server to see "
            "which formats each client really returns."
        )
    raise RuntimeError(
        f"YOUTUBE_NO_FORMATS: no usable formats from any player_client - {last_exc}"
    ) from last_exc


def _yt_with_clients(key: str, outdir, configure, runner, *,
                     use_proxy: bool = False, deadline: float | None = None):
    """Run `runner(ydl_opts)` against YouTube, rotating player_client groups.

    key       - identifies the target (URL / video id) for the circuit breaker
    configure - callback that sets format / postprocessors on the opts dict
    runner    - callback that performs the yt-dlp call and returns its result
    deadline  - time.monotonic() value after which no NEW client is started
    """
    if not YOUTUBE_BREAKER.allow():
        raise RuntimeError(
            "YOUTUBE_CIRCUIT_OPEN: YouTube downloads are paused after repeated failures"
        )
    clients = _ordered_player_clients()
    last_exc = None
    saw_bot_check = False
    for attempt, player_clients in enumerate(clients):
        if attempt > 0 and deadline is not None and time.monotonic() > deadline:
            break
        ydl_opts = _build_ydl_opts_base(outdir, player_clients)
        configure(ydl_opts)
        if use_proxy:
            proxy = _pick_youtube_proxy(attempt)
            if proxy:
                ydl_opts["proxy"] = proxy
        try:
            result = runner(ydl_opts)
        except MediaTooLargeError:
            YOUTUBE_BREAKER.success()  # YouTube answered fine; the track is just too long
            raise
        except Exception as e:
            last_exc = e
            if not _is_retryable_youtube_error(e):
                if _is_definitive_youtube_answer(e):
                    # a clear answer about THIS video (private / removed / geo-blocked):
                    # YouTube itself is reachable, so this is not an outage.
                    YOUTUBE_BREAKER.success()
                else:
                    # unrecognised error: neither proof of health nor of an outage.
                    # Never call success() here - and make new error types visible.
                    log.warning("YouTube unrecognised error (not retried): %s", _short(e))
                raise
            saw_bot_check = saw_bot_check or _is_bot_check_error(e)
            first = player_clients[0]
            _YT_CLIENT_FAILURE_COUNTS[first] = _YT_CLIENT_FAILURE_COUNTS.get(first, 0) + 1
            log.warning(
                "YouTube player_client %s failed (%d/%d): %s",
                player_clients, attempt + 1, len(clients), _short(e),
            )
            continue
        # success: let this client's failure count decay and close the breaker
        first = player_clients[0]
        if first in _YT_CLIENT_FAILURE_COUNTS:
            _YT_CLIENT_FAILURE_COUNTS[first] = max(0, _YT_CLIENT_FAILURE_COUNTS[first] - 1)
        YOUTUBE_BREAKER.success()
        return result
    if (
        last_exc is not None and YT_COOKIELESS_RETRY and COOKIES_FILE and saw_bot_check
        and (deadline is None or time.monotonic() <= deadline)
    ):
        global _cookieless_hint_logged
        ydl_opts = _build_ydl_opts_base(outdir, clients[0])
        ydl_opts.pop("cookiefile", None)
        configure(ydl_opts)
        if use_proxy:
            proxy = _pick_youtube_proxy(0)
            if proxy:
                ydl_opts["proxy"] = proxy
        try:
            result = runner(ydl_opts)
        except MediaTooLargeError:
            YOUTUBE_BREAKER.success()
            raise
        except Exception as e:
            last_exc = e
            saw_bot_check = saw_bot_check or _is_bot_check_error(e)
            log.warning("YouTube retry WITHOUT cookies failed too: %s", _short(e))
        else:
            if not _cookieless_hint_logged:
                _cookieless_hint_logged = True
                log.error(
                    "YOUTUBE_COOKIES_HARMFUL: the request failed with cookies but worked WITHOUT them - "
                    "YOUTUBE_COOKIES is stale/rotated. Export fresh cookies from a private window "
                    "(and close it), or remove YOUTUBE_COOKIES."
                )
            YOUTUBE_BREAKER.success()
            return result
    YOUTUBE_BREAKER.failure(key)
    _raise_ytdlp_failure(last_exc, saw_bot_check)


def _build_ydl_opts_base(outdir, player_clients, cookies_file: str | None = "__default__"):
    """Shared yt-dlp options for all download functions.

    Only `player_client` is passed to the YouTube extractor. The old code also
    sent `skip_webpage` (not an option yt-dlp knows) and `player_skip=
    webpage,configs,js` for android/tv clients; yt-dlp documents that
    skipping those requests "could cause some issues", and it also prevents
    the JS challenge solver from working.

    `cookies_file`: which cookies.txt to attach, if any.
      - omitted (the default) -> COOKIES_FILE (YouTube's), for YouTube callers
      - an explicit path       -> that platform's own cookie file
      - None                   -> no cookies at all (e.g. a deliberate
        cookieless retry, or a platform that doesn't use cookies)
    """
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "extractor_args": {"youtube": {"player_client": list(player_clients)}},
        "http_headers": {"User-Agent": DEFAULT_UA},
        "geo_bypass": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if outdir:
        opts["outtmpl"] = os.path.join(outdir, "%(id)s.%(ext)s")
    if cookies_file == "__default__":
        cookies_file = COOKIES_FILE
    cookie_copy = _private_cookie_copy(cookies_file, outdir)
    if cookie_copy:
        opts["cookiefile"] = cookie_copy
    return opts


def _log_ytdlp_environment() -> None:
    """One startup line with everything needed to diagnose YouTube failures
    (the production logs never showed the yt-dlp version or JS runtime)."""
    try:
        from yt_dlp.version import __version__ as ytdlp_version
    except Exception:
        ytdlp_version = "?"
    runtimes = [n for n in ("deno", "node", "bun", "qjs") if shutil.which(n)]
    has_ejs = importlib.util.find_spec("yt_dlp_ejs") is not None
    log.info(
        "YTDLP_ENV: yt-dlp=%s | JS runtimes on PATH=%s | yt-dlp-ejs=%s | player_clients=%s | "
        "max_attempts=%d | track_limit=%ds",
        ytdlp_version, runtimes or "NONE", has_ejs,
        ["+".join(g) for g in PLAYER_CLIENT_FALLBACKS], YT_MAX_CLIENT_ATTEMPTS, MAX_TRACK_SECONDS,
    )
    if not runtimes:
        log.warning(
            "YTDLP_ENV: no JS runtime (deno/node) found. Current yt-dlp needs one to solve YouTube's "
            "JS challenges; without it most formats are missing and downloads fail with "
            "'Requested format is not available'. Install Deno (or Node >= 20)."
        )
    if not shutil.which("ffprobe"):
        log.warning(
            "YTDLP_ENV: ffprobe not found (imageio-ffmpeg ships only ffmpeg). yt-dlp logs 'Unable to "
            "extract metadata' and audio post-processing is less reliable. Install the system ffmpeg package."
        )
    if not has_ejs:
        log.warning(
            "YTDLP_ENV: yt-dlp-ejs is not installed. Use `pip install -U 'yt-dlp[default]'` "
            "(includes yt-dlp-ejs)."
        )


def _private_cookie_copy(cookies_file: str | None, outdir: str | None) -> str | None:
    """Return a per-download PRIVATE COPY of a cookies.txt file.

    This matters because yt-dlp does not treat `cookiefile` as read-only - it
    writes the (possibly refreshed) cookie jar BACK to that same path when it
    finishes. With HEAVY_JOB_SLOTS=2 that means two yt-dlp instances can be
    writing the one shared /tmp/yt_cookies.txt at the same time, and one ends
    up reading a half-written file. That's exactly the
    "'/tmp/yt_cookies.txt' does not look like a Netscape format cookies file"
    error seen at runtime even though the very same file loaded fine at
    startup - the file was fine, concurrency corrupted it afterwards.

    Giving each download its own copy inside its own temp dir means yt-dlp's
    write-back lands on a throwaway file that gets deleted with the rest of
    the download's tempdir, and the pristine original is never mutated.
    """
    if not cookies_file or not os.path.exists(cookies_file):
        return None
    if not outdir:
        return cookies_file  # metadata-only probes don't run concurrently on one dir
    try:
        dest = os.path.join(outdir, os.path.basename(cookies_file))
        shutil.copyfile(cookies_file, dest)
        return dest
    except Exception as e:
        log.warning("could not make a private cookie copy (%s), falling back to the shared file", e)
        return cookies_file

def _scrape_og_image(url: str, outdir: str, filename_prefix: str = "image"):
    """Last-resort image grab that doesn't depend on yt-dlp at all: fetch the
    page HTML directly and pull the og:image meta tag. Used when yt-dlp's
    extractor can't even build metadata for a post (some Pinterest image
    pins raise 'No video formats found!' during extraction itself, before
    any format selection or thumbnail data is available)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.warning("og:image scrape failed to fetch page %s: %s", url, e)
        return None

    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html)
    if not m:
        # some pages put content before property
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html)
    if not m:
        return None
    img_url = m.group(1).replace("&amp;", "&")

    try:
        req = urllib.request.Request(img_url, headers={"User-Agent": DEFAULT_UA})
        filepath = os.path.join(outdir, f"{filename_prefix}_{uuid.uuid4().hex[:8]}.jpg")
        with urllib.request.urlopen(req, timeout=30) as resp, open(filepath, "wb") as f:
            f.write(resp.read())
    except Exception as e:
        log.warning("og:image scrape failed to download image %s: %s", img_url, e)
        return None
    return filepath, {"id": filename_prefix, "title": filename_prefix, "ext": "jpg"}


def _download_image_fallback(url: str, outdir: str, cookies_file: str | None = None):
    """Some posts (Instagram photo-only posts, some Pinterest image pins)
    have no video formats at all, so yt-dlp's normal video download raises
    'No video formats found!'. This grabs the highest-res display image
    directly instead. Used as a safety net for both platforms.

    Note: for some Pinterest image pins, yt-dlp's own extractor raises this
    same "No video formats found!" error DURING metadata extraction itself
    (even with download=False) - there's no info dict to pull a thumbnail
    from at all in that case. When that happens this falls through to a
    plain HTML og:image scrape instead of propagating the error.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }
    if cookies_file and os.path.exists(cookies_file):
        ydl_opts["cookiefile"] = cookies_file
    try:
        with _YDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if "entries" in info:
            info = info["entries"][0]
        thumbs = info.get("thumbnails") or []
        img_url = None
        if thumbs:
            img_url = max(thumbs, key=lambda th: (th.get("width") or 0) * (th.get("height") or 0)).get("url")
        if not img_url:
            img_url = info.get("thumbnail") or info.get("url")
        if img_url:
            filepath = os.path.join(outdir, f"{info.get('id') or 'photo'}.jpg")
            req = urllib.request.Request(img_url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=30) as resp, open(filepath, "wb") as f:
                f.write(resp.read())
            return filepath, info
    except Exception as e:
        log.info("yt-dlp metadata extraction for image fallback failed (%s), trying og:image scrape instead.", e)

    # yt-dlp couldn't help at all - try a plain HTML scrape as a last resort.
    return _scrape_og_image(url, outdir, filename_prefix="pin" if "pinterest" in url.lower() or "pin.it" in url.lower() else "image")


def _probe_video_stream(input_path: str) -> tuple[str, str]:
    """Returns (vcodec, pix_fmt) by parsing ffmpeg's own -i banner (no
    ffprobe binary is bundled, only imageio-ffmpeg's ffmpeg)."""
    try:
        proc = subprocess.run(
            [FFMPEG_PATH, "-i", input_path], capture_output=True, text=True, timeout=20
        )
    except subprocess.TimeoutExpired:
        return "", ""
    stderr = proc.stderr or ""
    vcodec = pix_fmt = ""
    m = re.search(r"Video:\s*([a-zA-Z0-9_]+)", stderr)
    if m:
        vcodec = m.group(1).lower()
    m = re.search(r"Video:.*?,\s*(yuv[jJ]?\d{3}p(?:10le)?)", stderr)
    if m:
        pix_fmt = m.group(1).lower()
    return vcodec, pix_fmt


def _ffmpeg_normalize_for_ios(input_path: str, outdir: str) -> tuple[str, int, int, int]:
    """Make a downloaded video play correctly on Telegram-iOS.

    Why this is needed: Instagram (and some other platforms) often serve a
    single progressive stream that yt-dlp passes through untouched, and
    that stream can be HEVC or have the moov atom at the end of the file.
    Android/desktop Telegram decode/stream that fine, but iOS's
    VideoToolbox decoder is much stricter about codec/profile compliance
    and needs a front-loaded moov to start progressive playback - when it
    can't, it just shows the first frame while the (separately-decoded,
    more tolerant) AAC audio keeps playing.

    Two paths, in order of preference:
    1. If the video is ALREADY H.264/yuv420p (the common case for
       Instagram), just remux it (-c copy, stream copy) - this only
       rewrites the container to move moov to the front and costs almost
       no time/CPU/memory, with zero quality loss.
    2. Only if the codec itself is incompatible (HEVC/VP9/AV1/etc.) do we
       pay for a real re-encode - and even then at a quality-first CRF
       since this path is now the rare exception, not the common case.

    Returns (output_path, width, height, duration_seconds), parsed from
    ffmpeg's own stderr banner (no ffprobe binary is bundled).
    """
    output_path = os.path.join(outdir, f"ios_{uuid.uuid4().hex[:8]}.mp4")
    vcodec, pix_fmt = _probe_video_stream(input_path)
    is_already_compatible = vcodec in ("h264", "avc1") and pix_fmt.startswith("yuv420")
    size_mb = os.path.getsize(input_path) / (1024 * 1024) if os.path.exists(input_path) else 0
    MAX_REENCODE_MB = 60  # protects against OOM-killing the re-encode on Railway

    if is_already_compatible or size_mb > MAX_REENCODE_MB:
        # Fast path: lossless remux, just fixes the moov position. Also used
        # as the safe fallback for oversized incompatible-codec files -
        # a real re-encode of those risks an OOM kill, so we settle for
        # "faststart fixed, codec left as-is" rather than crashing.
        if not is_already_compatible:
            log.info("skipping re-encode for %.1fMB non-H264 file (OOM risk) - remuxing only", size_mb)
        cmd = [
            FFMPEG_PATH, "-y", "-i", input_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
    else:
        # Slow path: the codec itself isn't iOS-safe, a real re-encode is
        # unavoidable - but keep quality high since this is now the rare case.
        cmd = [
            FFMPEG_PATH, "-y", "-i", input_path,
            # only cap truly oversized video, never touch normal Reels/Stories res
            "-vf", "scale='min(1920,iw)':'-2'",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-profile:v", "high", "-level", "4.1",
            "-pix_fmt", "yuv420p",
            "-x264-params", "rc-lookahead=20:ref=3",
            "-threads", "2",
            "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
            "-movflags", "+faststart",
            output_path,
        ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except subprocess.TimeoutExpired:
        raise RuntimeError("ffmpeg iOS-normalize timed out after 240s")
    stderr = proc.stderr or ""
    if proc.returncode != 0 or not os.path.exists(output_path):
        killed = " (likely OOM-killed by the host - out of memory)" if proc.returncode == -9 else ""
        raise RuntimeError(f"ffmpeg iOS-normalize failed (code {proc.returncode}){killed}: {stderr[-500:]}")

    width = height = duration = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", stderr)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = int(h * 3600 + mnt * 60 + s)
    return output_path, width, height, duration


_instagram_cookie_hint_logged = False


def _is_instagram_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        phrase in msg for phrase in (
            "rate-limit", "rate limit", "please wait a few minutes",
            "http error 429", "too many requests", "login required",
            "restricted video", "check the details and try again",
        )
    )


_instagram_empty_hint_last = 0.0


def _note_instagram_empty_response(exc: Exception, retried_without_cookies: bool) -> None:
    """Instagram returned an EMPTY body (`Expecting value: line 1 column 1`).
    That alone doesn't say WHY - stale cookies, a rate limit, a login wall or
    an IP block all look identical - but if it happens even WITHOUT cookies
    (either because there were none to begin with, or the cookieless retry
    failed the same way), cookies are not the explanation and someone should
    look at it. Logged always; a Telegram alert to ADMIN_IDS is throttled to
    once per hour so a burst of identical failures doesn't spam the chat."""
    if not retried_without_cookies:
        log.warning("INSTAGRAM_EMPTY_RESPONSE (retrying without cookies next): %s", _short(exc, 200))
        return
    log.error(
        "INSTAGRAM_EMPTY_RESPONSE: empty/non-JSON body even WITHOUT cookies - not a stale-cookie "
        "issue. Likely a rate limit, login wall, or this IP being blocked by Instagram."
    )
    notify_admins(
        "instagram_empty_response",
        "🚨 Instagram hamma so'rovda bo'sh javob qaytaryapti (cookie bilan HAM, cookiesiz HAM).\n"
        "Ehtimol: IP bloklangan yoki rate-limit. Cookie yangilash yordam bermaydi.\n"
        "Tavsiya: PROXY_URL sozlang yoki biroz kuting.",
    )


def _execute_ytdlp_download(ydl_opts: dict, url: str, outdir: str):
    """Runs a single yt-dlp download attempt with the given (already fully
    built) options and returns (filepath, info). Shared by every
    platform-specific downloader below purely to avoid re-typing this exact
    extract+rename dance six times - it carries no platform decisions."""
    with _YDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        # merged output is renamed to merge_output_format's extension
        merged = os.path.splitext(filename)[0] + ".mp4"
        if os.path.exists(merged):
            filename = merged
        return filename, info


def _download_youtube(url: str, outdir: str, use_proxy: bool):
    """YouTube is the one platform that needs the InnerTube player_client
    fallback loop. Merges best video+audio. The rotation, circuit breaker and
    error tagging live in _yt_with_clients(); an optional proxy is layered on
    top via YOUTUBE_PROXY / YOUTUBE_PROXY_LIST if configured."""

    def configure(ydl_opts: dict) -> None:
        ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ydl_opts["merge_output_format"] = "mp4"

    def runner(ydl_opts: dict):
        return _execute_ytdlp_download(ydl_opts, url, outdir)

    return _yt_with_clients(url, outdir, configure, runner, use_proxy=use_proxy)


def _download_tiktok(url: str, outdir: str):
    """TikTok: never proxied (its CDN blocks most proxy ranges harder than
    going direct), single attempt, no cookies needed for public videos."""
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    ydl_opts["merge_output_format"] = "mp4"
    return _execute_ytdlp_download(ydl_opts, url, outdir)


def _is_empty_response_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "failed to parse json" in msg or "expecting value" in msg


def _download_instagram(url: str, outdir: str, use_proxy: bool):
    """Instagram: optional login cookies (rate-limit/private-account errors
    otherwise), a photo-only-post image fallback, and an iOS-compatibility
    remux/re-encode pass since it frequently serves a single progressive
    stream that Telegram-iOS can't always play directly.

    On an "empty response" failure (Instagram returned no JSON body - the
    single symptom shared by stale cookies, a rate limit and a login wall,
    see _note_instagram_empty_response) this retries once WITHOUT cookies:
    if cookies are the problem that alone fixes it; if not, both attempts
    fail the same way and that is a much stronger signal to alert on than
    either failure alone.
    """

    def attempt(cookies_file: str | None):
        opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=cookies_file)
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"
        if use_proxy and GENERAL_PROXY:
            opts["proxy"] = GENERAL_PROXY
        return _execute_ytdlp_download(opts, url, outdir)

    try:
        filename, info = attempt(INSTAGRAM_COOKIES_FILE)
    except Exception as e:
        if "no video formats found" in str(e).lower():
            result = _download_image_fallback(url, outdir, INSTAGRAM_COOKIES_FILE)
            if result:
                return result
            raise
        if not _is_empty_response_error(e):
            _handle_instagram_hard_failure(e)
            raise
        if not INSTAGRAM_COOKIES_FILE:
            # no cookies configured - this WAS already the (only) cookieless
            # attempt, so there is nothing left to escalate to.
            _note_instagram_empty_response(e, retried_without_cookies=True)
            raise
        log.info("Instagram empty response with cookies, retrying without cookies: %s", url)
        try:
            filename, info = attempt(None)
        except Exception as e2:
            if not _is_empty_response_error(e2):
                _handle_instagram_hard_failure(e2)
                raise e2
            _note_instagram_empty_response(e2, retried_without_cookies=True)
            raise e2
        else:
            log.warning(
                "INSTAGRAM_COOKIES_HARMFUL: this request failed WITH cookies but worked "
                "WITHOUT them - INSTAGRAM_COOKIES is likely stale. Export fresh cookies from "
                "a logged-in session, or remove INSTAGRAM_COOKIES."
            )

    ext = os.path.splitext(filename)[1].lower()
    if SKIP_IOS_NORMALIZE:
        return filename, info
    if ext in (".mp4", ".mov", ".mkv", ".webm"):
        try:
            norm_path, w, h, dur = _ffmpeg_normalize_for_ios(filename, outdir)
            os.remove(filename)
            filename = norm_path
            info["width"], info["height"], info["duration"] = w, h, dur
        except Exception as e:
            log.warning("iOS normalize failed, sending original file instead: %s", e)
    return filename, info


def _handle_instagram_hard_failure(exc: Exception) -> None:
    """Errors other than the empty-response pattern (rate-limit wording,
    login walls, etc.) - still worth one throttled admin alert."""
    if _is_instagram_rate_limit_error(exc):
        notify_admins(
            "instagram_rate_limit",
            "⚠️ Instagram: rate-limit/login-wall javoblari kelyapti.\n"
            f"So'nggi xato: {_short(exc, 300)}\n"
            "Cookie yangilash yoki PROXY_URL sozlash kerak bo'lishi mumkin.",
        )


def _download_pinterest(url: str, outdir: str, use_proxy: bool):
    """Pinterest: pins can be a video OR a plain image. A lightweight probe
    (metadata only, no download) first checks whether this pin actually has
    a real video stream:
      - image pin (no video formats) -> og:image scrape fallback is fine,
        that IS the content.
      - video pin whose format genuinely can't be fetched -> fallback is
        NOT used, since silently handing back a single static thumbnail
        frame when the user asked for a video would be misleading. A clear
        "couldn't download this video" error is raised instead.
      - probe itself failed (type unknown) -> same as video pin, err on
        the side of not silently substituting a static image.
    """
    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "http_headers": {"User-Agent": DEFAULT_UA},
    }
    if use_proxy and GENERAL_PROXY:
        probe_opts["proxy"] = GENERAL_PROXY
    is_video_pin = None  # None = couldn't determine
    try:
        with _YDL(probe_opts) as ydl:
            probe_info = ydl.extract_info(url, download=False)
        if "entries" in probe_info:
            probe_info = probe_info["entries"][0]
        formats = probe_info.get("formats") or []
        is_video_pin = any(f.get("vcodec") not in (None, "none") for f in formats)
    except Exception as e:
        log.info("PINTEREST_PROBE_FAILED: could not pre-classify pin type for %s (%s)", url, e)

    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = "best[ext=mp4]/best[ext=webm]/best[ext=jpg]/best[ext=png]/best"
    ydl_opts["merge_output_format"] = "mp4"
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY
    try:
        return _execute_ytdlp_download(ydl_opts, url, outdir)
    except Exception as e:
        msg = str(e).lower()
        if "no video formats found" in msg or "requested format is not available" in msg:
            if is_video_pin is False:
                # confirmed image pin - the image IS the real content
                result = _download_image_fallback(url, outdir, None)
                if result:
                    return result
            else:
                log.warning(
                    "PINTEREST_VIDEO_FORMAT_NOT_FOUND: %s (pin_type=%s): %s",
                    url, "video" if is_video_pin else "unknown", e,
                )
                raise RuntimeError(f"PINTEREST_VIDEO_UNAVAILABLE: {e}") from e
        raise


def _download_facebook(url: str, outdir: str, use_proxy: bool):
    """Facebook: most videos/reels are login-walled for logged-out
    (datacenter IP) requests, so cookies matter a lot more here than for
    other platforms. Prefers a single progressive format since Facebook's
    extractor often doesn't expose separate video+audio streams to merge."""
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=FACEBOOK_COOKIES_FILE)
    ydl_opts["format"] = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
    ydl_opts["merge_output_format"] = "mp4"
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY
    try:
        return _execute_ytdlp_download(ydl_opts, url, outdir)
    except Exception as e:
        if "cannot parse data" in str(e).lower():
            log.warning("FACEBOOK_PARSE_ERROR: page structure unreadable or login-walled for %s: %s", url, e)
        elif not FACEBOOK_COOKIES_FILE:
            log.info(
                "Facebook download failed and no FACEBOOK_COOKIES is set - many Facebook "
                "videos/reels are login-walled and need cookies from a logged-in account to work."
            )
        raise


def _download_snapchat(url: str, outdir: str, use_proxy: bool):
    """Snapchat: public stories/spotlights only, single progressive format,
    and a shorter socket timeout since a story that has expired tends to
    hang rather than fail fast otherwise."""
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
    ydl_opts["merge_output_format"] = "mp4"
    ydl_opts["socket_timeout"] = 20
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY
    return _execute_ytdlp_download(ydl_opts, url, outdir)


def _run_ytdlp_download(url: str, outdir: str, use_proxy: bool, platform: str | None = None):
    """Router: dispatches to the platform-specific downloader above. Each
    one is fully self-contained (its own format selector, retry strategy,
    cookies, and fallbacks) rather than sharing one generic code path, so
    tuning one platform can never accidentally break another."""
    if platform == "youtube":
        return _download_youtube(url, outdir, use_proxy)
    if platform == "tiktok":
        return _download_tiktok(url, outdir)
    if platform == "instagram":
        return _download_instagram(url, outdir, use_proxy)
    if platform == "pinterest":
        return _download_pinterest(url, outdir, use_proxy)
    if platform == "facebook":
        return _download_facebook(url, outdir, use_proxy)
    if platform == "snapchat":
        return _download_snapchat(url, outdir, use_proxy)
    # Unknown/unlisted platform - best-effort generic attempt.
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    ydl_opts["merge_output_format"] = "mp4"
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY
    return _execute_ytdlp_download(ydl_opts, url, outdir)


def classify_download_error(exc: Exception) -> str:
    """Maps a yt-dlp exception to one of a small set of error codes so the
    UI can show a specific, actionable message instead of a generic one."""
    msg = str(exc).lower()
    if "memory_watermark_exceeded" in msg:
        return "ERROR_BUSY"
    # Tagged errors raised deliberately elsewhere in this file - check
    # these first since they're unambiguous (see _raise_ytdlp_failure,
    # _download_pinterest).
    if "youtube_ip_blocked" in msg or "youtube_no_formats" in msg or "youtube_circuit_open" in msg:
        return "ERROR_YOUTUBE_BLOCKED"
    if "pinterest_video_unavailable" in msg:
        return "ERROR_PINTEREST_VIDEO"
    if "cannot parse data" in msg:
        return "ERROR_FACEBOOK_PARSE"
    # Instagram (and sometimes other cookie-gated platforms) returns an
    # empty/HTML "please log in" response instead of JSON once its cookies
    # go stale, which yt-dlp then fails to parse as JSON. Left unhandled
    # this surfaces as a raw JSONDecodeError instead of a useful message.
    if "expecting value" in msg or "jsondecodeerror" in msg or "failed to parse json" in msg:
        return "ERROR_STALE_COOKIE"
    if "expired" in msg or "no longer available" in msg or "24 hours" in msg:
        return "ERROR_EXPIRED"
    if (
        "private" in msg or "login" in msg or "restricted" in msg or "log in" in msg
        or "--cookies" in msg or "content is unreachable" in msg  # e.g. Instagram stories
    ):
        return "ERROR_PRIVATE"
    # NB: match "http error 404", not a bare "404" - a bare "404" also matches
    # random video IDs that happen to contain those digits.
    if "not found" in msg or "http error 404" in msg or "deleted" in msg:
        return "ERROR_DELETED"
    # After exhausting all InnerTube client fallbacks, "video is unavailable" /
    # "requested format is not available" almost always means the video is
    # genuinely gone/region-blocked/age-restricted for this server, not a
    # bug in the format selector - treat it like a deleted/unavailable post
    # rather than a generic error.
    if "video unavailable" in msg or "requested format is not available" in msg or "no video formats found" in msg:
        return "ERROR_DELETED"
    return "ERROR_UNKNOWN"


async def download_media(url: str, outdir: str, platform: str):
    loop = asyncio.get_running_loop()
    use_proxy = platform != "tiktok"  # TikTok is never proxied, per requirements
    return await loop.run_in_executor(None, _run_ytdlp_download, url, outdir, use_proxy, platform)


# ============================================================
# SONG SEARCH: SoundCloud (primary) + VK Music (fallback)
#
# YouTube is only a guarded middle step here (circuit breaker + deadline):
# from datacenter IPs it can fail for every request.
# SoundCloud has no such bot-check for public tracks. VK Music is used only
# when a track is missing/copyright-blocked on SoundCloud, and requires a
# real VK account (VK_LOGIN + VK_PASSWORD env vars) to authorize search.
# ============================================================

# --- SoundCloud DRM blocklist ----------------------------------------------
# The logs showed the same DRM-protected (Go+) track failing twice within
# 1.3 s: the user picked it (DRM error), then the "fall back to search" step
# searched for the same artist+title, got the SAME track back and failed
# again. Remember DRM tracks so they are skipped instead of re-tried, and so
# they are not offered in search lists in the first place.
_SC_DRM_BLOCKLIST: dict[str, float] = {}
_SC_DRM_TTL_SECONDS = 24 * 3600
_SC_DRM_MAX_ENTRIES = 2000
_SC_ID_RE = re.compile(r"\[soundcloud[^\]]*\]\s+(\d+)")


def _is_drm_error(exc: Exception) -> bool:
    return "drm protected" in str(exc).lower()


def _sc_entry_keys(entry: dict) -> list[str]:
    keys = []
    for field in ("id", "url", "webpage_url"):
        value = entry.get(field)
        if value:
            keys.append(str(value))
    return keys


def _sc_ids_from_exc(exc: Exception) -> list[str]:
    return _SC_ID_RE.findall(str(exc))


def _mark_sc_drm(*keys: str) -> None:
    now = time.time()
    for key in keys:
        if key:
            _SC_DRM_BLOCKLIST[str(key)] = now
    while len(_SC_DRM_BLOCKLIST) > _SC_DRM_MAX_ENTRIES:
        _SC_DRM_BLOCKLIST.pop(next(iter(_SC_DRM_BLOCKLIST)), None)


def _is_sc_drm_blocked(*keys: str) -> bool:
    now = time.time()
    for key in keys:
        ts = _SC_DRM_BLOCKLIST.get(str(key))
        if ts is None:
            continue
        if now - ts > _SC_DRM_TTL_SECONDS:
            _SC_DRM_BLOCKLIST.pop(str(key), None)
            continue
        return True
    return False


def _run_soundcloud_search_download(query: str, outdir: str) -> tuple[str, str] | None:
    """Search SoundCloud and download the first playable result, skipping
    DRM-protected (Go+) tracks it can't fetch (remembered in _SC_DRM_BLOCKLIST)
    and tracks longer than MAX_TRACK_SECONDS. Returns (mp3_path,
    webpage_url) or None if nothing playable was found."""
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
        "match_filter": _duration_match_filter(),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    _sc_cookies = _private_cookie_copy(SOUNDCLOUD_COOKIES_FILE, outdir)
    if _sc_cookies:
        ydl_opts["cookiefile"] = _sc_cookies

    # Fetch several candidates up front (flat, no download) so a DRM-blocked
    # top result doesn't kill the whole search - just move to the next one.
    flat_opts = dict(ydl_opts)
    flat_opts["extract_flat"] = "in_playlist"
    flat_opts.pop("postprocessors", None)
    flat_opts.pop("match_filter", None)
    try:
        with _YDL(flat_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query}", download=False)
            candidates = [e for e in ((info or {}).get("entries") or []) if e and e.get("url")]
    except Exception as e:
        log.warning("SoundCloud search failed for '%s': %s", query, _short(e))
        return None

    skipped_drm = 0
    for cand in candidates:
        keys = _sc_entry_keys(cand)
        if _is_sc_drm_blocked(*keys):
            skipped_drm += 1
            continue
        duration = cand.get("duration")
        if duration and duration > MAX_TRACK_SECONDS:
            continue
        try:
            with _YDL(ydl_opts) as ydl:
                dl_info = ydl.extract_info(cand["url"], download=True)
                if not dl_info:
                    continue  # rejected by match_filter (too long / live)
                if "entries" in dl_info:
                    entries = [e for e in (dl_info.get("entries") or []) if e]
                    if not entries:
                        continue
                    dl_info = entries[0]
                filename = ydl.prepare_filename(dl_info)
                mp3_path = os.path.splitext(filename)[0] + ".mp3"
                if not os.path.exists(mp3_path):
                    continue
                webpage_url = dl_info.get("webpage_url") or dl_info.get("url") or ""
                return mp3_path, webpage_url
        except Exception as e:
            if _is_drm_error(e):
                _mark_sc_drm(*keys, *_sc_ids_from_exc(e))
            log.warning("SoundCloud candidate skipped for '%s': %s", query, _short(e))
            continue
    if skipped_drm:
        log.info("SoundCloud: skipped %d known DRM-protected candidate(s) for '%s'", skipped_drm, query)
    return None


# --- VK Music (fallback) ---------------------------------------------------
_VK_API_VERSION = "5.131"
_VK_CLIENT_ID = "2685278"          # Kate Mobile public client id
_VK_CLIENT_SECRET = "lxhD8OD7dMsqtXIm5IUY"  # Kate Mobile public client secret
_vk_token_cache: dict = {}
_VK_AUTH_COOLDOWN_SECONDS = 1800  # 30 min - avoid retrying login repeatedly and getting locked out
_vk_auth_cooldown_until = 0.0


_vk_hardcoded_token_invalid = False  # set True once VK confirms VK_ACCESS_TOKEN itself is bad


_vk_missing_creds_logged = False


async def _vk_get_token() -> str | None:
    global _vk_auth_cooldown_until, _vk_missing_creds_logged
    # Preferred path: a long-lived token obtained once via the Kate Mobile
    # OAuth implicit flow. No network call, no password-grant risk, no
    # cooldown - just use it directly every time, UNLESS VK itself has
    # already told us this exact token is invalid/expired this session.
    if VK_ACCESS_TOKEN and not _vk_hardcoded_token_invalid:
        return VK_ACCESS_TOKEN
    if _vk_token_cache.get("token"):
        return _vk_token_cache["token"]
    if not VK_LOGIN or not VK_PASSWORD:
        if not _vk_missing_creds_logged:
            _vk_missing_creds_logged = True
            log.warning("VK fallback disabled: VK_ACCESS_TOKEN / VK_LOGIN+VK_PASSWORD not set in environment")
        return None
    now = time.time()
    if now < _vk_auth_cooldown_until:
        log.info("VK fallback skipped: auth in cooldown for %d more sec", int(_vk_auth_cooldown_until - now))
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
        _vk_auth_cooldown_until = now + _VK_AUTH_COOLDOWN_SECONDS
        return None
    token = data.get("access_token")
    if not token:
        log.warning("VK auth failed: %s", data.get("error_description") or data)
        _vk_auth_cooldown_until = now + _VK_AUTH_COOLDOWN_SECONDS
        return None
    _vk_token_cache["token"] = token
    return token


async def _vk_search_tracks(query: str, count: int = 1) -> list[dict]:
    global _vk_hardcoded_token_invalid
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
        err = data["error"]
        log.warning("VK search error: %s", err.get("error_msg"))
        # error_code 4 / 5 = invalid or expired access_token. If this was
        # our hardcoded VK_ACCESS_TOKEN, stop using it and fall back to
        # VK_LOGIN/VK_PASSWORD (if set) on the next call instead of
        # failing forever with the same dead token.
        if err.get("error_code") in (4, 5) and token == VK_ACCESS_TOKEN and not _vk_hardcoded_token_invalid:
            _vk_hardcoded_token_invalid = True
            log.error(
                "VK_ACCESS_TOKEN is invalid/expired (VK rejected it) - ignoring it from now on. "
                "Get a fresh token, or set VK_LOGIN/VK_PASSWORD as a fallback."
            )
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


YT_SEARCH_CANDIDATES = max(1, int(os.getenv("YT_SEARCH_CANDIDATES", "3")))
# No NEW player_client attempt / candidate is started once this many seconds
# have passed since the search began (an attempt already running may finish).
YT_SEARCH_DEADLINE_SECONDS = int(os.getenv("YT_SEARCH_DEADLINE_SECONDS", "25"))


def _youtube_audio_download(url: str, outdir: str, deadline: float | None = None) -> str:
    """Download the audio of ONE YouTube video as mp3 (client rotation +
    circuit breaker via _yt_with_clients). Returns the mp3 path."""

    def configure(ydl_opts: dict) -> None:
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["match_filter"] = _duration_match_filter()
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]

    def runner(ydl_opts: dict) -> str:
        with _YDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise MediaTooLargeError(
                    f"video is live or longer than {MAX_TRACK_SECONDS // 60} min"
                )
            if "entries" in info:
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise RuntimeError("yt-dlp returned an empty playlist")
                info = entries[0]
            filename = ydl.prepare_filename(info)
            mp3_path = os.path.splitext(filename)[0] + ".mp3"
            if not os.path.exists(mp3_path):
                raise RuntimeError("mp3 file missing after yt-dlp post-processing")
            return mp3_path

    return _yt_with_clients(url, outdir, configure, runner, deadline=deadline)


def _run_youtube_search_download(query: str, outdir: str) -> tuple[str, str] | None:
    """Search YouTube and download the audio of the first playable result.
    Returns (mp3_path, watch_url) or None - never raises, since this is a
    fallback step in the search chain.

    Old behaviour: `ytsearch1:` + download=True, i.e. ONLY the top hit was ever
    tried (often a live/embed-restricted upload - see '(Live)' in the logs),
    and the whole search+download was repeated for each of the 6 clients.
    New behaviour: one cheap flat search, then up to YT_SEARCH_CANDIDATES
    different videos are tried, all inside YT_SEARCH_DEADLINE_SECONDS, and the
    circuit breaker makes the whole thing fail fast during an outage."""
    if YOUTUBE_BREAKER.is_open() and not YOUTUBE_BREAKER.allow():
        log.info("YouTube circuit open - skipping YouTube for '%s'", query)
        return None
    candidates = _run_youtube_list_search(query, YT_SEARCH_CANDIDATES)
    if not candidates:
        return None
    deadline = time.monotonic() + YT_SEARCH_DEADLINE_SECONDS
    for cand in candidates:
        if time.monotonic() > deadline:
            break
        duration = cand.get("duration")
        if duration and duration > MAX_TRACK_SECONDS:
            continue
        url = cand.get("url")
        if not url:
            continue
        try:
            return _youtube_audio_download(url, outdir, deadline=deadline), url
        except MediaTooLargeError:
            continue
        except Exception as e:
            log.warning("YouTube candidate %s skipped for '%s': %s", cand.get("id"), query, _short(e))
            if "youtube_circuit_open" in str(e).lower():
                break
            continue
    return None


def _vk_configured() -> bool:
    """True if VK can possibly work (avoids a pointless step + warning spam)."""
    return bool(
        (VK_ACCESS_TOKEN and not _vk_hardcoded_token_invalid)
        or _vk_token_cache.get("token")
        or (VK_LOGIN and VK_PASSWORD)
    )


async def search_and_download_song(query: str, outdir: str) -> tuple[str, str]:
    """Search order: SoundCloud -> YouTube -> VK Music (VK only when
    configured). Raises RuntimeError if everything fails."""
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run_soundcloud_search_download, query, outdir)
    if result:
        return result
    log.info("SoundCloud had no usable result for '%s' - trying YouTube", query)
    result = await loop.run_in_executor(None, _run_youtube_search_download, query, outdir)
    if result:
        return result
    if _vk_configured():
        log.info("YouTube had no usable result for '%s' - trying VK Music", query)
        result = await vk_search_and_download(query, outdir)
        if result:
            return result
    else:
        log.info("YouTube had no usable result for '%s' (VK not configured)", query)
    raise RuntimeError(f"'{query}' uchun SoundCloud, YouTube yoki VK Music'da hech narsa topilmadi")


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
    _sc_cookies = _private_cookie_copy(SOUNDCLOUD_COOKIES_FILE, None)
    if _sc_cookies:
        ydl_opts["cookiefile"] = _sc_cookies
    try:
        with _YDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
            entries = [e for e in (info.get("entries") or []) if e]
    except Exception as e:
        log.warning("SoundCloud list search failed for '%s': %s", query, e)
        entries = []

    results = []
    for e in entries:
        if _is_sc_drm_blocked(*_sc_entry_keys(e)):
            continue  # known DRM (Go+) track: it can never be downloaded
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


def _run_youtube_list_search(query: str, limit: int) -> list[dict]:
    """Flat YouTube search (metadata only). It never touches the player API,
    so rotating player_client groups gains nothing here - a single attempt is
    enough (the old code looped over all 6 clients)."""
    ydl_opts = _build_ydl_opts_base(None, PLAYER_CLIENT_FALLBACKS[0])
    ydl_opts["extract_flat"] = "in_playlist"
    ydl_opts["skip_download"] = True
    try:
        with _YDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except Exception as e:
        log.warning("YouTube list search failed for '%s': %s", query, _short(e))
        return []
    entries = [e for e in ((info or {}).get("entries") or []) if e]
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
                "source": "youtube",
            }
        )
    return results


async def text_search_songs(query: str, limit: int = SEARCH_FETCH_LIMIT) -> list[dict]:
    """Search order: SoundCloud -> YouTube -> VK Music."""
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _run_soundcloud_list_search, query, limit)
    if not results:
        log.info("SoundCloud list search empty for '%s' - trying YouTube", query)
        results = await loop.run_in_executor(None, _run_youtube_list_search, query, limit)
    if not results:
        log.info("YouTube list search empty for '%s' - trying VK Music", query)
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
        "match_filter": _duration_match_filter(),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    _sc_cookies = _private_cookie_copy(SOUNDCLOUD_COOKIES_FILE, outdir)
    if _sc_cookies:
        ydl_opts["cookiefile"] = _sc_cookies
    try:
        with _YDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise MediaTooLargeError(f"track is longer than {MAX_TRACK_SECONDS // 60} min")
            filename = ydl.prepare_filename(info)
            return os.path.splitext(filename)[0] + ".mp3"
    except Exception as e:
        if _is_drm_error(e):
            # remember it, so the follow-up "fall back to search" does not
            # pick this very same track again
            _mark_sc_drm(url, *_sc_ids_from_exc(e))
        raise


def _run_youtube_audio_download_by_url(url: str, outdir: str) -> str:
    return _youtube_audio_download(url, outdir)


async def download_song_by_url(url: str, outdir: str, source: str = "soundcloud") -> str:
    """Downloads a track picked from the search list. `source` tells us
    whether `url` is a SoundCloud/YouTube page URL (needs yt-dlp) or a
    direct VK mp3 link (plain HTTP GET)."""
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
    if source == "youtube":
        return await loop.run_in_executor(None, _run_youtube_audio_download_by_url, url, outdir)
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


def _has_url(message: Message) -> bool:
    """True if a URL appears ANYWHERE in the message text.

    aiogram's F.text.regexp() uses re.match() under the hood, which only
    checks from the very start of the string. That misses URLs buried
    inside a longer pasted block (e.g. a video player's debug/stats text
    that happens to contain a link) - such messages would otherwise fall
    through to the song-search handler and get searched for verbatim.
    """
    return bool(URL_RE.search(message.text or ""))


# ============================================================
# HANDLERS: media link
# ============================================================
@router.message(F.func(_has_url))
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
        async with HeavyJobSlot(status, lang, "downloading"):
            filepath, info = await download_media(url, outdir, platform)
    except Exception as e:
        shutil.rmtree(outdir, ignore_errors=True)
        if platform == "tiktok":
            await status.edit_text(t(lang, "tiktok_unavailable"))
        else:
            code = classify_download_error(e)
            # Distinct log level per severity, so monitoring can grep/alert
            # on the ones that actually need attention (e.g. YOUTUBE_BLOCKED)
            # separately from routine per-user failures.
            if code == "ERROR_YOUTUBE_BLOCKED":
                log.error("download failed (%s) for platform=%s: %s", code, platform, e)
            elif code in ("ERROR_FACEBOOK_PARSE", "ERROR_PINTEREST_VIDEO"):
                log.warning("download failed (%s) for platform=%s: %s", code, platform, e)
            else:
                log.info("download failed (%s) for platform=%s: %s", code, platform, e)
            if code == "ERROR_STALE_COOKIE":
                await status.edit_text(t(lang, "err_stale_cookie"))
            elif code == "ERROR_BUSY":
                await status.edit_text(t(lang, "err_busy"))
            elif code == "ERROR_YOUTUBE_BLOCKED":
                await status.edit_text(t(lang, "err_youtube_blocked"))
            elif code == "ERROR_PINTEREST_VIDEO":
                await status.edit_text(t(lang, "err_pinterest_video"))
            elif code == "ERROR_FACEBOOK_PARSE":
                await status.edit_text(t(lang, "err_facebook_parse"))
            elif code == "ERROR_PRIVATE":
                await status.edit_text(t(lang, "err_private"))
            elif code == "ERROR_EXPIRED":
                await status.edit_text(t(lang, "err_expired"))
            elif code == "ERROR_DELETED":
                await status.edit_text(t(lang, "link_not_found"))
            else:
                await status.edit_text(t(lang, "error"))
        return

    try:
        _ensure_within_upload_limit(filepath)
    except MediaTooLargeError as e:
        log.info("media too large to send (%s): %s", platform, e)
        shutil.rmtree(outdir, ignore_errors=True)
        await status.edit_text(t(lang, "err_media_too_large"))
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
            await message.answer_video(
                FSInputFile(filepath),
                caption=caption,
                reply_markup=keyboard,
                supports_streaming=True,
                width=info.get("width") or None,
                height=info.get("height") or None,
                duration=info.get("duration") or None,
            )
        _cache_put(FILE_CACHE, token, {"filepath": filepath, "source_url": url}, FILE_CACHE_MAX_SIZE)
        asyncio.create_task(_expire_cache(token, outdir, delay=CACHE_TTL_SECONDS))
    except Exception as e:
        log.warning("send failed: %s", e)
        await message.answer(t(lang, "error"))
        shutil.rmtree(outdir, ignore_errors=True)


async def _expire_cache(token: str, outdir: str, delay: int):
    await asyncio.sleep(delay)
    FILE_CACHE.pop(token, None)
    shutil.rmtree(outdir, ignore_errors=True)


# ============================================================
# HANDLER: user's own video / voice / audio message (not a link)
#
# Recognizes the music in it via Shazam, tells the user the title/artist,
# then tries to fetch a clean copy from SoundCloud/VK. If that fails, we
# fall back to giving a YouTube search link instead of a bare error.
# ============================================================
@router.message(F.video | F.voice | F.audio | F.video_note)
async def handle_own_media(message: Message):
    lang = await get_user_lang(message.from_user.id)

    missing = await get_unsubscribed_channels(message.from_user.id)
    if missing:
        await message.answer(t(lang, "subscribe_required"), reply_markup=subscribe_kb(lang, missing))
        return

    media = message.video or message.voice or message.audio or message.video_note
    if not media:
        return

    # Bot API getFile refuses files > 20 MB ('file is too big'). The logs showed this
    # ending as a generic error after the user had already waited; say so up front.
    if (getattr(media, "file_size", None) or 0) > TELEGRAM_GETFILE_LIMIT_BYTES:
        await message.answer(t(lang, "err_file_too_big_input"))
        return

    status = await message.answer(t(lang, "recognizing"))
    outdir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    try:
        file = await bot.get_file(media.file_id)
        ext = os.path.splitext(file.file_path or "")[1] or ".bin"
        src_path = os.path.join(outdir, f"input{ext}")
        buf = await bot.download_file(file.file_path)
        with open(src_path, "wb") as f:
            f.write(buf.read())

        loop = asyncio.get_running_loop()
        async with HeavyJobSlot(status, lang, "recognizing"):
            audio_sample = await loop.run_in_executor(None, extract_audio_for_recognition, src_path, outdir)
        if not audio_sample:
            await status.edit_text(t(lang, "not_recognized"))
            return
        song = await recognize_song(audio_sample)
        if not song:
            await status.edit_text(t(lang, "not_recognized"))
            return

        # tell the user what we found right away, before trying to fetch it
        await status.edit_text(t(lang, "found_song", title=song["title"], artist=song["artist"]))

        query = f"{song['artist']} {song['title']}"
        try:
            async with HeavyJobSlot(status, lang, "downloading"):
                mp3_path, song_link = await search_and_download_song(query, outdir)
            _ensure_within_upload_limit(mp3_path)
        except Exception as e:
            log.info("own-media song download failed, falling back to YouTube link: %s", e)
            yt_link = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
            text = t(lang, "download_failed_yt_link", link=yt_link)
            try:
                await status.edit_text(text)
            except Exception:
                await message.answer(text)
            return

        await message.answer_audio(
            FSInputFile(mp3_path),
            title=song["title"],
            performer=song["artist"],
            caption=build_song_caption(song_link),
            reply_markup=song_result_kb(lang, song["title"], song["artist"], song_link),
        )
        try:
            await status.delete()
        except Exception:
            pass
    except Exception as e:
        log.warning("own media recognition failed: %s", e)
        err_key = "err_file_too_big_input" if "file is too big" in str(e).lower() else "error"
        try:
            await status.edit_text(t(lang, err_key))
        except Exception:
            await message.answer(t(lang, err_key))
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


@router.message(F.text & ~F.func(_has_url) & ~F.text.startswith("/"))
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
    _cache_put(SEARCH_CACHE, token, {"query": query, "results": results, "page": 0}, SEARCH_CACHE_MAX_SIZE)
    asyncio.create_task(_expire_search_cache(token, delay=SEARCH_CACHE_TTL_SECONDS))

    text, kb = render_search_page(lang, token)
    await status.edit_text(text, reply_markup=kb)


# (user_id, track) pairs currently being downloaded. The logs showed two identical picks handled
# within 1 s of each other (double tap), each running the full SoundCloud->YouTube chain.
_INFLIGHT_PICKS: set = set()


@router.callback_query(F.data.startswith("srch:"))
async def cb_search_action(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id)
    _, token, action = call.data.split(":", 2)
    data = SEARCH_CACHE.get(token)
    if not data:
        await _safe_cb_answer(call, t(lang, "file_expired"), show_alert=True)
        return

    if action == "cancel":
        SEARCH_CACHE.pop(token, None)
        try:
            await call.message.delete()
        except Exception:
            pass
        await _safe_cb_answer(call)
        return

    if action == "prev":
        if data["page"] > 0:
            data["page"] -= 1
        text, kb = render_search_page(lang, token)
        await call.message.edit_text(text, reply_markup=kb)
        await _safe_cb_answer(call)
        return

    if action == "next":
        max_page = (len(data["results"]) - 1) // SEARCH_RESULTS_PER_PAGE
        if data["page"] < max_page:
            data["page"] += 1
        text, kb = render_search_page(lang, token)
        await call.message.edit_text(text, reply_markup=kb)
        await _safe_cb_answer(call)
        return

    # otherwise `action` is the 1..8 button - the user picked a song
    if not action.isdigit():
        await _safe_cb_answer(call)
        return
    start = data["page"] * SEARCH_RESULTS_PER_PAGE
    real_idx = start + int(action) - 1
    if real_idx >= len(data["results"]):
        await _safe_cb_answer(call)
        return
    entry = data["results"][real_idx]

    await _safe_cb_answer(call)
    pick_key = (call.from_user.id, str(entry.get("url") or entry.get("id")))
    if pick_key in _INFLIGHT_PICKS:
        return  # duplicate tap: this exact track is already being downloaded for this user
    _INFLIGHT_PICKS.add(pick_key)
    status = await call.message.answer(t(lang, "downloading"))
    work_dir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    try:
        source = entry.get("source", "soundcloud")
        title = entry.get("title") or "Unknown"
        performer = entry.get("uploader") or ""
        # Song picks used to bypass HEAVY_JOB_SEMAPHORE, so yt-dlp + ffmpeg jobs
        # could pile up beyond the 2 slots the 1 GB plan is sized for.
        async with HeavyJobSlot(status, lang, "downloading"):
            try:
                mp3_path = await download_song_by_url(entry["url"], work_dir, source=source)
                link_for_button = entry.get("url") if source in ("soundcloud", "youtube") else None
            except Exception as e:
                if source == "soundcloud" and _is_drm_error(e):
                    # this specific track is Go+/DRM-locked (now remembered in
                    # the blocklist) - fall back to a fresh search using its
                    # title/artist; the search skips the DRM track itself
                    log.info("picked track is DRM-protected, falling back to search: %s", title)
                    mp3_path, link_for_button = await search_and_download_song(
                        f"{performer} {title}".strip(), work_dir
                    )
                else:
                    raise
        _ensure_within_upload_limit(mp3_path)
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
    except MediaTooLargeError as e:
        log.info("song too long/large (text search): %s", e)
        try:
            await status.edit_text(t(lang, "err_media_too_large"))
        except Exception:
            pass
    except Exception as e:
        if "memory_watermark_exceeded" in str(e).lower():
            try:
                await status.edit_text(t(lang, "err_busy"))
            except Exception:
                pass
        else:
            log.info("song download (text search) failed, falling back to YouTube link: %s", _short(e, 400))
            query = f"{entry.get('uploader', '')} {entry.get('title', '')}".strip()
            yt_link = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
            text = t(lang, "download_failed_yt_link", link=yt_link)
            try:
                await status.edit_text(text)
            except Exception:
                pass
    finally:
        _INFLIGHT_PICKS.discard(pick_key)
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
        await _safe_cb_answer(call, t(lang, "file_expired"), show_alert=True)
        return

    await _safe_cb_answer(call)
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
        _cache_put(ARTIST_SEARCH_CACHE, artist_token, song["artist"], ARTIST_SEARCH_CACHE_MAX_SIZE)
        asyncio.create_task(_expire_artist_cache(artist_token, delay=SEARCH_CACHE_TTL_SECONDS))
        try:
            await call.message.edit_caption(
                caption=build_recognized_caption(song["title"], song["artist"], source_url),
                reply_markup=recognized_song_kb(lang, song["title"], song["artist"], artist_token),
            )
        except Exception as e:
            log.warning("could not edit video caption: %s", e)

        query = f"{song['artist']} {song['title']}"
        try:
            mp3_path, song_link = await search_and_download_song(query, work_dir)
            _ensure_within_upload_limit(mp3_path)
        except Exception as e:
            log.info("music download failed, falling back to YouTube link: %s", e)
            yt_link = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query)
            text = t(lang, "download_failed_yt_link", link=yt_link)
            try:
                await status.edit_text(text)
            except Exception:
                await call.message.answer(text)
            return
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
        await _safe_cb_answer(call, t(lang, "file_expired"), show_alert=True)
        return

    await _safe_cb_answer(call)
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
    _cache_put(SEARCH_CACHE, search_token, {"query": artist, "results": results, "page": 0}, SEARCH_CACHE_MAX_SIZE)
    asyncio.create_task(_expire_search_cache(search_token, delay=SEARCH_CACHE_TTL_SECONDS))
    text, kb = render_search_page(lang, search_token)
    await status.edit_text(text, reply_markup=kb)


# ============================================================
# ENTRYPOINT
# ============================================================
TMP_DISK_CLEANUP_INTERVAL = int(os.getenv("TMP_DISK_CLEANUP_INTERVAL", "30"))
STALE_TEMP_DIR_MAX_AGE_SECONDS = 600  # 10 minutes


def _sweep_stale_temp_dirs():
    """Best-effort removal of leftover per-download temp directories older
    than STALE_TEMP_DIR_MAX_AGE_SECONDS. Normal downloads clean up their own
    tempdir when they finish (success or failure via shutil.rmtree in the
    handler), so anything still here this old is orphaned - e.g. left behind
    by a crash mid-download - and just wastes the 1GB /tmp disk until
    something removes it."""
    now = time.time()
    try:
        for name in os.listdir(DOWNLOAD_ROOT):
            path = os.path.join(DOWNLOAD_ROOT, name)
            try:
                if not os.path.isdir(path):
                    continue
                if not (name.startswith("tmp") or "torona" in name.lower()):
                    continue
                age = now - os.path.getmtime(path)
                if age > STALE_TEMP_DIR_MAX_AGE_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                continue
    except Exception as e:
        log.warning("stale temp dir sweep failed: %s", e)


async def _periodic_disk_cleanup_task():
    """Runs _sweep_stale_temp_dirs() every TMP_DISK_CLEANUP_INTERVAL seconds
    for the life of the process, in a thread so the (blocking) os.listdir/
    stat/rmtree calls never stall the event loop."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(TMP_DISK_CLEANUP_INTERVAL)
        try:
            await loop.run_in_executor(None, _sweep_stale_temp_dirs)
        except Exception as e:
            log.warning("periodic disk cleanup task error: %s", e)


async def _periodic_db_health_check_task():
    """Simple 'SELECT 1' every 30s so a dead/unreachable database shows up
    as a clear, distinctly-tagged log line instead of surfacing later as a
    confusing pool-exhaustion error deep inside some unrelated handler."""
    while True:
        await asyncio.sleep(30)
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
        except Exception as e:
            log.error("DB_HEALTH_CHECK_FAILED: %s", e)


async def main():
    global BOT_DISPLAY_NAME, BOT_USERNAME

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set!")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is not set!")

    global _MAIN_EVENT_LOOP
    _MAIN_EVENT_LOOP = asyncio.get_running_loop()

    await init_db()

    # Clean up anything a previous crashed/killed run left behind before we
    # start accepting new downloads, then keep sweeping periodically.
    await asyncio.get_running_loop().run_in_executor(None, _sweep_stale_temp_dirs)
    asyncio.create_task(_periodic_disk_cleanup_task())
    asyncio.create_task(_periodic_db_health_check_task())

    me = await bot.get_me()
    BOT_DISPLAY_NAME = me.first_name or me.username or "Bot"
    BOT_USERNAME = me.username or ""
    log.info("Bot started as @%s (%s)", me.username, BOT_DISPLAY_NAME)
    _log_ytdlp_environment()

    await bot.delete_webhook(drop_pending_updates=True)
    # allowed_updates must be explicit: Telegram does NOT send chat_member or
    # chat_join_request updates unless they're requested. Without them the
    # mandatory-subscription feature silently breaks - join requests never
    # get logged, so users who tapped the invite link are told they still
    # aren't subscribed. resolve_used_update_types() derives the list from
    # the handlers actually registered above, so it stays correct as handlers
    # are added or removed.
    allowed = dp.resolve_used_update_types()
    log.info("polling with allowed_updates=%s", allowed)
    await dp.start_polling(bot, allowed_updates=allowed)


if __name__ == "__main__":
    asyncio.run(main())
