import asyncio
import html as _html
import importlib.util
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram import BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode, ChatMemberStatus, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.exceptions import (
    TelegramRetryAfter,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramServerError,
)
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
# Captured BEFORE the imageio-ffmpeg dir is prepended to PATH below, so this
# is the host's OWN ffmpeg (apt/nix-installed), never our bundled symlink.
# Some statically-linked imageio-ffmpeg builds (seen: 7.0.2) segfault when
# demuxing raw MPEG-TS (used by _download_hls_to_mp4 for Pinterest HLS pins);
# the system ffmpeg doesn't have that problem, so that one path prefers it
# when available and only falls back to the bundled binary otherwise.
SYSTEM_FFMPEG_PATH = shutil.which("ffmpeg")

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

# Song search (text search + Shazam recognition) uses YouTube first (guarded
# by a circuit breaker, see YOUTUBE_BREAKER, so a future YouTube outage still
# fails fast instead of stalling every search), then SoundCloud, then finally
# VK Music if credentials are configured. Only the audio stream is ever
# downloaded from YouTube (not the video track), so this is not slower or
# heavier than the old SoundCloud-first order.
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

# Runtime invalidation flags: when yt-dlp detects that exported cookies have
# been rotated/expired in the browser ("The provided ... account cookies are
# no longer valid"), we automatically stop passing the dead cookie file on
# subsequent requests so we don't poison every player_client attempt.
_YOUTUBE_COOKIES_INVALIDATED = False
_INSTAGRAM_COOKIES_INVALIDATED = False
_FACEBOOK_COOKIES_INVALIDATED = False


def _active_youtube_cookies() -> str | None:
    return None if _YOUTUBE_COOKIES_INVALIDATED else COOKIES_FILE


def _active_instagram_cookies() -> str | None:
    return None if _INSTAGRAM_COOKIES_INVALIDATED else INSTAGRAM_COOKIES_FILE


def _active_facebook_cookies() -> str | None:
    return None if _FACEBOOK_COOKIES_INVALIDATED else FACEBOOK_COOKIES_FILE


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
    "pinterest": re.compile(r"(pinterest\.[a-z]{2,}(?:\.[a-z]{2,})?/|pin\.it/)"),
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
# ANIMATED TELEGRAM CUSTOM EMOJIS & COLORED BUTTONS (Bot API 9.4+)
# ============================================================
# Verified 19-digit animated Telegram custom_emoji_id catalog mapping:
# key -> (custom_emoji_id, fallback_unicode_emoji)
CUSTOM_EMOJI_MAP: dict[str, tuple[str, str]] = {
    "wave": ("5397679249937155116", "👋"),
    "point_down": ("5436256074959194181", "👇"),
    "globe": ("5350469811233110106", "🌐"),
    "flag_uz": ("5350469811233110106", "🇺🇿"),
    "flag_ru": ("5461155860094921420", "🇷🇺"),
    "flag_gb": ("5467887736000093669", "🇬🇧"),
    "link": ("5271604874419647061", "🔗"),
    "hourglass": ("5363826996314912469", "⏳"),
    "clock": ("5348383560933850994", "⏰"),
    "stopwatch": ("5348383560933850994", "⏱"),
    "check": ("5206607081334906820", "✅"),
    "cross": ("5435930383294170924", "❌"),
    "music": ("5348201978306509336", "🎵"),
    "notes": ("5348201978306509336", "🎶"),
    "headphones": ("5354899958329784877", "🎧"),
    "sad": ("5429231939414409061", "😔"),
    "lyrics": ("5346132860631791153", "📜"),
    "search": ("5348544647977254780", "🔍"),
    "warning": ("5431445849026611010", "⚠️"),
    "exclamation": ("5274099962655816924", "❗️"),
    "alert": ("5424972470023104089", "🚨"),
    "question": ("5436113877181941026", "❓"),
    "info": ("5323442290708985472", "ℹ️"),
    "lock": ("5296369303661067030", "🔐"),
    "forbidden": ("5260293700088511294", "⛔"),
    "pin": ("5346081186714246816", "📌"),
    "paperclip": ("5305265301917549162", "📎"),
    "settings": ("5341715473882955310", "🛠"),
    "stats": ("5231200819986047254", "📊"),
    "user": ("5280882957261770698", "👤"),
    "edit": ("5395444784611480792", "✍️"),
    "megaphone": ("5291895124559671372", "📢"),
    "plus": ("5397916757333654639", "➕"),
    "list": ("5345987742276797245", "📋"),
    "trash": ("5255933371980223131", "🗑"),
    "upload": ("5258166849463419417", "📤"),
    "download": ("5301067323573047237", "📥"),
    "package": ("5195169153928946332", "📦"),
    "empty_box": ("5453996128137676323", "📭"),
    "back": ("5255999157994297240", "⬅️"),
    "prev": ("5255999157994297240", "◀️"),
    "next": ("5456119091817384146", "▶️"),
    "eye": ("5350654889963828012", "👁"),
    "video": ("5255906670168543594", "🎬"),
    "sparkles": ("5436020079391183032", "✨"),
    "fire": ("5424972470023104089", "🔥"),
    "star": ("5438496463044752972", "⭐️"),
    "rocket": ("5361930420361378687", "🚀"),
    "heart": ("5348576757152759323", "❤️"),
    "thumbs_up": ("5368324170671202286", "👍"),
}

_ID_TO_FALLBACK: dict[str, str] = {v[0]: v[1] for v in CUSTOM_EMOJI_MAP.values()}
_TG_EMOJI_TAG_RE = re.compile(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', re.DOTALL)


def ce(key: str, fallback: str | None = None) -> str:
    """Return an HTML <tg-emoji emoji-id="...">fallback</tg-emoji> tag for
    animated Telegram custom emojis in messages and captions."""
    emoji_id, default_fb = CUSTOM_EMOJI_MAP.get(key, ("5436020079391183032", "✨"))
    fb = fallback if fallback is not None else default_fb
    return f'<tg-emoji emoji-id="{emoji_id}">{fb}</tg-emoji>'


def emoji_id_for(key: str) -> str:
    """Return the 19-digit custom_emoji_id for `icon_custom_emoji_id` on buttons."""
    return CUSTOM_EMOJI_MAP[key][0]


def strip_tg_emoji(text: str | None) -> str:
    """Replace <tg-emoji ...>fallback</tg-emoji> with its inner fallback emoji
    (used for plain-text surfaces like callback popup alerts or fallback retries)."""
    if not text:
        return ""
    return _TG_EMOJI_TAG_RE.sub(r"\1", text)


def _strip_leading_emoji_label(text: str) -> str:
    """Remove a leading emoji token + space from a button label (e.g. '🎵 Musiqani aniqlash' -> 'Musiqani aniqlash')
    when `icon_custom_emoji_id` already renders the animated icon at the start of the button."""
    cleaned = strip_tg_emoji(text).strip()
    parts = cleaned.split(" ", 1)
    if len(parts) == 2 and any(ord(ch) > 0x2000 for ch in parts[0]):
        return parts[1].strip()
    return cleaned


def ikb(
    text: str,
    *,
    style: str | None = None,
    emoji_key: str | None = None,
    keep_leading_emoji: bool = False,
    **kwargs,
) -> InlineKeyboardButton:
    """Build an InlineKeyboardButton with Bot API 9.4+ colored `style`
    ('primary' blue, 'success' green, 'danger' red) and animated `icon_custom_emoji_id`."""
    label = strip_tg_emoji(text)
    if emoji_key and not keep_leading_emoji:
        stripped = _strip_leading_emoji_label(label)
        if stripped:
            label = stripped
    extra: dict = {}
    if style:
        extra["style"] = style
    if emoji_key and emoji_key in CUSTOM_EMOJI_MAP:
        extra["icon_custom_emoji_id"] = CUSTOM_EMOJI_MAP[emoji_key][0]
    return InlineKeyboardButton(text=label, **extra, **kwargs)


def rkb(
    text: str,
    *,
    style: str | None = None,
    emoji_key: str | None = None,
    keep_leading_emoji: bool = False,
    **kwargs,
) -> KeyboardButton:
    """Build a Reply KeyboardButton with Bot API 9.4+ colored `style`
    and animated `icon_custom_emoji_id`."""
    label = strip_tg_emoji(text)
    if emoji_key and not keep_leading_emoji:
        stripped = _strip_leading_emoji_label(label)
        if stripped:
            label = stripped
    extra: dict = {}
    if style:
        extra["style"] = style
    if emoji_key and emoji_key in CUSTOM_EMOJI_MAP:
        extra["icon_custom_emoji_id"] = CUSTOM_EMOJI_MAP[emoji_key][0]
    return KeyboardButton(text=label, **extra, **kwargs)


# ============================================================
# TRANSLATIONS
# ============================================================
TEXTS = {
    "uz": {
        "choose_lang": f'{ce("globe")} Tilni tanlang / Выберите язык / Choose a language {ce("point_down")}',
        "welcome": (
            f'Assalomu alaykum! {ce("wave")} {ce("sparkles")}\n\n'
            f'{ce("rocket")} <b>{{bot_name}}</b> ga xush kelibsiz!\n\n'
            f'{ce("video")} Menga Instagram, YouTube, TikTok, Pinterest, Facebook yoki Snapchat havolasini '
            f'yuboring — men videoni/mediani tez va sifatli yuklab beraman {ce("fire")}\n\n'
            f'{ce("headphones")} Video ostidagi rangli tugma orqali esa undagi musiqani aniqlab, '
            f'MP3 shaklda yuborib bera olaman {ce("music")}'
        ),
        "send_link": f'{ce("link")} Havolani yuboring (Instagram / YouTube / TikTok / Pinterest / Snapchat) {ce("sparkles")}',
        "downloading": f'{ce("hourglass")} Yuklanmoqda, biroz kuting... {ce("download")}',
        "queued": f'{ce("hourglass")} Hozir band, navbatingiz kelishi bilan boshlanadi... {ce("clock")}',
        "caption": f'{ce("check")} Botimizdan foydalanganingiz uchun rahmat! {ce("heart")}',
        "detect_music_btn": "🎵 Musiqani aniqlash",
        "recognizing": f'{ce("headphones")} Musiqa aniqlanmoqda... {ce("music")}',
        "not_recognized": f'{ce("sad")} Kechirasiz, bu videodagi musiqani aniqlab bo\'lmadi.',
        "found_song": f'{ce("notes")} Topildi: <b>{{title}}</b> — {ce("user")} {{artist}}\n{ce("hourglass")} Yuklab olinmoqda... {ce("download")}',
        "download_failed_yt_link": f'{ce("cross")} Musiqani yuklab bo\'lmadi.\n{ce("link")} Quyidagi havola orqali topishingiz mumkin:\n{{link}}',
        "song_caption": f'{ce("music")} {{title}} — {{artist}}',
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Rassom bo'yicha qidirish",
        "btn_youtube_link": "🔍 Manbada ochish",
        "lyrics_notice": f'{ce("lyrics")} Qo\'shiq matnini mualliflik huquqi tufayli to\'liq ko\'rsata olmayman, lekin quyidagi havoladan uni topishingiz mumkin {ce("point_down")}',
        "tiktok_unavailable": f'{ce("warning")} Kechirasiz, hozircha TikTok xizmatlari ishlamayapti. Birozdan so\'ng qayta urinib ko\'ring.',
        "link_not_found": f'{ce("cross")} Bu post topilmadi — o\'chirilgan, yopiq (private) yoki linkda xatolik bo\'lishi mumkin.',
        "err_private": f'{ce("lock")} Bu post yopiq (private) yoki yuklab olish cheklangan.\n{ce("info")} Uni ilova ichidan ulashib ko\'ring yoki ochiq (public) qilishni so\'rang.',
        "err_expired": f'{ce("clock")} Bu kontent muddati tugagan (masalan, Snapchat story faqat 24 soat ochiq turadi) va endi mavjud emas.',
        "err_stale_cookie": f'{ce("warning")} Instagram hozircha bu postni bermayapti. Birozdan so\'ng qayta urinib ko\'ring yoki havolani tekshiring.',
        "err_youtube_blocked": f'{ce("hourglass")} YouTube hozircha vaqtincha ishlamayapti, keyinroq urinib ko\'ring.',
        "err_media_too_large": f'{ce("warning")} Fayl juda katta yoki uzun (Telegram bot limiti ~50 MB, treklar uchun ~20 daqiqa). Boshqa variantni tanlang.',
        "err_file_too_big_input": f'{ce("warning")} Yuborilgan fayl juda katta (bot 20 MB gacha fayllarni o\'qiy oladi). Qisqaroq video yuboring.',
        "err_busy": f'{ce("hourglass")} Bot hozir band (band xotira), birozdan keyin qayta urinib ko\'ring.',
        "err_pinterest_video": f'{ce("pin")} Bu Pinterest postini (rasm/video/GIF) hozircha yuklab bo\'lmadi. Post o\'chirilgan yoki yopiq bo\'lishi mumkin.',
        "err_facebook_parse": f'{ce("cross")} Bu Facebook video\'sini yuklab bo\'lmadi, ehtimol u shaxsiy (private) yoki cheklangan.',
        "unsupported_link": f'{ce("cross")} Bu havola qo\'llab-quvvatlanmaydi. Instagram, YouTube, TikTok, Pinterest, Facebook yoki Snapchat havolasini yuboring {ce("link")}',
        "error": f'{ce("cross")} Xatolik yuz berdi, qaytadan urinib ko\'ring.',
        "no_link": f'{ce("exclamation")} Iltimos, media havolasini yuboring {ce("link")}',
        "admin_only": f'{ce("forbidden")} Bu buyruq faqat administratorlar uchun.',
        "admin_panel": f'{ce("settings")} <b>Admin panel</b> {ce("sparkles")}',
        "stats": f'{ce("stats")} <b>Statistika:</b>\n\n{ce("user")} Jami foydalanuvchilar: <b>{{count}}</b> {ce("sparkles")}',
        "broadcast_ask": f'{ce("edit")} Yuboriladigan xabar matnini yuboring {ce("megaphone")}:',
        "broadcast_done": f'{ce("check")} Xabar <b>{{count}}</b> ta foydalanuvchiga muvaffaqiyatli yuborildi {ce("rocket")}',
        "file_expired": f'{ce("stopwatch")} Vaqt tugadi, iltimos havolani qayta yuboring.',
        "lang_set": f'{ce("check")} Til o\'zbekcha etib o\'rnatildi {ce("sparkles")}',
        "searching": f'{ce("search")} Qidirilmoqda... {ce("music")}',
        "search_no_results": f'{ce("sad")} Hech narsa topilmadi. Boshqa nom bilan urinib ko\'ring {ce("search")}',
        "search_results_range": f'{ce("list")} Natijalar <b>{{start}}-{{end}}</b> / <b>{{total}}</b>',
        "help": (
            f'{ce("info")} <b>Yordam</b> {ce("sparkles")}\n\n'
            f'{ce("link")} <b>1)</b> Instagram, YouTube, TikTok, Pinterest, Facebook yoki Snapchat havolasini yuboring.\n'
            f'{ce("video")} <b>2)</b> Bot mediani tez va sifatli yuklab beradi.\n'
            f'{ce("music")} <b>3)</b> Video ostidagi {ce("headphones")} tugmasini bosing — bot videodagi musiqani aniqlab, '
            f'MP3 shaklida yuboradi.\n'
            f'{ce("search")} <b>4)</b> Yoki shunchaki qo\'shiq/ijrochi nomini yozib yuboring — bot YouTube\'dan '
            f'qidirib, ro\'yxatdan tanlaganingizni MP3 shaklida yuboradi {ce("notes")}\n\n'
            f'{ce("globe")} Tilni o\'zgartirish uchun pastdagi <b>"Til"</b> tugmasidan foydalaning.'
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
        "cache_cleared": f'{ce("check")} Vaqtinchalik cache tozalandi (<b>{{count}}</b> ta yozuv) {ce("trash")}',
        "db_export_empty": f'{ce("empty_box")} Bazada foydalanuvchilar topilmadi.',
        "db_export_caption": f'{ce("package")} DB export — <b>{{count}}</b> ta foydalanuvchi (.vk fayllar zip ichida) {ce("check")}',
        "db_export_fail": f'{ce("cross")} DB export qilishda xatolik yuz berdi.',
        "db_import_ask": f'{ce("download")} Import qilish uchun avval yuborilgan .zip yoki .vk fayl(lar)ni yuboring {ce("package")}',
        "db_import_done": (
            f'{ce("check")} Import tugadi:\n'
            f'{ce("plus")} Qo\'shildi: <b>{{added}}</b> ta\n'
            f'{ce("edit")} Yangilandi: <b>{{updated}}</b> ta\n'
            f'{ce("cross")} Xato: <b>{{failed}}</b> ta'
        ),
        "db_import_fail": f'{ce("cross")} Faylni o\'qib bo\'lmadi. To\'g\'ri .zip yoki .vk fayl yuboring.',
        "db_import_no_file": f'{ce("exclamation")} Iltimos, .zip yoki .vk fayl yuboring {ce("package")}',
        "ask_channel": (
            f'{ce("paperclip")} <b>Kanal/gurux qo\'shish uchun:</b>\n\n'
            f'{ce("settings")} <b>1)</b> Botni o\'sha kanal/guruhga <b>administrator</b> qilib qo\'ying.\n'
            f'{ce("megaphone")} <b>2)</b> Shu yerga o\'sha kanal/guruhdagi istalgan xabarni forward qiling, '
            f'yoki uning @username\'ini, yoki chat_id sini yuboring.\n\n'
            f'{ce("warning")} Yopiq (private) kanal bo\'lsa, botga <b>\'Invite Users via Link\'</b> huquqini ham bering.\n'
            f'{ce("info")} Taklif havolasi (https://t.me/+...) yuborish ishlamaydi — forward qiling.'
        ),
        "channel_added": f'{ce("check")} <b>"{{title}}"</b> majburiy obunalar ro\'yxatiga qo\'shildi {ce("sparkles")}',
        "channel_add_fail_not_admin": f'{ce("cross")} Botni avval o\'sha kanal/guruhga administrator qiling, keyin qaytadan urinib ko\'ring.',
        "channel_add_fail_link": f'{ce("cross")} Kanal uchun taklif havolasi (invite link) yaratib bo\'lmadi, shuning uchun kanal saqlanmadi. Botning o\'sha kanalda \'Invite Users via Link\' huquqi borligini tekshirib, qaytadan urinib ko\'ring.',
        "channel_add_fail_not_found": f'{ce("cross")} Bunday kanal/guruh topilmadi. Username\'ni tekshiring (@kanal), yoki eng ishonchlisi — o\'sha kanaldan istalgan xabarni menga forward qiling.',
        "channel_add_fail_invite_link": f'{ce("cross")} Taklif havolasi (https://t.me/+...) orqali kanalni qo\'shib bo\'lmaydi — Telegram bunga ruxsat bermaydi.\n\n{ce("info")} Buning o\'rniga: o\'sha kanaldan istalgan xabarni menga <b>forward</b> qiling, yoki ochiq kanal bo\'lsa @username yuboring.',
        "channel_add_fail_wrong_type": f'{ce("cross")} Faqat kanal yoki guruhni majburiy obuna sifatida qo\'shish mumkin.',
        "channel_add_fail_no_invite_perm": f'{ce("cross")} Botda bu kanalda taklif havolasi yaratish huquqi yo\'q.\n\n{ce("settings")} Kanal sozlamalari → Administratorlar → Bot → <b>\'Invite Users via Link\'</b> huquqini yoqing va qayta urinib ko\'ring.',
        "channel_add_fail": f'{ce("cross")} Kanal/guruhni aniqlab bo\'lmadi. Forward yoki @username/ID yuboring.',
        "channel_list_empty": f'{ce("empty_box")} Hozircha majburiy obunalar yo\'q.',
        "channel_list_title": f'{ce("list")} <b>Majburiy obunalar ro\'yxati:</b>',
        "channel_removed": f'{ce("trash")} Kanal ro\'yxatdan olib tashlandi.',
        "subscribe_required": f'{ce("warning")} Botdan foydalanish uchun quyidagi kanal(lar)ga a\'zo bo\'ling {ce("point_down")}',
        "check_sub_btn": "✅ Tekshirish",
        "still_not_subscribed": f'{ce("cross")} Siz hali barcha kanallarga a\'zo bo\'lmadingiz.',
        "now_subscribed": f'{ce("check")} Rahmat! Endi botdan foydalanishingiz mumkin, havolani yuboring {ce("sparkles")}',
        "channel_type_public": "ochiq kanal/gurux",
        "channel_subs_label": "{count} a'zo",
        "channel_type_private": "yopiq kanal/gurux",
    },
    "ru": {
        "choose_lang": f'{ce("globe")} Tilni tanlang / Выберите язык / Choose a language {ce("point_down")}',
        "welcome": (
            f'Привет! {ce("wave")} {ce("sparkles")}\n\n'
            f'{ce("rocket")} Добро пожаловать в <b>{{bot_name}}</b>!\n\n'
            f'{ce("video")} Отправьте мне ссылку с Instagram, YouTube, TikTok, Pinterest, Facebook или Snapchat — '
            f'я быстро скачаю видео/медиа {ce("fire")}\n\n'
            f'{ce("headphones")} А цветной кнопкой под видео можно распознать музыку и получить её в MP3 {ce("music")}'
        ),
        "send_link": f'{ce("link")} Отправьте ссылку (Instagram / YouTube / TikTok / Pinterest / Snapchat) {ce("sparkles")}',
        "downloading": f'{ce("hourglass")} Загружается, подождите... {ce("download")}',
        "queued": f'{ce("hourglass")} Сейчас все занято, начнём, как только освободится место в очереди... {ce("clock")}',
        "caption": f'{ce("check")} Спасибо, что пользуетесь ботом! {ce("heart")}',
        "detect_music_btn": "🎵 Распознать музыку",
        "recognizing": f'{ce("headphones")} Распознаём музыку... {ce("music")}',
        "not_recognized": f'{ce("sad")} Не удалось распознать музыку в этом видео.',
        "found_song": f'{ce("notes")} Найдено: <b>{{title}}</b> — {ce("user")} {{artist}}\n{ce("hourglass")} Загружается... {ce("download")}',
        "download_failed_yt_link": f'{ce("cross")} Не удалось скачать музыку.\n{ce("link")} Вы можете найти её по этой ссылке:\n{{link}}',
        "song_caption": f'{ce("music")} {{title}} — {{artist}}',
        "btn_lyrics": "📜 Текст песни",
        "btn_artist_search": "🔍 Поиск по исполнителю",
        "btn_youtube_link": "🔍 Открыть источник",
        "lyrics_notice": f'{ce("lyrics")} Не могу показать полный текст песни из-за авторских прав, но вы можете найти его по ссылке ниже {ce("point_down")}',
        "tiktok_unavailable": f'{ce("warning")} Извините, сервисы TikTok сейчас не работают. Попробуйте позже.',
        "link_not_found": f'{ce("cross")} Пост не найден — он мог быть удалён, закрыт (private) или ссылка неверна.',
        "err_private": f'{ce("lock")} Этот пост закрыт (private) или загрузка ограничена владельцем.\n{ce("info")} Попробуйте поделиться им из самого приложения или попросите сделать его публичным.',
        "err_expired": f'{ce("clock")} Срок действия этого контента истёк (например, Snapchat-истории доступны только 24 часа) и он больше не доступен.',
        "err_stale_cookie": f'{ce("warning")} Instagram сейчас не отдаёт этот пост. Попробуйте ещё раз чуть позже или проверьте ссылку.',
        "err_youtube_blocked": f'{ce("hourglass")} YouTube сейчас временно не работает, попробуйте позже.',
        "err_media_too_large": f'{ce("warning")} Файл слишком большой или длинный (лимит бота Telegram ~50 МБ, для треков ~20 минут). Выберите другой вариант.',
        "err_file_too_big_input": f'{ce("warning")} Присланный файл слишком большой (бот читает файлы до 20 МБ). Отправьте видео покороче.',
        "err_busy": f'{ce("hourglass")} Бот сейчас перегружен, попробуйте ещё раз через некоторое время.',
        "err_pinterest_video": f'{ce("pin")} Не удалось скачать этот пин Pinterest (фото/видео/GIF). Возможно, он удалён или закрыт.',
        "err_facebook_parse": f'{ce("cross")} Не удалось скачать это видео с Facebook, возможно оно приватное или ограничено.',
        "unsupported_link": f'{ce("cross")} Эта ссылка не поддерживается. Отправьте ссылку с Instagram, YouTube, TikTok, Pinterest, Facebook или Snapchat {ce("link")}',
        "error": f'{ce("cross")} Произошла ошибка, попробуйте ещё раз.',
        "no_link": f'{ce("exclamation")} Пожалуйста, отправьте ссылку на медиа {ce("link")}',
        "admin_only": f'{ce("forbidden")} Эта команда только для администраторов.',
        "admin_panel": f'{ce("settings")} <b>Админ-панель</b> {ce("sparkles")}',
        "stats": f'{ce("stats")} <b>Статистика:</b>\n\n{ce("user")} Всего пользователей: <b>{{count}}</b> {ce("sparkles")}',
        "broadcast_ask": f'{ce("edit")} Отправьте текст рассылки {ce("megaphone")}:',
        "broadcast_done": f'{ce("check")} Сообщение отправлено <b>{{count}}</b> пользователям {ce("rocket")}',
        "file_expired": f'{ce("stopwatch")} Время истекло, отправьте ссылку заново.',
        "lang_set": f'{ce("check")} Язык установлен: русский {ce("sparkles")}',
        "searching": f'{ce("search")} Ищем... {ce("music")}',
        "search_no_results": f'{ce("sad")} Ничего не найдено. Попробуйте другой запрос {ce("search")}',
        "search_results_range": f'{ce("list")} Результаты <b>{{start}}-{{end}}</b> / <b>{{total}}</b>',
        "help": (
            f'{ce("info")} <b>Помощь</b> {ce("sparkles")}\n\n'
            f'{ce("link")} <b>1)</b> Отправьте ссылку с Instagram, YouTube, TikTok, Pinterest, Facebook или Snapchat.\n'
            f'{ce("video")} <b>2)</b> Бот быстро скачает медиа.\n'
            f'{ce("music")} <b>3)</b> Нажмите кнопку {ce("headphones")} под видео — бот распознает музыку и пришлёт её в MP3.\n'
            f'{ce("search")} <b>4)</b> Или просто напишите название песни/исполнителя — бот найдёт на YouTube '
            f'и пришлёт выбранный трек в MP3 {ce("notes")}\n\n'
            f'{ce("globe")} Чтобы сменить язык, используйте кнопку <b>"Язык"</b> внизу.'
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
        "cache_cleared": f'{ce("check")} Временный кэш очищен (<b>{{count}}</b> записей) {ce("trash")}',
        "db_export_empty": f'{ce("empty_box")} Пользователи в базе не найдены.',
        "db_export_caption": f'{ce("package")} Экспорт БД — <b>{{count}}</b> пользователей (.vk файлы в zip) {ce("check")}',
        "db_export_fail": f'{ce("cross")} Ошибка при экспорте БД.',
        "db_import_ask": f'{ce("download")} Для импорта отправьте ранее выгруженный .zip или .vk файл(ы) {ce("package")}',
        "db_import_done": (
            f'{ce("check")} Импорт завершён:\n'
            f'{ce("plus")} Добавлено: <b>{{added}}</b>\n'
            f'{ce("edit")} Обновлено: <b>{{updated}}</b>\n'
            f'{ce("cross")} Ошибок: <b>{{failed}}</b>'
        ),
        "db_import_fail": f'{ce("cross")} Не удалось прочитать файл. Отправьте корректный .zip или .vk файл.',
        "db_import_no_file": f'{ce("exclamation")} Пожалуйста, отправьте .zip или .vk файл {ce("package")}',
        "ask_channel": (
            f'{ce("paperclip")} <b>Чтобы добавить канал/группу:</b>\n\n'
            f'{ce("settings")} <b>1)</b> Сделайте бота <b>администратором</b> в этом канале/группе.\n'
            f'{ce("megaphone")} <b>2)</b> Перешлите сюда любое сообщение из него, либо отправьте его @username '
            f'или chat_id.\n\n'
            f'{ce("warning")} Для закрытого канала также включите право <b>«Invite Users via Link»</b>.'
        ),
        "channel_added": f'{ce("check")} <b>"{{title}}"</b> добавлен в обязательные подписки {ce("sparkles")}',
        "channel_add_fail_not_admin": f'{ce("cross")} Сначала сделайте бота администратором канала/группы, затем попробуйте снова.',
        "channel_add_fail_link": f'{ce("cross")} Не удалось создать пригласительную ссылку для канала, поэтому канал не сохранён. Проверьте, что у бота есть право «Invite Users via Link» в этом канале, и попробуйте снова.',
        "channel_add_fail_not_found": f'{ce("cross")} Такой канал/группа не найдены. Проверьте username (@канал) или, что надёжнее, перешлите мне любое сообщение из этого канала.',
        "channel_add_fail_invite_link": f'{ce("cross")} Добавить канал по пригласительной ссылке (https://t.me/+...) нельзя — Telegram этого не поддерживает.\n\n{ce("info")} Вместо этого: <b>перешлите</b> мне любое сообщение из канала, либо отправьте @username, если канал публичный.',
        "channel_add_fail_wrong_type": f'{ce("cross")} В качестве обязательной подписки можно добавить только канал или группу.',
        "channel_add_fail_no_invite_perm": f'{ce("cross")} У бота нет права создавать пригласительные ссылки в этом канале.\n\n{ce("settings")} Настройки канала → Администраторы → Бот → включите <b>«Invite Users via Link»</b> и попробуйте снова.',
        "channel_add_fail": f'{ce("cross")} Не удалось определить канал/группу. Перешлите сообщение или отправьте @username/ID.',
        "channel_list_empty": f'{ce("empty_box")} Обязательных подписок пока нет.',
        "channel_list_title": f'{ce("list")} <b>Список обязательных подписок:</b>',
        "channel_removed": f'{ce("trash")} Канал удалён из списка.',
        "subscribe_required": f'{ce("warning")} Чтобы пользоваться ботом, подпишитесь на следующие канал(ы) {ce("point_down")}',
        "check_sub_btn": "✅ Проверить",
        "still_not_subscribed": f'{ce("cross")} Вы ещё не подписаны на все каналы.',
        "now_subscribed": f'{ce("check")} Спасибо! Теперь вы можете пользоваться ботом, отправьте ссылку {ce("sparkles")}',
        "channel_type_public": "открытый канал/группа",
        "channel_subs_label": "{count} подписчиков",
        "channel_type_private": "закрытый канал/группа",
    },
    "en": {
        "choose_lang": f'{ce("globe")} Tilni tanlang / Выберите язык / Choose a language {ce("point_down")}',
        "welcome": (
            f'Hello! {ce("wave")} {ce("sparkles")}\n\n'
            f'{ce("rocket")} Welcome to <b>{{bot_name}}</b>!\n\n'
            f'{ce("video")} Send me a link from Instagram, YouTube, TikTok, Pinterest, Facebook or Snapchat — '
            f'I\'ll quickly download the media for you {ce("fire")}\n\n'
            f'{ce("headphones")} Use the colored button under the video to recognize '
            f'the music in it and get it as an MP3 {ce("music")}'
        ),
        "send_link": f'{ce("link")} Send a link (Instagram / YouTube / TikTok / Pinterest / Snapchat) {ce("sparkles")}',
        "downloading": f'{ce("hourglass")} Downloading, please wait... {ce("download")}',
        "queued": f'{ce("hourglass")} All slots are busy right now, this will start as soon as one frees up... {ce("clock")}',
        "caption": f'{ce("check")} Thanks for using our bot! {ce("heart")}',
        "detect_music_btn": "🎵 Recognize music",
        "recognizing": f'{ce("headphones")} Recognizing the music... {ce("music")}',
        "not_recognized": f'{ce("sad")} Sorry, couldn\'t recognize the music in this video.',
        "found_song": f'{ce("notes")} Found: <b>{{title}}</b> — {ce("user")} {{artist}}\n{ce("hourglass")} Downloading... {ce("download")}',
        "download_failed_yt_link": f'{ce("cross")} Could not download the music.\n{ce("link")} You can find it via this link:\n{{link}}',
        "song_caption": f'{ce("music")} {{title}} — {{artist}}',
        "btn_lyrics": "📜 Lyrics",
        "btn_artist_search": "🔍 Search by artist",
        "btn_youtube_link": "🔍 Open source",
        "lyrics_notice": f'{ce("lyrics")} I can\'t display full lyrics due to copyright, but you can find them via the link below {ce("point_down")}',
        "tiktok_unavailable": f'{ce("warning")} Sorry, TikTok services aren\'t working right now. Please try again later.',
        "link_not_found": f'{ce("cross")} Post not found — it may have been deleted, made private, or the link is wrong.',
        "err_private": f'{ce("lock")} This post is private or downloads are restricted by the owner.\n{ce("info")} Try sharing it from within the app itself, or ask for it to be made public.',
        "err_expired": f'{ce("clock")} This content has expired (e.g. Snapchat stories only stay up for 24 hours) and is no longer available.',
        "err_stale_cookie": f'{ce("warning")} Instagram is not serving this post right now. Please try again a bit later or check the link.',
        "err_youtube_blocked": f'{ce("hourglass")} YouTube is temporarily unavailable, please try again later.',
        "err_media_too_large": f'{ce("warning")} The file is too big or too long (Telegram bot limit is ~50 MB, ~20 min for tracks). Please pick another option.',
        "err_file_too_big_input": f'{ce("warning")} The file you sent is too large (the bot can read files up to 20 MB). Please send a shorter video.',
        "err_busy": f'{ce("hourglass")} The bot is under heavy load right now, please try again in a bit.',
        "err_pinterest_video": f'{ce("pin")} This Pinterest pin (image/video/GIF) couldn\'t be downloaded right now. It may have been removed or be private.',
        "err_facebook_parse": f'{ce("cross")} This Facebook video couldn\'t be downloaded, it may be private or restricted.',
        "unsupported_link": f'{ce("cross")} This link isn\'t supported. Please send a link from Instagram, YouTube, TikTok, Pinterest, Facebook or Snapchat {ce("link")}',
        "error": f'{ce("cross")} Something went wrong, please try again.',
        "no_link": f'{ce("exclamation")} Please send a media link {ce("link")}',
        "admin_only": f'{ce("forbidden")} This command is for admins only.',
        "admin_panel": f'{ce("settings")} <b>Admin panel</b> {ce("sparkles")}',
        "stats": f'{ce("stats")} <b>Stats:</b>\n\n{ce("user")} Total users: <b>{{count}}</b> {ce("sparkles")}',
        "broadcast_ask": f'{ce("edit")} Send the broadcast text {ce("megaphone")}:',
        "broadcast_done": f'{ce("check")} Message sent to <b>{{count}}</b> users {ce("rocket")}',
        "file_expired": f'{ce("stopwatch")} Session expired, please send the link again.',
        "lang_set": f'{ce("check")} Language set to English {ce("sparkles")}',
        "searching": f'{ce("search")} Searching... {ce("music")}',
        "search_no_results": f'{ce("sad")} Nothing found. Try a different search term {ce("search")}',
        "search_results_range": f'{ce("list")} Results <b>{{start}}-{{end}}</b> / <b>{{total}}</b>',
        "help": (
            f'{ce("info")} <b>Help</b> {ce("sparkles")}\n\n'
            f'{ce("link")} <b>1)</b> Send a link from Instagram, YouTube, TikTok, Pinterest, Facebook or Snapchat.\n'
            f'{ce("video")} <b>2)</b> The bot quickly downloads the media.\n'
            f'{ce("music")} <b>3)</b> Tap the {ce("headphones")} button under the video — the bot recognizes the music and sends it as MP3.\n'
            f'{ce("search")} <b>4)</b> Or just type a song/artist name — the bot will search YouTube and send the '
            f'track you pick as MP3 {ce("notes")}\n\n'
            f'{ce("globe")} To change language, use the <b>"Language"</b> button below.'
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
        "cache_cleared": f'{ce("check")} Temporary cache cleared (<b>{{count}}</b> entries) {ce("trash")}',
        "db_export_empty": f'{ce("empty_box")} No users found in the database.',
        "db_export_caption": f'{ce("package")} DB export — <b>{{count}}</b> users (.vk files inside a zip) {ce("check")}',
        "db_export_fail": f'{ce("cross")} DB export failed.',
        "db_import_ask": f'{ce("download")} To import, send a previously exported .zip or .vk file(s) {ce("package")}',
        "db_import_done": (
            f'{ce("check")} Import finished:\n'
            f'{ce("plus")} Added: <b>{{added}}</b>\n'
            f'{ce("edit")} Updated: <b>{{updated}}</b>\n'
            f'{ce("cross")} Failed: <b>{{failed}}</b>'
        ),
        "db_import_fail": f'{ce("cross")} Could not read the file. Send a valid .zip or .vk file.',
        "db_import_no_file": f'{ce("exclamation")} Please send a .zip or .vk file {ce("package")}',
        "ask_channel": (
            f'{ce("paperclip")} <b>To add a channel/group:</b>\n\n'
            f'{ce("settings")} <b>1)</b> Make the bot an <b>administrator</b> there.\n'
            f'{ce("megaphone")} <b>2)</b> Forward any message from it here, or send its @username or chat_id.\n\n'
            f'{ce("warning")} For private channels, also enable <b>"Invite Users via Link"</b>.'
        ),
        "channel_added": f'{ce("check")} <b>"{{title}}"</b> added to mandatory subscriptions {ce("sparkles")}',
        "channel_add_fail_not_admin": f'{ce("cross")} Make the bot an administrator of that channel/group first, then try again.',
        "channel_add_fail_link": f'{ce("cross")} Couldn\'t create an invite link for the channel, so it wasn\'t saved. Check the bot has the "Invite Users via Link" permission there, then try again.',
        "channel_add_fail_not_found": f'{ce("cross")} No such channel/group found. Check the username (@channel), or more reliably, forward me any message from that channel.',
        "channel_add_fail_invite_link": f'{ce("cross")} A channel can\'t be added via an invite link (https://t.me/+...) — Telegram doesn\'t allow it.\n\n{ce("info")} Instead: <b>forward</b> me any message from the channel, or send @username if it\'s public.',
        "channel_add_fail_wrong_type": f'{ce("cross")} Only a channel or a group can be added as a mandatory subscription.',
        "channel_add_fail_no_invite_perm": f'{ce("cross")} The bot doesn\'t have permission to create invite links in that channel.\n\n{ce("settings")} Channel settings → Administrators → the bot → enable <b>"Invite Users via Link"</b>, then try again.',
        "channel_add_fail": f'{ce("cross")} Couldn\'t detect the channel/group. Forward a message, or send its @username/ID.',
        "channel_list_empty": f'{ce("empty_box")} No mandatory subscriptions yet.',
        "channel_list_title": f'{ce("list")} <b>Mandatory subscriptions:</b>',
        "channel_removed": f'{ce("trash")} Channel removed from the list.',
        "subscribe_required": f'{ce("warning")} To use the bot, please subscribe to the following channel(s) {ce("point_down")}',
        "check_sub_btn": "✅ Check",
        "still_not_subscribed": f'{ce("cross")} You haven\'t subscribed to all channels yet.',
        "now_subscribed": f'{ce("check")} Thanks! You can use the bot now, send a link {ce("sparkles")}',
        "channel_type_public": "public channel/group",
        "channel_subs_label": "{count} subs",
        "channel_type_private": "private channel/group",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in TEXTS else "uz"
    text = TEXTS[lang].get(key, TEXTS["uz"][key])
    if not kwargs:
        return text
    safe_kwargs = {
        k: (_html.escape(v) if isinstance(v, str) and "<tg-emoji" not in v else v)
        for k, v in kwargs.items()
    }
    return text.format(**safe_kwargs)


def _btn_texts(key: str) -> set[str]:
    """Return both full and stripped button labels across all languages so
    ReplyKeyboardMarkup handlers match whether or not leading emojis are stripped."""
    matches: set[str] = set()
    for v in TEXTS.values():
        raw = v[key]
        matches.add(raw)
        matches.add(strip_tg_emoji(raw))
        stripped = _strip_leading_emoji_label(raw)
        if stripped:
            matches.add(stripped)
    return matches


# ============================================================
# DATABASE (PostgreSQL via asyncpg) + IN-MEMORY CACHING
#
# Production logs showed unhandled asyncpg TimeoutErrors inside `get_lang`
# and `list_channels` during heavy download/ffmpeg spikes because EVERY
# single message and callback executed 2-3 DB queries (`add_user_if_missing`,
# `get_lang`, `list_channels`). Caching known users, user languages, and
# mandatory channels in memory eliminates >95% of routine DB queries and
# provides an instant fallback if the DB briefly stalls.
# ============================================================
_KNOWN_USERS: set[int] = set()
_USER_LANG_CACHE: dict[int, str] = {}
_USER_CACHE_MAX_SIZE = 50000
_CHANNELS_CACHE: list | None = None
_CHANNELS_CACHE_TS: float = 0.0
_CHANNELS_CACHE_TTL: float = 60.0
_JOIN_REQUEST_CACHE: set[tuple[int, int]] = set()


def _remember_user_lang(user_id: int, lang: str) -> None:
    _KNOWN_USERS.add(user_id)
    if len(_KNOWN_USERS) > _USER_CACHE_MAX_SIZE:
        _KNOWN_USERS.clear()
        _KNOWN_USERS.add(user_id)
    _cache_put(_USER_LANG_CACHE, user_id, lang, _USER_CACHE_MAX_SIZE)


def _invalidate_channels_cache() -> None:
    global _CHANNELS_CACHE_TS
    _CHANNELS_CACHE_TS = 0.0


async def init_db():
    global pool
    pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=15,
        timeout=10,
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
    if user_id in _KNOWN_USERS:
        return
    try:
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id
            )
        _KNOWN_USERS.add(user_id)
    except Exception as e:
        log.warning("DB add_user_if_missing skipped (user=%s): %s", user_id, _short(e))


async def set_lang(user_id: int, lang: str):
    _remember_user_lang(user_id, lang)
    try:
        async with pool.acquire(timeout=8) as conn:
            await conn.execute(
                """INSERT INTO users (user_id, lang) VALUES ($1, $2)
                   ON CONFLICT (user_id) DO UPDATE SET lang = EXCLUDED.lang""",
                user_id, lang,
            )
    except Exception as e:
        log.warning("DB set_lang failed (cached in memory for user=%s): %s", user_id, _short(e))


async def get_lang(user_id: int) -> str | None:
    cached = _USER_LANG_CACHE.get(user_id)
    if cached:
        return cached
    try:
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow("SELECT lang FROM users WHERE user_id=$1", user_id)
        if row and row["lang"]:
            _remember_user_lang(user_id, row["lang"])
            return row["lang"]
        return None
    except Exception as e:
        log.warning("DB get_lang timeout/error for user=%s (falling back to 'uz'): %s", user_id, _short(e))
        return _USER_LANG_CACHE.get(user_id, "uz")


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
    _invalidate_channels_cache()


async def list_channels():
    global _CHANNELS_CACHE, _CHANNELS_CACHE_TS
    now = time.monotonic()
    if _CHANNELS_CACHE is not None and (now - _CHANNELS_CACHE_TS) < _CHANNELS_CACHE_TTL:
        return _CHANNELS_CACHE
    try:
        async with pool.acquire(timeout=5) as conn:
            rows = await conn.fetch("SELECT * FROM mandatory_channels ORDER BY id")
        _CHANNELS_CACHE = rows
        _CHANNELS_CACHE_TS = now
        return rows
    except Exception as e:
        log.warning("DB list_channels timeout/error (using cached channels): %s", _short(e))
        return _CHANNELS_CACHE if _CHANNELS_CACHE is not None else []


async def remove_channel(channel_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM mandatory_channels WHERE id=$1", channel_id)
    _invalidate_channels_cache()


async def get_channel(channel_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM mandatory_channels WHERE id=$1", channel_id)


async def log_join_request(chat_id: int, user_id: int):
    _JOIN_REQUEST_CACHE.add((chat_id, user_id))
    try:
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "INSERT INTO join_requests (chat_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                chat_id, user_id,
            )
    except Exception as e:
        log.warning("DB log_join_request failed (cached in memory): %s", _short(e))


async def has_join_request(chat_id: int, user_id: int) -> bool:
    if (chat_id, user_id) in _JOIN_REQUEST_CACHE:
        return True
    try:
        async with pool.acquire(timeout=5) as conn:
            row = await conn.fetchrow(
                "SELECT 1 FROM join_requests WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
            )
        if row is not None:
            _JOIN_REQUEST_CACHE.add((chat_id, user_id))
            return True
        return False
    except Exception as e:
        log.warning("DB has_join_request timeout/error: %s", _short(e))
        return (chat_id, user_id) in _JOIN_REQUEST_CACHE


async def clear_join_request(chat_id: int, user_id: int):
    """Drop a logged join request."""
    _JOIN_REQUEST_CACHE.discard((chat_id, user_id))
    try:
        async with pool.acquire(timeout=5) as conn:
            await conn.execute(
                "DELETE FROM join_requests WHERE chat_id=$1 AND user_id=$2", chat_id, user_id
            )
    except Exception as e:
        log.warning("DB clear_join_request error: %s", _short(e))


async def get_channel_by_chat_id(chat_id: int):
    if _CHANNELS_CACHE is not None:
        for c in _CHANNELS_CACHE:
            if c["chat_id"] == chat_id:
                return c
    try:
        async with pool.acquire(timeout=5) as conn:
            return await conn.fetchrow("SELECT * FROM mandatory_channels WHERE chat_id=$1", chat_id)
    except Exception as e:
        log.warning("DB get_channel_by_chat_id timeout/error: %s", _short(e))
        return None


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
    _remember_user_lang(user_id, lang)
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
# Increase HTTP request timeout from default 60s to 180s so 30-50 MB video
# uploads don't fail with `HTTP Client says - Request timeout error` (seen in logs).
_bot_session = AiohttpSession(timeout=180)
bot = Bot(
    token=BOT_TOKEN,
    session=_bot_session,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())


async def _send_with_retry(coro_factory, retries: int = 2):
    """Execute a Telegram send/upload call, retrying once on transient
    TelegramNetworkError / TelegramServerError (e.g. Request timeout or Bad Gateway)."""
    for attempt in range(retries):
        try:
            return await coro_factory()
        except (TelegramNetworkError, TelegramServerError) as e:
            if attempt + 1 >= retries:
                raise
            log.warning("Telegram send transient error (%s), retrying (%d/%d)...", _short(e), attempt + 1, retries)
            await asyncio.sleep(2 * (attempt + 1))


async def _custom_emoji_fallback_middleware(make_request, b: Bot, method):
    """Resilient Bot API 9.4+ middleware: if the bot owner does not have
    Telegram Premium yet or Telegram rejects a custom emoji ID on a message
    or button, automatically strip <tg-emoji> tags (keeping fallback emojis)
    and `icon_custom_emoji_id` (keeping button `style` colors!) and retry."""
    try:
        return await make_request(b, method)
    except TelegramBadRequest as e:
        err_msg = str(e).lower()
        if any(
            token in err_msg
            for token in ("custom_emoji", "document_invalid", "button_icon", "emoji_id", "can't use custom emoji")
        ):
            log.warning("Custom emoji rejected by Telegram (%s) - retrying with standard emoji fallback", e)
            for attr in ("text", "caption"):
                val = getattr(method, attr, None)
                if isinstance(val, str) and "<tg-emoji" in val:
                    object.__setattr__(method, attr, strip_tg_emoji(val))
            markup = getattr(method, "reply_markup", None)
            if markup is not None:
                rows = getattr(markup, "inline_keyboard", None) or getattr(markup, "keyboard", None)
                if rows:
                    for row in rows:
                        for btn in row:
                            cid = getattr(btn, "icon_custom_emoji_id", None)
                            extra = getattr(btn, "__pydantic_extra__", None)
                            if not cid and isinstance(extra, dict):
                                cid = extra.get("icon_custom_emoji_id")
                            if cid:
                                fb = _ID_TO_FALLBACK.get(str(cid))
                                cur_text = getattr(btn, "text", "") or ""
                                if fb and fb not in cur_text:
                                    object.__setattr__(btn, "text", f"{fb} {cur_text}".strip())
                                if getattr(btn, "icon_custom_emoji_id", None):
                                    object.__setattr__(btn, "icon_custom_emoji_id", None)
                                if isinstance(extra, dict):
                                    extra.pop("icon_custom_emoji_id", None)
            return await make_request(b, method)
        raise


bot.session.middleware(_custom_emoji_fallback_middleware)


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
    if args and isinstance(args[0], str):
        args = (strip_tg_emoji(args[0]), *args[1:])
    if isinstance(kwargs.get("text"), str):
        kwargs["text"] = strip_tg_emoji(kwargs["text"])
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
    if isinstance(exc, TelegramForbiddenError):
        # The person blocked the bot or deleted the chat - happens routinely
        # and isn't actionable, so a short line is enough; the full
        # traceback here would just bury real errors in the logs.
        log.info("update dropped: bot was blocked by the user")
        return True
    if isinstance(exc, (TelegramNetworkError, TelegramServerError)):
        # Transient Telegram/network hiccup (the request itself already
        # timed out inside aiogram's own retry logic or Telegram returned 502 Bad Gateway)
        log.warning("update dropped: Telegram network/server error (%s)", exc)
        return True
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        log.warning("update dropped: operation timed out (%s)", exc)
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
                ikb(text="🇺🇿 O'zbekcha", callback_data="lang:uz", style="success", emoji_key="flag_uz"),
                ikb(text="🇷🇺 Русский", callback_data="lang:ru", style="primary", emoji_key="flag_ru"),
                ikb(text="🇬🇧 English", callback_data="lang:en", style="primary", emoji_key="flag_gb"),
            ]
        ]
    )


def music_inline_kb(lang: str, token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                ikb(
                    text=t(lang, "detect_music_btn"),
                    callback_data=f"music:{token}",
                    style="primary",
                    emoji_key="music",
                )
            ]
        ]
    )


def build_media_caption(source_url: str) -> str:
    """"BotName | Shazam | source" line shown under a downloaded video/photo
    (see reference screenshot), before music recognition is triggered."""
    safe_bot_name = _html.escape(BOT_DISPLAY_NAME)
    bot_link = f"https://t.me/{BOT_USERNAME}?start=video" if BOT_USERNAME else ""
    bot_part = f'<a href="{bot_link}">{safe_bot_name}</a>' if bot_link else safe_bot_name
    shazam_part = '<a href="https://www.shazam.com">Shazam</a>'
    source_part = f'<a href="{source_url}">source</a>' if source_url else "source"
    return f'{ce("video")} {bot_part} | {ce("headphones")} {shazam_part} | {ce("link")} {source_part}'


def build_recognized_caption(title: str, artist: str, source_url: str) -> str:
    """Caption the video is edited to once music recognition succeeds
    (see reference screenshot): song title line, blank line, then the
    same "BotName | Shazam | source" row as before."""
    safe_title = _html.escape(title)
    safe_artist = _html.escape(artist)
    if safe_title and safe_artist:
        header = f'{ce("notes")} <b>{safe_title}</b> — {ce("user")} {safe_artist}'
    else:
        header = f'{ce("notes")} <b>{safe_title or safe_artist}</b>'
    return f"{header}\n\n{build_media_caption(source_url)}"


def recognized_song_kb(lang: str, title: str, artist: str, artist_token: str) -> InlineKeyboardMarkup:
    """Buttons shown on the video after recognition (see reference
    screenshot): a row of quick-search links, plus a "search by artist"
    button that re-runs our own song search using the recognized artist."""
    query = urllib.parse.quote(f"{artist} {title}".strip())
    rows = [
        [
            ikb(text="Google", url=f"https://www.google.com/search?q={query}", style="primary", emoji_key="globe"),
            ikb(text="YouTube Music", url=f"https://music.youtube.com/search?q={query}", style="danger", emoji_key="video"),
            ikb(text="Spotify", url=f"https://open.spotify.com/search/{query}", style="success", emoji_key="headphones"),
        ],
        [
            ikb(
                text=t(lang, "btn_artist_search"),
                callback_data=f"asearch:{artist_token}",
                style="primary",
                emoji_key="search",
            )
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_song_caption(song_link: str | None) -> str:
    """"@botusername | info" line shown under the sent MP3 (see reference
    screenshot). Tapping @botusername opens the bot and triggers /start
    via the deep-link start parameter; tapping "info" opens the song's
    source page (SoundCloud), when we have one."""
    bot_link = f"https://t.me/{BOT_USERNAME}?start=song" if BOT_USERNAME else ""
    bot_part = f'{ce("music")} <a href="{bot_link}">@{BOT_USERNAME}</a>' if bot_link else f'{ce("music")} @{BOT_USERNAME}'
    if song_link:
        return f'{bot_part} | {ce("info")} <a href="{song_link}">info</a>'
    return bot_part


def song_result_kb(lang: str, title: str, artist: str, song_link: str | None) -> InlineKeyboardMarkup:
    """
    Single row under the sent MP3 (see reference screenshot):
    - wide "Lyrics" button links out to a lyrics search page instead of
      reproducing the full copyrighted lyrics text inside the bot.
    - second button opens the song's source page.
    """
    query = f"{artist} {title}".strip() or title
    lyrics_url = "https://genius.com/search?q=" + urllib.parse.quote(query)
    row = [ikb(text=t(lang, "btn_lyrics"), url=lyrics_url, style="primary", emoji_key="lyrics")]
    if song_link:
        row.append(ikb(text=t(lang, "btn_youtube_link"), url=song_link, style="success", emoji_key="search"))
    return InlineKeyboardMarkup(inline_keyboard=[row])


def user_reply_kb(lang: str, is_admin: bool) -> ReplyKeyboardMarkup:
    row = [
        rkb(text=t(lang, "btn_lang"), style="primary", emoji_key="globe"),
        rkb(text=t(lang, "btn_help"), style="success", emoji_key="question"),
    ]
    keyboard = [row]
    if is_admin:
        keyboard.append([rkb(text=t(lang, "btn_admin"), style="danger", emoji_key="settings")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def admin_reply_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                rkb(text=t(lang, "btn_stats"), style="primary", emoji_key="stats"),
                rkb(text=t(lang, "btn_broadcast"), style="success", emoji_key="megaphone"),
            ],
            [rkb(text=t(lang, "btn_add_channel"), style="success", emoji_key="plus")],
            [rkb(text=t(lang, "btn_list_channels"), style="primary", emoji_key="list")],
            [
                rkb(text=t(lang, "btn_clear_cache"), style="danger", emoji_key="trash"),
                rkb(text=t(lang, "btn_db_export"), style="primary", emoji_key="upload"),
            ],
            [rkb(text=t(lang, "btn_db_import"), style="primary", emoji_key="download")],
            [rkb(text=t(lang, "btn_back"), style="danger", emoji_key="back")],
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
    await message.answer(f'{ce("sparkles")} <b>{_html.escape(BOT_DISPLAY_NAME)}</b> {ce("rocket")}')
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
@router.message(F.text.in_(_btn_texts("btn_lang")))
async def btn_change_lang(message: Message):
    await message.answer(t("uz", "choose_lang"), reply_markup=lang_inline_kb())


@router.message(F.text.in_(_btn_texts("btn_help")))
async def btn_help(message: Message):
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "help"))


@router.message(F.text.in_(_btn_texts("btn_admin")))
async def btn_admin_panel(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "admin_panel"), reply_markup=admin_reply_kb(lang))


@router.message(F.text.in_(_btn_texts("btn_back")))
async def btn_back_to_user(message: Message, state: FSMContext):
    await state.clear()
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "send_link"), reply_markup=user_reply_kb(lang, is_admin(message.from_user.id)))


# ============================================================
# HANDLERS: admin - stats / broadcast
# ============================================================
@router.message(F.text.in_(_btn_texts("btn_stats")))
async def btn_stats(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not is_admin(message.from_user.id):
        return
    await message.answer(t(lang, "stats", count=await count_users()))


@router.message(F.text.in_(_btn_texts("btn_broadcast")))
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
@router.message(F.text.in_(_btn_texts("btn_clear_cache")))
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
@router.message(F.text.in_(_btn_texts("btn_db_export")))
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
@router.message(F.text.in_(_btn_texts("btn_db_import")))
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
@router.message(F.text.in_(_btn_texts("btn_add_channel")))
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
            [
                ikb(
                    text=f"❌ {c['title']} ({kind}, {subs_label})",
                    callback_data=f"delchan:{c['id']}",
                    style="danger",
                    emoji_key="trash",
                )
            ]
        )
    return rows


@router.message(F.text.in_(_btn_texts("btn_list_channels")))
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
            rows.append([ikb(text=f"➕ {label}", url=url, style="primary", emoji_key="plus")])
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
    rows.append([ikb(text=t(lang, "check_sub_btn"), callback_data="checksub", style="success", emoji_key="check")])
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
        global _YOUTUBE_COOKIES_INVALIDATED, _INSTAGRAM_COOKIES_INVALIDATED, _FACEBOOK_COOKIES_INVALIDATED
        text = str(msg)
        low = text.lower()
        # Automatically disable rotated/invalid cookies at runtime so we stop
        # passing dead cookies to every subsequent yt-dlp request.
        if "cookies are no longer valid" in low:
            if "[youtube" in low and not _YOUTUBE_COOKIES_INVALIDATED:
                _YOUTUBE_COOKIES_INVALIDATED = True
                log.error(
                    "YOUTUBE_COOKIES_ROTATED: yt-dlp reported YouTube cookies are no longer valid - "
                    "auto-disabling YOUTUBE_COOKIES for this session and switching to cookieless clients."
                )
                notify_admins(
                    "youtube_cookies_rotated",
                    f'{ce("warning")} <b>YouTube cookies</b> muddati tugagan yoki brauzerda yangilangan (rotated).\n'
                    f'{ce("info")} Bot avtomatik ravishda cookiesiz rejimga o\'tdi. Yangi cookies eksport qilib YOUTUBE_COOKIES ni yangilang.',
                )
            elif "[instagram" in low and not _INSTAGRAM_COOKIES_INVALIDATED:
                _INSTAGRAM_COOKIES_INVALIDATED = True
                log.error(
                    "INSTAGRAM_COOKIES_ROTATED: yt-dlp reported Instagram cookies are no longer valid - "
                    "auto-disabling INSTAGRAM_COOKIES for this session."
                )
                notify_admins(
                    "instagram_cookies_rotated",
                    f'{ce("warning")} <b>Instagram cookies</b> eskirgan (no longer valid).\n'
                    f'{ce("info")} Bot avtomatik ravishda cookiesiz rejimga o\'tdi. INSTAGRAM_COOKIES ni yangilang.',
                )
            elif "[facebook" in low and not _FACEBOOK_COOKIES_INVALIDATED:
                _FACEBOOK_COOKIES_INVALIDATED = True
                log.error(
                    "FACEBOOK_COOKIES_ROTATED: yt-dlp reported Facebook cookies are no longer valid - "
                    "auto-disabling FACEBOOK_COOKIES for this session."
                )
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
                        f'{ce("alert")} <b>{_html.escape(self.name.capitalize())}</b> downloads muvaffaqiyatsiz bo\'lyapti '
                        f'({len(self._fails)} marta, {len({k for _, k in self._fails})} xil manba).\n'
                        f'{ce("warning")} Sabab ehtimol: JS runtime (deno/node) o\'rnatilmagan, cookie eskirgan yoki IP bloklangan.\n'
                        f'{ce("info")} Loglarda YTDLP_ENV va YOUTUBE_COOKIES_HARMFUL qatorlarini tekshiring.',
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
    "requested format is not available",  # this client returned no usable formats
    "no video formats found",
    "http error 403",                     # googlevideo URL rejected (missing PO token)
    "page needs to be reloaded",          # seen in prod: every candidate, every request
)

# Answers that are about THIS video, not about YouTube being unreachable - no
# point burning the other 2 player_client attempts on these. "video
# unavailable" moved here from _RETRYABLE_MARKERS after production logs
# showed it NEVER resolved by switching clients (always ended up exhausting
# all 3 anyway) - unlike "requested format is not available" or "page needs
# to be reloaded", which genuinely differ per client.
_DEFINITIVE_MARKERS = (
    "private video", "video is private", "has been removed", "has been terminated",
    "members-only", "members only", "not available in your country",
    "blocked it in your country", "copyright",
    "video unavailable", "video is unavailable",
    "video is not available", "this video is not available",
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
    active_yt_cookies = _active_youtube_cookies()
    if (
        last_exc is not None and YT_COOKIELESS_RETRY and active_yt_cookies and saw_bot_check
        and (deadline is None or time.monotonic() <= deadline)
    ):
        global _cookieless_hint_logged, _YOUTUBE_COOKIES_INVALIDATED
        # Try cookieless-friendly clients (android_vr and tv/mweb) rather than only clients[0]
        cookieless_candidates = [["android_vr"], ["tv"], ["mweb"], clients[0]]
        seen_c = set()
        dedup_cookieless = []
        for c in cookieless_candidates:
            k = tuple(c)
            if k not in seen_c:
                seen_c.add(k)
                dedup_cookieless.append(c)
        for idx, cl_client in enumerate(dedup_cookieless[:2]):
            if idx > 0 and deadline is not None and time.monotonic() > deadline:
                break
            ydl_opts = _build_ydl_opts_base(outdir, cl_client, cookies_file=None)
            configure(ydl_opts)
            if use_proxy:
                proxy = _pick_youtube_proxy(idx)
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
                log.warning("YouTube retry WITHOUT cookies (%s) failed too: %s", cl_client, _short(e))
            else:
                _YOUTUBE_COOKIES_INVALIDATED = True
                if not _cookieless_hint_logged:
                    _cookieless_hint_logged = True
                    log.error(
                        "YOUTUBE_COOKIES_HARMFUL: the request failed with cookies but worked WITHOUT them - "
                        "auto-disabling YOUTUBE_COOKIES for this session. Export fresh cookies from a private window "
                        "(and close it), or remove YOUTUBE_COOKIES."
                    )
                YOUTUBE_BREAKER.success()
                return result
    YOUTUBE_BREAKER.failure(key)
    _raise_ytdlp_failure(last_exc, saw_bot_check)


# Clients in yt-dlp that do not support cookies (passing cookiefile causes yt-dlp
# to skip the client or fail with a bot-check).
_COOKIELESS_ONLY_YT_CLIENTS = {"android_vr", "android", "ios", "android_sdkless"}


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
        cookies_file = _active_youtube_cookies()
        # Do not attach cookies if all requested player_clients are cookieless-only
        if player_clients and all(c in _COOKIELESS_ONLY_YT_CLIENTS for c in player_clients):
            cookies_file = None
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

_IG_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_CRAWLER_UA = "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"


def _scrape_instagram_embed(url: str, outdir: str, want_video: bool = True):
    """Fallback for Instagram posts/reels when yt-dlp fails with 'There is no
    video in this post', 'No video formats found!', or 'Instagram sent an
    empty media response'. Fetches Instagram's public `/p/<code>/embed/captioned/`
    endpoint (which works without login cookies even from datacenter IPs) and
    extracts the direct video_url or display_url/EmbeddedMediaImage."""
    m = _IG_SHORTCODE_RE.search(url or "")
    if not m:
        return None
    shortcode = m.group(1)
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        req = urllib.request.Request(
            embed_url,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        log.info("Instagram embed fetch failed for %s: %s", shortcode, _short(e))
        return None

    # 1. Try video_url in embedded JSON if want_video is True
    if want_video:
        vid_matches = re.findall(r'"video_url"\s*:\s*"([^"]+)"', html)
        for raw_vid in vid_matches:
            try:
                vid_url = raw_vid.encode("utf-8").decode("unicode_escape").replace("\/", "/")
                vid_url = _html.unescape(vid_url)
                if vid_url.startswith("http"):
                    filepath = os.path.join(outdir, f"ig_{shortcode}.mp4")
                    _http_download(vid_url, filepath, GENERAL_PROXY, max_bytes=TELEGRAM_UPLOAD_LIMIT_BYTES)
                    w, h, dur = _probe_media_dims(filepath)
                    return filepath, {"id": shortcode, "title": shortcode, "ext": "mp4", "width": w, "height": h, "duration": dur}
            except Exception as e:
                log.info("Instagram embed video download failed for %s: %s", shortcode, _short(e))

    # 2. Try EmbeddedMediaImage src or display_url in embedded JSON
    img_candidates = []
    for pat in (
        r'<img[^>]+class="[^"]*EmbeddedMediaImage[^"]*"[^>]+src="([^"]+)"',
        r'<img[^>]+src="([^"]+)"[^>]+class="[^"]*EmbeddedMediaImage[^"]*"',
        r'"display_url"\s*:\s*"([^"]+)"',
    ):
        for found in re.findall(pat, html):
            cleaned = found.encode("utf-8").decode("unicode_escape") if "\\u" in found or "\\/" in found else found
            cleaned = _html.unescape(cleaned.replace("\/", "/"))
            if cleaned.startswith("http") and cleaned not in img_candidates:
                img_candidates.append(cleaned)

    for img_url in img_candidates:
        try:
            filepath = os.path.join(outdir, f"ig_{shortcode}.jpg")
            _http_download(img_url, filepath, GENERAL_PROXY, max_bytes=40 * 1024 * 1024)
            return filepath, {"id": shortcode, "title": shortcode, "ext": "jpg", "_media_kind": "image"}
        except Exception as e:
            log.info("Instagram embed image download failed for %s: %s", shortcode, _short(e))

    return None


def _scrape_og_image(url: str, outdir: str, filename_prefix: str = "image"):
    """Last-resort image/media grab that doesn't depend on yt-dlp at all: fetch
    the page HTML directly and pull the og:image meta tag. Tries both desktop
    User-Agent and Facebook's crawler User-Agent (Instagram serves OpenGraph
    tags to crawler UAs even when serving a login shell to browsers)."""
    if "instagram.com" in (url or "").lower():
        ig_res = _scrape_instagram_embed(url, outdir, want_video=False)
        if ig_res:
            return ig_res

    img_url = None
    for ua in (DEFAULT_UA, _CRAWLER_UA):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            log.info("og:image scrape fetch (%s) failed for %s: %s", ua[:18], url, _short(e))
            continue

        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html, re.I)
        if m:
            img_url = _html.unescape(m.group(1))
            break

    if not img_url:
        return None

    last_err = None
    for cand in _pinimg_candidates(img_url):  # Pinterest: /originals/ first, given size as fallback
        try:
            ext = os.path.splitext(urllib.parse.urlparse(cand).path)[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                ext = ".jpg"
            filepath = os.path.join(outdir, f"{filename_prefix}_{uuid.uuid4().hex[:8]}{ext}")
            _http_download(cand, filepath, None, max_bytes=40 * 1024 * 1024)
            return filepath, {"id": filename_prefix, "title": filename_prefix, "ext": ext.lstrip(".")}
        except Exception as e:
            last_err = e
    log.warning("og:image scrape failed to download image %s: %s", img_url, last_err)
    return None


def _download_image_fallback(url: str, outdir: str, cookies_file: str | None = None):
    """Some posts (Instagram photo-only posts, some Pinterest image pins)
    have no video formats at all, so yt-dlp's normal video download raises
    'No video formats found!' or 'There is no video in this post'. This grabs
    the highest-res display image directly instead."""
    if "instagram.com" in (url or "").lower():
        ig_res = _scrape_instagram_embed(url, outdir, want_video=False)
        if ig_res:
            return ig_res

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

    return _scrape_og_image(url, outdir, filename_prefix="pin" if "pinterest" in url.lower() or "pin.it" in url.lower() else "image")


def _probe_video_stream(input_path: str) -> tuple[str, str]:
    """Returns (vcodec, pix_fmt) by parsing ffmpeg's own -i banner."""
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
    """Make a downloaded video play correctly on Telegram-iOS without OOM-killing
    the 1 GB Railway container.

    Fixes applied from production logs (12 OOM kills `code -9` and 1 240s timeout):
    1. Treat both `yuv420p` and `yuvj420p` (and any `h264`/`avc1` stream that isn't
       10-bit) as already iOS-compatible so it takes the instant `-c copy` remux
       path (<0.3s, ~10 MB RAM) instead of launching libx264.
    2. Skip re-encode (remux only) whenever the file is > 28 MB or process RSS is
       already > 480 MB.
    3. When a real re-encode IS needed (e.g. a small VP9/HEVC/AV1 clip), cap both
       width and height to 720x1280, cap framerate to 30fps, and use `-preset ultrafast`
       with `rc-lookahead=0:sync-lookahead=0:ref=1:bframes=0` and `-threads 1`.
       This cuts libx264 memory usage from ~450 MB down to ~45 MB and finishes 4x faster.
    """
    output_path = os.path.join(outdir, f"ios_{uuid.uuid4().hex[:8]}.mp4")
    vcodec, pix_fmt = _probe_video_stream(input_path)
    is_already_compatible = (
        vcodec in ("h264", "avc1")
        and ("10le" not in pix_fmt)
        and (pix_fmt.startswith(("yuv420", "yuvj420")) or pix_fmt == "")
    )
    size_mb = os.path.getsize(input_path) / (1024 * 1024) if os.path.exists(input_path) else 0
    rss_mb = _current_rss_mb() or 0
    MAX_REENCODE_MB = 28

    if is_already_compatible or size_mb > MAX_REENCODE_MB or rss_mb > 480:
        if not is_already_compatible:
            log.info(
                "skipping re-encode for %.1fMB non-H264 file (RSS=%.0fMB, OOM protection) - remuxing only",
                size_mb, rss_mb,
            )
        cmd = [
            FFMPEG_PATH, "-y", "-nostats", "-i", input_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        needs_reencode_slot = False
    else:
        cmd = [
            FFMPEG_PATH, "-y", "-nostats", "-i", input_path,
            "-vf", "scale='min(720,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-r", "30",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-profile:v", "main", "-level", "4.0",
            "-pix_fmt", "yuv420p",
            "-x264-params", "rc-lookahead=0:sync-lookahead=0:ref=1:bframes=0:sliced-threads=0",
            "-threads", "1",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart",
            output_path,
        ]
        needs_reencode_slot = True

    if needs_reencode_slot:
        _REENCODE_SEMAPHORE.acquire()
    try:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg iOS-normalize timed out after 90s")
    finally:
        if needs_reencode_slot:
            _REENCODE_SEMAPHORE.release()
    stderr = proc.stderr or ""
    if proc.returncode != 0 or not os.path.exists(output_path):
        killed = " (likely OOM-killed by the host - out of memory)" if proc.returncode == -9 else ""
        raise RuntimeError(f"ffmpeg iOS-normalize failed (code {proc.returncode}){killed}: {stderr[-500:]}")

    width = height = duration = 0
    # Match the LAST Video: line in stderr (the output stream dimensions after scaling)
    v_matches = re.findall(r"Video:.*?(\d{2,5})x(\d{2,5})", stderr)
    if v_matches:
        width, height = int(v_matches[-1][0]), int(v_matches[-1][1])
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m:
        h, mnt, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        duration = int(h * 3600 + mnt * 60 + s)
    return output_path, width, height, duration


def _shrink_video_to_upload_limit(input_path: str, outdir: str) -> str | None:
    """If a downloaded video exceeds TELEGRAM_UPLOAD_LIMIT_BYTES (49 MB),
    attempt a fast 1-thread ultrafast transcode at a calculated bitrate to fit
    into ~44 MB instead of discarding the download and returning an error."""
    try:
        size = os.path.getsize(input_path)
    except OSError:
        return None
    if size <= TELEGRAM_UPLOAD_LIMIT_BYTES:
        return input_path
    if size > 130 * 1024 * 1024:
        return None  # too large to transcode quickly on 1GB RAM
    rss = _current_rss_mb() or 0
    if rss > 520:
        return None
    _, _, duration = _probe_media_dims(input_path)
    if duration <= 0 or duration > 600:
        return None
    # Target ~43 MB total -> (43 * 8192) kbits / duration
    total_kbps = int((43 * 8192) / max(duration, 1))
    audio_kbps = 96
    video_kbps = max(200, min(2500, total_kbps - audio_kbps))
    shrunk_path = os.path.join(outdir, f"shrunk_{uuid.uuid4().hex[:8]}.mp4")
    cmd = [
        FFMPEG_PATH, "-y", "-nostats", "-i", input_path,
        "-vf", "scale='min(720,iw)':'min(1280,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{int(video_kbps * 1.15)}k", "-bufsize", f"{video_kbps * 2}k",
        "-pix_fmt", "yuv420p",
        "-x264-params", "rc-lookahead=0:sync-lookahead=0:ref=1:bframes=0:sliced-threads=0",
        "-threads", "1",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k", "-ar", "44100",
        "-movflags", "+faststart",
        shrunk_path,
    ]
    _REENCODE_SEMAPHORE.acquire()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as e:
        log.warning("video shrink timed out/failed: %s", _short(e))
        return None
    finally:
        _REENCODE_SEMAPHORE.release()
    if proc.returncode == 0 and os.path.exists(shrunk_path) and os.path.getsize(shrunk_path) <= TELEGRAM_UPLOAD_LIMIT_BYTES:
        log.info(
            "shrunk oversized video from %.1fMB to %.1fMB to fit Telegram 50MB limit",
            size / 1048576, os.path.getsize(shrunk_path) / 1048576,
        )
        return shrunk_path
    try:
        if os.path.exists(shrunk_path):
            os.remove(shrunk_path)
    except OSError:
        pass
    return None


_instagram_cookie_hint_logged = False

REENCODE_SLOTS = int(os.getenv("REENCODE_SLOTS", "1"))
_REENCODE_SEMAPHORE = threading.Semaphore(REENCODE_SLOTS)


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
        f'{ce("alert")} <b>Instagram</b> hamma so\'rovda bo\'sh javob qaytaryapti (cookie bilan HAM, cookiesiz HAM).\n'
        f'{ce("warning")} Ehtimol: IP bloklangan yoki rate-limit. Cookie yangilash yordam bermaydi.\n'
        f'{ce("info")} Tavsiya: PROXY_URL sozlang yoki biroz kuting.',
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
    """YouTube downloader with H.264 preference and <48 MB size-aware format
    selection so 50-100 MB 1080p60 streams aren't downloaded when a 720p/480p
    stream fits inside Telegram's 50 MB Bot API upload limit."""

    def configure(ydl_opts: dict) -> None:
        ydl_opts["format"] = (
            "bv*[vcodec^=avc1][filesize<44M]+ba[ext=m4a][filesize<5M]/"
            "bv*[vcodec^=avc1][filesize_approx<44M]+ba[ext=m4a]/"
            "bv*[vcodec^=avc1][height<=720]+ba[ext=m4a]/"
            "b[vcodec^=avc1][filesize<48M]/b[vcodec^=avc1][height<=720]/"
            "bv*[ext=mp4][filesize<44M]+ba[ext=m4a]/"
            "bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
            "bv*[vcodec^=avc1]+ba[ext=m4a]/b[vcodec^=avc1]/"
            "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best"
        )
        ydl_opts["merge_output_format"] = "mp4"

    def runner(ydl_opts: dict):
        return _execute_ytdlp_download(ydl_opts, url, outdir)

    return _yt_with_clients(url, outdir, configure, runner, use_proxy=use_proxy)


def _download_tiktok(url: str, outdir: str):
    """TikTok: never proxied, prefers H.264 (avc1) under 48 MB."""
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = (
        "bv*[vcodec^=avc1][filesize<45M]+ba[ext=m4a]/"
        "b[vcodec^=avc1][filesize<48M]/bv*[vcodec^=avc1]+ba[ext=m4a]/b[vcodec^=avc1]/"
        "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<48M]/"
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    )
    ydl_opts["merge_output_format"] = "mp4"
    return _execute_ytdlp_download(ydl_opts, url, outdir)


def _is_empty_response_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "failed to parse json" in msg
        or "expecting value" in msg
        or "empty media response" in msg
        or "cookies are no longer valid" in msg
    )


def _is_instagram_no_video_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "no video formats found" in msg
        or "there is no video in this post" in msg
        or "no video in this post" in msg
    )


def _download_instagram(url: str, outdir: str, use_proxy: bool):
    """Instagram downloader:
    1. Prefers H.264 (`vcodec^=avc1`) and `<45M` formats so Reels don't pull
       60-85 MB VP9/HEVC streams that require heavy re-encoding or exceed 50 MB.
    2. Handles both 'No video formats found!' and 'There is no video in this post'
       by falling back to `_download_image_fallback` (using Instagram's embed endpoint).
    3. Recognizes 'Instagram sent an empty media response' as a stale-cookie /
       empty-response signal, retries without cookies, and falls back to
       `_scrape_instagram_embed` if yt-dlp still fails.
    """
    global _INSTAGRAM_COOKIES_INVALIDATED
    if "/reels/audio/" in url.lower() or "/audio/" in url.lower():
        raise RuntimeError(f"UNSUPPORTED_INSTAGRAM_AUDIO_URL: {url}")

    def attempt(cookies_file: str | None):
        opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=cookies_file)
        opts["format"] = (
            "bv*[vcodec^=avc1][filesize<44M]+ba[ext=m4a]/"
            "bv*[vcodec^=avc1][filesize_approx<44M]+ba[ext=m4a]/"
            "b[vcodec^=avc1][filesize<48M]/"
            "bv*[vcodec^=avc1]+ba[ext=m4a]/b[vcodec^=avc1]/"
            "best[ext=mp4][filesize<48M]/bv*[ext=mp4][filesize<44M]+ba[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )
        opts["merge_output_format"] = "mp4"
        if use_proxy and GENERAL_PROXY:
            opts["proxy"] = GENERAL_PROXY
        return _execute_ytdlp_download(opts, url, outdir)

    active_ig_cookies = _active_instagram_cookies()
    try:
        filename, info = attempt(active_ig_cookies)
    except Exception as e:
        if _is_instagram_no_video_error(e):
            result = _download_image_fallback(url, outdir, active_ig_cookies)
            if result:
                return result
            raise
        if not _is_empty_response_error(e):
            # Even for other errors on public posts/reels, try embed fallback once
            embed_res = _scrape_instagram_embed(url, outdir, want_video=True)
            if embed_res:
                filename, info = embed_res
            else:
                _handle_instagram_hard_failure(e)
                raise
        else:
            if not active_ig_cookies:
                embed_res = _scrape_instagram_embed(url, outdir, want_video=True)
                if embed_res:
                    filename, info = embed_res
                else:
                    _note_instagram_empty_response(e, retried_without_cookies=True)
                    raise
            else:
                log.info("Instagram empty response with cookies, retrying without cookies: %s", url)
                try:
                    filename, info = attempt(None)
                except Exception as e2:
                    if _is_instagram_no_video_error(e2):
                        result = _download_image_fallback(url, outdir, None)
                        if result:
                            return result
                    embed_res = _scrape_instagram_embed(url, outdir, want_video=True)
                    if embed_res:
                        filename, info = embed_res
                    else:
                        if not _is_empty_response_error(e2):
                            _handle_instagram_hard_failure(e2)
                            raise e2
                        _note_instagram_empty_response(e2, retried_without_cookies=True)
                        raise e2
                else:
                    _INSTAGRAM_COOKIES_INVALIDATED = True
                    log.warning(
                        "INSTAGRAM_COOKIES_HARMFUL: this request failed WITH cookies but worked "
                        "WITHOUT them - auto-disabling INSTAGRAM_COOKIES for this session."
                    )

    ext = os.path.splitext(filename)[1].lower()
    if SKIP_IOS_NORMALIZE:
        return filename, info
    if ext in (".mp4", ".mov", ".mkv", ".webm"):
        try:
            norm_path, w, h, dur = _ffmpeg_normalize_for_ios(filename, outdir)
            if norm_path != filename and os.path.exists(filename):
                os.remove(filename)
            filename = norm_path
            if w and h:
                info["width"], info["height"] = w, h
            if dur:
                info["duration"] = dur
        except Exception as e:
            log.warning("iOS normalize failed, sending original file instead: %s", e)
    return filename, info


def _handle_instagram_hard_failure(exc: Exception) -> None:
    """Errors other than the empty-response pattern (rate-limit wording,
    login walls, etc.) - still worth one throttled admin alert."""
    if _is_instagram_rate_limit_error(exc):
        notify_admins(
            "instagram_rate_limit",
            f'{ce("warning")} <b>Instagram:</b> rate-limit/login-wall javoblari kelyapti.\n'
            f'{ce("info")} So\'nggi xato: <code>{_html.escape(_short(exc, 300))}</code>\n'
            f'{ce("settings")} Cookie yangilash yoki PROXY_URL sozlash kerak bo\'lishi mumkin.',
        )


# ============================================================
# PINTEREST
#
# Pinterest has three kinds of pin and yt-dlp only understands one of them:
#   video  -> yt-dlp works
#   image  -> yt-dlp raises "No video formats found!" (prod log: pin.it/4UN5zHWt5,
#             which the old code then reported as "video could not be downloaded")
#   GIF    -> not handled at all (a .gif went to send_video)
# So this reads the pin page itself, decides image / video / GIF from the pin's
# own data, and downloads exactly that. yt-dlp stays as the fallback.
# ============================================================
PINTEREST_MAX_BYTES = 60 * 1024 * 1024
TELEGRAM_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024  # sendPhoto limit; bigger goes as a document
_PIN_ID_RE = re.compile(r"/pin/(?:[^/?#]*?--)?(\d{6,})")
_PIN_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_PIN_MEDIA_HEADERS = {"User-Agent": DEFAULT_UA, "Referer": "https://www.pinterest.com/"}
_PIN_EXT_BY_CTYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
    "image/gif": ".gif", "video/mp4": ".mp4",
}
_PIN_KNOWN_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}


def _http_open(url: str, proxy: str | None = None, timeout: int = 20, headers: dict | None = None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    return opener.open(urllib.request.Request(url, headers=headers or _PIN_HEADERS), timeout=timeout)


def _http_get_text(url: str, proxy: str | None = None, timeout: int = 20) -> tuple[str, str]:
    """GET a page, following redirects (pin.it short links). Returns (final_url, text)."""
    with _http_open(url, proxy, timeout) as resp:
        raw = resp.read(8 * 1024 * 1024)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.geturl(), raw.decode(charset, errors="ignore")


def _http_download(url: str, dest: str, proxy: str | None = None,
                   max_bytes: int = PINTEREST_MAX_BYTES, timeout: int = 30) -> str:
    """Stream `url` into `dest` with a hard size cap. Returns the content type."""
    with _http_open(url, proxy, timeout, headers=_PIN_MEDIA_HEADERS) as resp:
        ctype = resp.headers.get_content_type() or ""
        length = resp.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise MediaTooLargeError(f"media is {int(length) / 1048576:.0f} MB")
        total = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise MediaTooLargeError(f"media is larger than {max_bytes // 1048576} MB")
                f.write(chunk)
    if total == 0:
        raise RuntimeError("empty download")
    if ctype.startswith("text/"):
        raise RuntimeError(f"got {ctype} instead of media")
    return ctype


def _pinterest_pin_id(text: str) -> str | None:
    m = _PIN_ID_RE.search(text or "")
    return m.group(1) if m else None


def _pin_walk(obj):
    """Yield every dict inside a JSON structure (iterative, no recursion limit)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _pinterest_json_blobs(page: str) -> list:
    blobs = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>', page, re.S | re.I
    ):
        try:
            blobs.append(json.loads(m.group(1)))
        except Exception:
            continue
    return blobs


def _pinterest_find_pin_node(blobs: list, pin_id: str) -> dict | None:
    """The page JSON also carries dozens of RELATED pins. Only the node whose
    own id equals this pin's id may be used, otherwise a related pin's video
    could be delivered instead of the one that was asked for."""
    best = None
    keys = ("images", "videos", "story_pin_data", "carousel_data")
    for blob in blobs:
        for d in _pin_walk(blob):
            if str(d.get("id")) == pin_id and any(k in d for k in keys):
                score = sum(1 for k in keys if d.get(k))
                if best is None or score > best[0]:
                    best = (score, d)
    return best[1] if best else None


def _pin_url_path(u: str) -> str:
    return urllib.parse.urlparse(u).path.lower()


def _pinimg_candidates(url: str) -> list[str]:
    """i.pinimg.com serves the same picture at many sizes; /originals/ is the
    full-resolution one. Try it first, keep the given URL as the fallback
    (the original may have a different extension and 404)."""
    out = []
    if "i.pinimg.com" in url:
        out.append(re.sub(r"(i\.pinimg\.com)/(?:\d+x\d*(?:_\w+)?|originals)/", r"\1/originals/", url, count=1))
    out.append(url)
    return list(dict.fromkeys(out))


def _pinterest_media_from_node(node: dict) -> dict | None:
    """Decide image / video / gif from one pin's own data."""
    images = node.get("images") if isinstance(node.get("images"), dict) else {}
    orig = images.get("orig") if isinstance(images.get("orig"), dict) else {}
    orig_url = orig.get("url") if isinstance(orig.get("url"), str) else None

    # GIF: Pinterest keeps the animated original as .gif (it may ALSO expose an
    # mp4 conversion under `videos`, so this check has to come before video).
    if orig_url and _pin_url_path(orig_url).endswith(".gif"):
        return {"kind": "gif", "urls": [orig_url]}

    vlist = None
    videos = node.get("videos")
    if isinstance(videos, dict) and isinstance(videos.get("video_list"), dict):
        vlist = videos["video_list"]
    if not vlist:  # idea / story pins keep video_list nested inside pages/blocks
        for d in _pin_walk(node):
            if isinstance(d.get("video_list"), dict) and d["video_list"]:
                vlist = d["video_list"]
                break
    if vlist:
        mp4, hls = [], []
        for v in vlist.values():
            if not isinstance(v, dict) or not isinstance(v.get("url"), str):
                continue
            path = _pin_url_path(v["url"])
            size = (v.get("height") or 0) * (v.get("width") or 0)
            if path.endswith(".mp4"):
                mp4.append((size, v["url"]))
            elif path.endswith(".m3u8"):
                hls.append((size, v["url"]))
        mp4.sort(reverse=True)
        hls.sort(reverse=True)
        urls = [u for _, u in mp4] + [u for _, u in hls]
        if urls:
            return {"kind": "video", "urls": urls}

    picked = orig_url
    if not picked:
        sized = [v for v in images.values() if isinstance(v, dict) and isinstance(v.get("url"), str)]
        if sized:
            picked = max(sized, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0))["url"]
    if not picked:  # carousel: first slide's image
        for d in _pin_walk(node.get("carousel_data") or {}):
            o = d.get("orig")
            if isinstance(o, dict) and isinstance(o.get("url"), str):
                picked = o["url"]
                break
    if picked:
        if _pin_url_path(picked).endswith(".gif"):
            return {"kind": "gif", "urls": [picked]}
        return {"kind": "image", "urls": _pinimg_candidates(picked)}
    return None


def _pinterest_media_from_meta(page: str, blobs: list) -> dict | None:
    """Fallback when the pin's JSON couldn't be located: og:/twitter: meta tags
    and JSON-LD always describe the MAIN pin."""
    metas: dict[str, str] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        key = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        val = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if key and val:
            metas.setdefault(key.group(1).lower(), _html.unescape(val.group(1)))

    video_urls = [
        metas[k] for k in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream")
        if metas.get(k)
    ]
    for blob in blobs:
        for d in _pin_walk(blob):
            if d.get("@type") == "VideoObject" and isinstance(d.get("contentUrl"), str):
                video_urls.append(d["contentUrl"])
    mp4 = [u for u in video_urls if _pin_url_path(u).endswith(".mp4")]
    hls = [u for u in video_urls if _pin_url_path(u).endswith(".m3u8")]
    gif = [u for u in video_urls if _pin_url_path(u).endswith(".gif")]
    if gif:
        return {"kind": "gif", "urls": gif[:1]}
    if mp4 or hls:
        return {"kind": "video", "urls": list(dict.fromkeys(mp4 + hls))}

    img = metas.get("og:image") or metas.get("twitter:image:src") or metas.get("twitter:image")
    if not img:
        return None
    if _pin_url_path(img).endswith(".gif"):
        return {"kind": "gif", "urls": [img]}
    # A video pin whose video URL we could not find must NOT be answered with
    # its thumbnail - leave that decision to the yt-dlp fallback.
    if "video_list" in page or metas.get("og:type", "").lower().startswith("video"):
        return None
    return {"kind": "image", "urls": _pinimg_candidates(img)}


def _pinterest_extract(url: str, proxy: str | None) -> tuple[str | None, dict | None, str]:
    """Returns (pin_id, media, source). `media` is {"kind", "urls"} or None."""
    final_url, page = _http_get_text(url, proxy)
    pin_id = _pinterest_pin_id(final_url) or _pinterest_pin_id(url)
    if not pin_id:
        canon = re.search(r'rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', page, re.I)
        pin_id = _pinterest_pin_id(canon.group(1)) if canon else None
    blobs = _pinterest_json_blobs(page)
    if pin_id:
        node = _pinterest_find_pin_node(blobs, pin_id)
        if node:
            media = _pinterest_media_from_node(node)
            if media:
                return pin_id, media, "page-json"
    media = _pinterest_media_from_meta(page, blobs)
    if media:
        return pin_id, media, "meta"
    if pin_id:  # last data source: Pinterest's public widget endpoint
        try:
            _, txt = _http_get_text(
                f"https://widgets.pinterest.com/v3/pidgets/pins/info/?pin_ids={pin_id}", proxy
            )
            node = _pinterest_find_pin_node([json.loads(txt)], pin_id)
            media = _pinterest_media_from_node(node) if node else None
            if media:
                return pin_id, media, "widget-api"
        except Exception as e:
            log.info("PINTEREST_WIDGET_API_FAILED pin=%s: %s", pin_id, _short(e))
    return pin_id, None, "none"


def _probe_media_dims(path: str) -> tuple[int, int, int]:
    """(width, height, duration_seconds) from ffmpeg's own banner (no ffprobe needed)."""
    try:
        proc = subprocess.run([FFMPEG_PATH, "-i", path], capture_output=True, text=True, timeout=20)
    except Exception:
        return 0, 0, 0
    err = proc.stderr or ""
    w = h = dur = 0
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)))
    return w, h, dur


def _download_hls_to_mp4(m3u8_url: str, dest: str, proxy: str | None = None,
                         max_bytes: int = PINTEREST_MAX_BYTES, deadline_seconds: int = 120) -> None:
    """HLS-only pins. The playlist is parsed and its segments are downloaded and
    joined here (MPEG-TS segments simply concatenate), then ffmpeg only does a
    plain stream-copy remux of that ONE file. ffmpeg's own HLS demuxer is
    deliberately not used: a static ffmpeg build was seen to segfault in it."""
    started = time.monotonic()
    base_url = m3u8_url
    text = _http_get_text(base_url, proxy)[1]
    if "#EXT-X-STREAM-INF" in text:  # master playlist -> the highest-bandwidth variant
        best_bw, best_uri, pending_bw = -1, None, 0
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                pending_bw = int(m.group(1)) if m else 0
            elif line and not line.startswith("#"):
                if pending_bw >= best_bw:
                    best_bw, best_uri = pending_bw, line
                pending_bw = 0
        if not best_uri:
            raise RuntimeError("HLS master playlist without variants")
        base_url = urllib.parse.urljoin(base_url, best_uri)
        text = _http_get_text(base_url, proxy)[1]

    init_uri = None
    segments = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise RuntimeError("encrypted HLS stream is not supported")
        if line.startswith("#EXT-X-MAP"):
            m = re.search(r'URI="([^"]+)"', line)
            if m:
                init_uri = urllib.parse.urljoin(base_url, m.group(1))
        elif line and not line.startswith("#"):
            segments.append(urllib.parse.urljoin(base_url, line))
    if not segments:
        raise RuntimeError("HLS playlist has no segments")
    if len(segments) > 2000:
        raise MediaTooLargeError("HLS stream has too many segments")

    raw = dest + ".hls"
    total = 0
    try:
        with open(raw, "wb") as out:
            for seg_url in ([init_uri] if init_uri else []) + segments:
                if time.monotonic() - started > deadline_seconds:
                    raise RuntimeError("HLS download took too long")
                with _http_open(seg_url, proxy, 30, headers=_PIN_MEDIA_HEADERS) as resp:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise MediaTooLargeError(f"media is larger than {max_bytes // 1048576} MB")
                        out.write(chunk)
        if total == 0:
            raise RuntimeError("HLS segments were empty")
        # Prefer the system ffmpeg for this specific remux - see
        # SYSTEM_FFMPEG_PATH's docstring (the bundled binary can segfault on
        # raw MPEG-TS input). Falls back to the bundled one if there is no
        # system ffmpeg; if THAT also fails, the pin is reported unavailable
        # rather than crashing anything.
        for exe in ([SYSTEM_FFMPEG_PATH] if SYSTEM_FFMPEG_PATH else []) + [FFMPEG_PATH]:
            proc = subprocess.run(
                [exe, "-y", "-i", raw, "-c", "copy", "-movflags", "+faststart", dest],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                break
        else:
            raise RuntimeError(f"ffmpeg remux failed (code {proc.returncode}): {(proc.stderr or '')[-300:]}")
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass


def _pinterest_fetch_media(media: dict, pin_id: str | None, outdir: str, proxy: str | None):
    """Download the first working URL of `media`. Returns (filepath, info)."""
    kind = media["kind"]
    base = f"pin_{pin_id or uuid.uuid4().hex[:8]}"
    last_exc: Exception | None = None
    too_large: Exception | None = None
    for u in media["urls"]:
        try:
            if _pin_url_path(u).endswith(".m3u8"):
                path = os.path.join(outdir, base + ".mp4")
                _download_hls_to_mp4(u, path, proxy)
            else:
                ext = os.path.splitext(_pin_url_path(u))[1]
                path = os.path.join(outdir, base + (ext if ext in _PIN_KNOWN_EXTS else ".bin"))
                ctype = _http_download(u, path, proxy)
                if ext not in _PIN_KNOWN_EXTS:
                    real = _PIN_EXT_BY_CTYPE.get(ctype)
                    if not real:
                        raise RuntimeError(f"unknown media type {ctype!r}")
                    os.replace(path, os.path.join(outdir, base + real))
                    path = os.path.join(outdir, base + real)
        except MediaTooLargeError as e:
            too_large = e  # a smaller rendition further down the list may still fit
            continue
        except Exception as e:
            last_exc = e
            log.info("PINTEREST_URL_FAILED kind=%s url=%s: %s", kind, u[:120], _short(e))
            continue

        info = {"id": pin_id or base, "title": "pinterest", "_media_kind": kind,
                "ext": os.path.splitext(path)[1].lstrip(".")}
        if kind == "video":
            width = height = duration = 0
            if not SKIP_IOS_NORMALIZE:
                try:  # near-free remux for H.264: faststart + reliable width/height
                    norm, width, height, duration = _ffmpeg_normalize_for_ios(path, outdir)
                    os.remove(path)
                    path = norm
                except Exception as e:
                    log.warning("Pinterest video normalize failed, sending as downloaded: %s", _short(e))
            if not (width and height):
                width, height, duration = _probe_media_dims(path)
            info.update(width=width, height=height, duration=duration)
        return path, info
    if too_large and not last_exc:
        raise too_large
    raise last_exc or RuntimeError("no downloadable URL for this pin")


def _pinterest_ytdlp(url: str, outdir: str, use_proxy: bool):
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=None)
    ydl_opts["format"] = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best"
    ydl_opts["merge_output_format"] = "mp4"
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY
    filename, info = _execute_ytdlp_download(ydl_opts, url, outdir)
    info["_media_kind"] = "video"
    return filename, info


def _download_pinterest(url: str, outdir: str, use_proxy: bool):
    """Pinterest, in order:
      1. read the pin page ourselves -> exact kind (image / video / GIF) and URL
      2. yt-dlp (videos), if 1 found nothing or its download failed
      3. og:image, ONLY when yt-dlp says "No video formats found" and step 1 did
         not positively identify a video (that message is what image pins
         produce; a real video pin is never answered with its thumbnail).
    """
    proxy = GENERAL_PROXY if use_proxy else None
    pin_id = media = None
    source = "none"
    try:
        pin_id, media, source = _pinterest_extract(url, proxy)
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            raise RuntimeError(f"HTTP Error {e.code}: Pinterest pin not found") from e
        log.info("PINTEREST_PAGE_HTTP_%s for %s", e.code, url)
    except Exception as e:
        log.info("PINTEREST_SCRAPE_FAILED for %s: %s", url, _short(e))

    if media:
        try:
            result = _pinterest_fetch_media(media, pin_id, outdir, proxy)
            log.info("PINTEREST_OK kind=%s via=%s pin=%s", media["kind"], source, pin_id)
            return result
        except MediaTooLargeError:
            raise
        except Exception as e:
            log.warning("PINTEREST_MEDIA_DOWNLOAD_FAILED kind=%s via=%s: %s", media["kind"], source, _short(e))

    scraped_kind = media["kind"] if media else None
    try:
        result = _pinterest_ytdlp(url, outdir, use_proxy)
        log.info("PINTEREST_OK kind=video via=yt-dlp pin=%s", pin_id)
        return result
    except Exception as e:
        msg = str(e).lower()
        no_video = "no video formats found" in msg or "requested format is not available" in msg
        if no_video and scraped_kind in (None, "image"):
            result = _download_image_fallback(url, outdir, None)
            if result:
                path, info = result
                info["_media_kind"] = "image"
                log.info("PINTEREST_OK kind=image via=og:image pin=%s", pin_id)
                return path, info
        log.warning("PINTEREST_FAILED pin=%s scraped_kind=%s: %s", pin_id, scraped_kind, _short(e))
        raise RuntimeError(f"PINTEREST_MEDIA_UNAVAILABLE: {e}") from e


def _download_facebook(url: str, outdir: str, use_proxy: bool):
    """Facebook: most videos/reels are login-walled for logged-out
    (datacenter IP) requests, so cookies matter a lot more here than for
    other platforms. Prefers a single progressive format since Facebook's
    extractor often doesn't expose separate video+audio streams to merge."""
    ydl_opts = _build_ydl_opts_base(outdir, ["web"], cookies_file=_active_facebook_cookies())
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
    if isinstance(exc, MediaTooLargeError):
        return "ERROR_TOO_LARGE"
    if "pinterest_media_unavailable" in msg or "pinterest_video_unavailable" in msg:
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
    if (
        "video unavailable" in msg
        or "video is unavailable" in msg
        or "video is not available" in msg
        or "this video is not available" in msg
        or "requested format is not available" in msg
        or "no video formats found" in msg
        or "there is no video in this post" in msg
    ):
        return "ERROR_DELETED"
    if "unsupported url" in msg or "unsupported_instagram_audio_url" in msg:
        return "ERROR_UNSUPPORTED"
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


_NOISY_TITLE_RE = re.compile(
    r"\s*(?:\([^)]*(?:official|video|audio|clip|lyrics?|music|visualizer|remaster|hd|4k|live|version|klip|premyera)[^)]*\)"
    r"|\[[^\]]*(?:official|video|audio|clip|lyrics?|music|visualizer|remaster|hd|4k|live|version|klip|premyera)[^\]]*\])",
    re.I,
)


def _clean_song_query_variants(query: str, title: str = "", performer: str = "") -> list[str]:
    """Build a deduplicated list of search query variants from a potentially
    noisy YouTube/SoundCloud title so fallback searches on SoundCloud/VK succeed."""
    candidates = []
    for raw in (f"{performer} {title}".strip(), title.strip(), query.strip()):
        if not raw:
            continue
        cleaned = _NOISY_TITLE_RE.sub("", raw)
        cleaned = re.sub(r"\b(?:official\s+music\s+video|official\s+video|official\s+audio|music\s+video|4k|hd)\b", "", cleaned, flags=re.I)
        cleaned = " ".join(cleaned.replace("|", " ").split()).strip(" -–—")
        for item in (cleaned, raw):
            if item and item not in candidates:
                candidates.append(item)
    return candidates or [query.strip()]


async def search_and_download_song(query: str, outdir: str, fallback_queries: list[str] | None = None) -> tuple[str, str]:
    """Search order: YouTube -> SoundCloud -> VK Music (VK only when
    configured). If `fallback_queries` are provided (e.g. cleaned title or
    original user search query), tries them on SoundCloud/VK before failing."""
    loop = asyncio.get_running_loop()
    queries = []
    for q in [query] + (fallback_queries or []) + _clean_song_query_variants(query):
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    result = await loop.run_in_executor(None, _run_youtube_search_download, queries[0], outdir)
    if result:
        return result
    log.info("YouTube had no usable result for '%s' - trying SoundCloud", queries[0])
    for q in queries[:3]:
        result = await loop.run_in_executor(None, _run_soundcloud_search_download, q, outdir)
        if result:
            return result
    if _vk_configured():
        log.info("SoundCloud had no usable result for '%s' - trying VK Music", queries[0])
        for q in queries[:3]:
            result = await vk_search_and_download(q, outdir)
            if result:
                return result
    else:
        log.info("SoundCloud had no usable result for '%s' (VK not configured)", queries[0])
    raise RuntimeError(f"'{query}' uchun YouTube, SoundCloud yoki VK Music'da hech narsa topilmadi")


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
        # YouTube is now the PRIMARY song source, so keep hour-long mixes and
        # live streams out of the list the user picks from: they would only
        # be rejected at download time (MAX_TRACK_SECONDS / is_live).
        dur = e.get("duration")
        if (dur and dur > MAX_TRACK_SECONDS) or e.get("live_status") in ("is_live", "is_upcoming"):
            continue
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
    """When YouTube downloads are healthy, searches YouTube -> SoundCloud -> VK.
    When YOUTUBE_BREAKER is open (YouTube downloads paused due to bot-check),
    searches SoundCloud first so the user picks directly playable SoundCloud
    tracks instead of blocked YouTube video IDs."""
    loop = asyncio.get_running_loop()
    if YOUTUBE_BREAKER.is_open():
        results = await loop.run_in_executor(None, _run_soundcloud_list_search, query, limit)
        if results:
            return results
        if _vk_configured():
            results = await _vk_list_search(query, limit)
            if results:
                return results
        return await loop.run_in_executor(None, _run_youtube_list_search, query, limit)

    results = await loop.run_in_executor(None, _run_youtube_list_search, query, limit)
    if not results:
        log.info("YouTube list search empty for '%s' - trying SoundCloud", query)
        results = await loop.run_in_executor(None, _run_soundcloud_list_search, query, limit)
    if not results and _vk_configured():
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
        f'{ce("search")} <b>{_html.escape(str(data["query"]))}</b> {ce("notes")}',
        t(lang, "search_results_range", start=start + 1, end=end, total=len(results)),
        "",
    ]
    for i, item in enumerate(page_items, start=1):
        dur = format_duration(item.get("duration"))
        views = format_count(item.get("view_count"))
        safe_title = _html.escape(str(item.get("title") or "Unknown"))
        lines.append(f'{ce("music")} <b>{i}.</b> {safe_title} — {ce("clock")} {dur} · {ce("eye")} {views}')
    text = "\n".join(lines)

    number_rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(1, len(page_items) + 1):
        row.append(
            ikb(
                text=str(i),
                callback_data=f"srch:{token}:{i}",
                style="primary",
                emoji_key="music",
            )
        )
        if len(row) == 4:
            number_rows.append(row)
            row = []
    if row:
        number_rows.append(row)

    nav_row = []
    if page > 0:
        nav_row.append(ikb(text="«", callback_data=f"srch:{token}:prev", style="primary", emoji_key="prev"))
    nav_row.append(ikb(text="✕", callback_data=f"srch:{token}:cancel", style="danger", emoji_key="cross"))
    if end < len(results):
        nav_row.append(ikb(text="»", callback_data=f"srch:{token}:next", style="success", emoji_key="next"))

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


def _extract_url_from_message(message: Message) -> str | None:
    """Extract the first URL from message.text, message.caption, or
    text_link entities. If the message carries its own playable media
    (video/audio/voice/video_note), only treat a caption URL as a link
    request when it points to a supported download platform (Instagram,
    YouTube, TikTok, Pinterest, Facebook, Snapchat) so forwarded videos
    with t.me channel links in their caption still reach handle_own_media."""
    if message.text:
        m = URL_RE.search(message.text)
        if m:
            return m.group(1)
    if message.entities:
        for ent in message.entities:
            if getattr(ent, "url", None) and URL_RE.search(ent.url):
                return ent.url

    has_playable_media = bool(
        message.video or message.voice or message.audio or message.video_note
    )
    caption_urls: list[str] = []
    if message.caption:
        caption_urls.extend(URL_RE.findall(message.caption))
    if message.caption_entities:
        for ent in message.caption_entities:
            if getattr(ent, "url", None) and URL_RE.search(ent.url):
                caption_urls.append(ent.url)
    for u in caption_urls:
        if not has_playable_media or detect_platform(u):
            return u
    return None


def _has_url(message: Message) -> bool:
    return _extract_url_from_message(message) is not None


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

    url = _extract_url_from_message(message)
    if not url:
        await message.answer(t(lang, "no_link"))
        return

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
            elif code == "ERROR_TOO_LARGE":
                await status.edit_text(t(lang, "err_media_too_large"))
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
            elif code == "ERROR_UNSUPPORTED":
                await status.edit_text(t(lang, "unsupported_link"))
            else:
                await status.edit_text(t(lang, "error"))
        return

    try:
        _ensure_within_upload_limit(filepath)
    except MediaTooLargeError as e:
        loop = asyncio.get_running_loop()
        shrunk = await loop.run_in_executor(None, _shrink_video_to_upload_limit, filepath, outdir)
        if shrunk:
            if shrunk != filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except OSError:
                    pass
            filepath = shrunk
        else:
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
    is_photo = ext in (".jpg", ".jpeg", ".png", ".webp")
    is_gif = ext == ".gif" or info.get("_media_kind") == "gif"
    # A picture / GIF has no soundtrack, so the "recognize music" button would
    # only ever answer "not recognized".
    keyboard = None if (is_photo or is_gif) else music_inline_kb(lang, token)

    try:
        if is_gif:
            try:
                await _send_with_retry(lambda: message.answer_animation(FSInputFile(filepath), caption=caption))
            except TelegramBadRequest as e:
                log.info("send_animation refused (%s), sending as document", _short(e))
                await _send_with_retry(lambda: message.answer_document(FSInputFile(filepath), caption=caption))
        elif is_photo:
            # sendPhoto: max 10 MB and strict dimension limits; anything it
            # refuses still reaches the user, uncompressed, as a document.
            if os.path.getsize(filepath) > TELEGRAM_PHOTO_LIMIT_BYTES:
                await _send_with_retry(lambda: message.answer_document(FSInputFile(filepath), caption=caption))
            else:
                try:
                    await _send_with_retry(lambda: message.answer_photo(FSInputFile(filepath), caption=caption))
                except TelegramBadRequest as e:
                    log.info("send_photo refused (%s), sending as document", _short(e))
                    await _send_with_retry(lambda: message.answer_document(FSInputFile(filepath), caption=caption))
        else:
            try:
                await _send_with_retry(
                    lambda: message.answer_video(
                        FSInputFile(filepath),
                        caption=caption,
                        reply_markup=keyboard,
                        supports_streaming=True,
                        width=info.get("width") or None,
                        height=info.get("height") or None,
                        duration=info.get("duration") or None,
                    )
                )
            except TelegramBadRequest as e:
                log.info("send_video refused (%s), sending as document", _short(e))
                await _send_with_retry(
                    lambda: message.answer_document(FSInputFile(filepath), caption=caption, reply_markup=keyboard)
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
def _is_media_document(message: Message) -> bool:
    doc = message.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    if mime.startswith(("audio/", "video/")):
        return True
    ext = os.path.splitext((doc.file_name or "").lower())[1]
    return ext in (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".mp4", ".mov", ".mkv", ".webm")


@router.message(F.video | F.voice | F.audio | F.video_note | F.func(_is_media_document))
async def handle_own_media(message: Message):
    lang = await get_user_lang(message.from_user.id)

    missing = await get_unsubscribed_channels(message.from_user.id)
    if missing:
        await message.answer(t(lang, "subscribe_required"), reply_markup=subscribe_kb(lang, missing))
        return

    media = message.video or message.voice or message.audio or message.video_note or message.document
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

        await _send_with_retry(
            lambda: message.answer_audio(
                FSInputFile(mp3_path),
                title=song["title"],
                performer=song["artist"],
                caption=build_song_caption(song_link),
                reply_markup=song_result_kb(lang, song["title"], song["artist"], song_link),
            )
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


_BOT_STATUS_PHRASES = (
    "youtube hozircha vaqtincha ishlamayapti",
    "youtube сейчас временно не работает",
    "youtube is temporarily unavailable",
    "yuklanmoqda, biroz kuting",
    "xatolik yuz berdi",
    "bu post topilmadi",
    "kechirasiz, hozircha tiktok",
)


@router.message(F.text & ~F.func(_has_url) & ~F.text.startswith("/"))
async def handle_text_search(message: Message):
    lang = await get_user_lang(message.from_user.id)

    missing = await get_unsubscribed_channels(message.from_user.id)
    if missing:
        await message.answer(t(lang, "subscribe_required"), reply_markup=subscribe_kb(lang, missing))
        return

    query = strip_tg_emoji(message.text or "").strip()
    if not query:
        return
    # Ignore bot's own error/status messages forwarded back by users or overly long blocks
    q_low = query.lower()
    if len(query) > 150 or "\n" in query or any(p in q_low for p in _BOT_STATUS_PHRASES):
        await message.answer(t(lang, "send_link"))
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
    # status/work_dir moved inside the try (below): they used to sit BEFORE it,
    # so if the person had blocked the bot (or Telegram briefly timed out) right
    # here, the exception skipped the finally entirely and pick_key was stuck
    # in _INFLIGHT_PICKS forever - silently ignoring that exact track for that
    # user on every future attempt, even after they unblocked the bot.
    work_dir = tempfile.mkdtemp(dir=DOWNLOAD_ROOT)
    try:
        status = await call.message.answer(t(lang, "downloading"))
        source = entry.get("source", "soundcloud")
        title = entry.get("title") or "Unknown"
        performer = entry.get("uploader") or ""
        # Song picks used to bypass HEAVY_JOB_SEMAPHORE, so yt-dlp + ffmpeg jobs
        # could pile up beyond the 2 slots the 1 GB plan is sized for.
        async with HeavyJobSlot(status, lang, "downloading"):
            try:
                mp3_path = await download_song_by_url(entry["url"], work_dir, source=source)
                link_for_button = entry.get("url") if source in ("soundcloud", "youtube") else None
            except MediaTooLargeError:
                raise
            except Exception as e:
                # Production logs showed 23 failures in `cb_search_action` where
                # `source == "youtube"` failed with YOUTUBE_IP_BLOCKED,
                # YOUTUBE_CIRCUIT_OPEN, or "This video is not available", and
                # previously never fell back to SoundCloud/VK. Always fall back
                # to `search_and_download_song` with cleaned title + original query!
                log.info(
                    "picked %s track failed (%s), falling back to multi-source search for '%s'",
                    source, _short(e, 140), title,
                )
                fallback_variants = _clean_song_query_variants(
                    str(data.get("query") or ""), title=title, performer=performer
                )
                primary_q = fallback_variants[0] if fallback_variants else f"{performer} {title}".strip()
                mp3_path, link_for_button = await search_and_download_song(
                    primary_q, work_dir, fallback_queries=fallback_variants[1:]
                )
        _ensure_within_upload_limit(mp3_path)
        await _send_with_retry(
            lambda: call.message.answer_audio(
                FSInputFile(mp3_path),
                title=title,
                performer=performer,
                caption=build_song_caption(link_for_button),
                reply_markup=song_result_kb(lang, title, performer, link_for_button),
            )
        )
        try:
            await status.delete()
        except Exception:
            pass
    except TelegramForbiddenError:
        # The person blocked the bot (or deleted the chat) sometime between
        # tapping the result and now - there's nowhere left to deliver
        # anything. Ordinary and frequent, not a bug: log it briefly instead
        # of the full traceback the outer dispatcher error handler would
        # otherwise print, and let `finally` below release pick_key so the
        # same track isn't silently ignored forever if they unblock later.
        log.info("song pick abandoned: user blocked the bot")
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
            async with HeavyJobSlot(status, lang, "downloading"):
                mp3_path, song_link = await search_and_download_song(
                    query, work_dir, fallback_queries=[song["title"]]
                )
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
        await _send_with_retry(
            lambda: call.message.answer_audio(
                FSInputFile(mp3_path),
                title=song["title"],
                performer=song["artist"],
                caption=build_song_caption(song_link),
                reply_markup=song_result_kb(lang, song["title"], song["artist"], song_link),
            )
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


@router.message()
async def handle_unmatched_message(message: Message):
    """Catch-all for unsupported message types (stickers, locations, contacts,
    non-media documents, etc.) so updates are gracefully acknowledged instead
    of logged as 'Update id=... is not handled'."""
    if message.chat.type != ChatType.PRIVATE:
        return
    lang = await get_user_lang(message.from_user.id)
    await message.answer(t(lang, "send_link"))


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
