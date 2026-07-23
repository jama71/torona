[file name]: main (14).py
[file content begin]
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
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
VK_TOKEN = os.getenv("VK_TOKEN", "").strip()  # <-- VK Music uchun token

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
# TRANSLATIONS
# ============================================================
TEXTS = {
    "uz": {
        "choose_lang": "Tilni tanlang / Выберите язык / Choose a language 👇",
        "welcome": (
            "Assalomu alaykum! 👋\n\n<b>{bot_name}</b> ga xush kelibsiz!\n\n"
            "Menga Instagram, TikTok, Pinterest yoki Snapchat havolasini "
            "yuboring — men videoni/mediani yuklab beraman. Video ostidagi tugma orqali "
            "esa undagi musiqani aniqlab, MP3 shaklda yuborib bera olaman 🎵"
        ),
        "send_link": "🔗 Havolani yuboring (Instagram / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Yuklanmoqda, biroz kuting...",
        "caption": "✅ Botimizdan foydalanganingiz uchun rahmat!",
        "detect_music_btn": "🎵 Musiqani aniqlash",
        "recognizing": "🎧 Musiqa aniqlanmoqda...",
        "not_recognized": "😔 Kechirasiz, bu videodagi musiqani aniqlab bo'lmadi.",
        "found_song": "🎶 Topildi: {title} — {artist}\n⏳ Yuklab olinmoqda...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_source_link": "🔍 Manbani ochish",
        "youtube_unavailable": "⚠️ Kechirasiz, YouTube'dan video yuklash xizmati vaqtincha ishlamayapti. Instagram, TikTok va boshqalar ishlayveradi.",
        "tiktok_unavailable": "⚠️ Kechirasiz, hozircha TikTok xizmatlari ishlamayapti. Birozdan so'ng qayta urinib ko'ring.",
        "unsupported_link": "❌ Bu havola qo'llab-quvvatlanmaydi. Instagram, TikTok, Pinterest yoki Snapchat havolasini yuboring.",
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
            "1) Instagram, TikTok, Pinterest yoki Snapchat havolasini yuboring.\n"
            "2) Bot mediani yuklab beradi.\n"
            "3) Video ostidagi 🎵 tugmasini bosing — bot videodagi musiqani aniqlab, "
            "MP3 shaklida yuboradi.\n"
            "4) Yoki shunchaki qo'shiq/ijrochi nomini yozib yuboring — bot SoundCloud va Deezer'dan "
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
            "Отправьте мне ссылку с Instagram, TikTok, Pinterest или Snapchat — "
            "я скачаю видео. А кнопкой под видео можно распознать музыку и получить её в MP3 🎵"
        ),
        "send_link": "🔗 Отправьте ссылку (Instagram / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Загружается, подождите...",
        "caption": "✅ Спасибо, что пользуетесь ботом!",
        "detect_music_btn": "🎵 Распознать музыку",
        "recognizing": "🎧 Распознаём музыку...",
        "not_recognized": "😔 Не удалось распознать музыку в этом видео.",
        "found_song": "🎶 Найдено: {title} — {artist}\n⏳ Загружается...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Текст песни",
        "btn_source_link": "🔍 Открыть источник",
        "youtube_unavailable": "⚠️ Извините, сервис загрузки с YouTube временно не работает. Instagram, TikTok и другие продолжают работать.",
        "tiktok_unavailable": "⚠️ Извините, сервисы TikTok сейчас не работают. Попробуйте позже.",
        "unsupported_link": "❌ Эта ссылка не поддерживается. Отправьте ссылку с Instagram, TikTok, Pinterest или Snapchat.",
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
            "1) Отправьте ссылку с Instagram, TikTok, Pinterest или Snapchat.\n"
            "2) Бот скачает медиа.\n"
            "3) Нажмите кнопку 🎵 под видео — бот распознает музыку и пришлёт её в MP3.\n"
            "4) Или просто напишите название песни/исполнителя — бот найдет на SoundCloud и Deezer "
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
            "Send me a link from Instagram, TikTok, Pinterest or Snapchat — "
            "I'll download the media for you. Use the button under the video to recognize "
            "the music in it and get it as an MP3 🎵"
        ),
        "send_link": "🔗 Send a link (Instagram / TikTok / Pinterest / Snapchat).",
        "downloading": "⏳ Downloading, please wait...",
        "caption": "✅ Thanks for using our bot!",
        "detect_music_btn": "🎵 Recognize music",
        "recognizing": "🎧 Recognizing the music...",
        "not_recognized": "😔 Sorry, couldn't recognize the music in this video.",
        "found_song": "🎶 Found: {title} — {artist}\n⏳ Downloading...",
        "song_caption": "🎵 {title} — {artist}",
        "btn_lyrics": "📜 Lyrics",
        "btn_source_link": "🔍 Open source",
        "youtube_unavailable": "⚠️ Sorry, YouTube download service is temporarily unavailable. Instagram, TikTok, etc. work as usual.",
        "tiktok_unavailable": "⚠️ Sorry, TikTok services aren't working right now. Please try again later.",
        "unsupported_link": "❌ This link isn't supported. Please send a link from Instagram, TikTok, Pinterest or Snapchat.",
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
            "1) Send a link from Instagram, TikTok, Pinterest or Snapchat.\n"
            "2) The bot downloads the media.\n"
            "3) Tap the 🎵 button under the video — the bot recognizes the music and sends it as MP3.\n"
            "4) Or just type a song/artist name — the bot will search SoundCloud and Deezer and send the "
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


def song_result_kb(lang: str, title: str, artist: str, source_url: str | None) -> InlineKeyboardMarkup:
    query = f"{artist} {title}".strip() or title
    lyrics_url = "https://genius.com/search?q=" + urllib.parse.quote(query)
    rows = [[InlineKeyboardButton(text=t(lang, "btn_lyrics"), url=lyrics_url)]]
    if source_url:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_source_link"), url=source_url)])
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
# DOWNLOAD HELPERS (Non-YouTube Media & SoundCloud/Deezer Music)
# ============================================================
def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


def _run_ytdlp_download(url: str, outdir: str, use_proxy: bool):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "format": "best",
        "http_headers": {"User-Agent": DEFAULT_UA},
        "geo_bypass": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if outdir:
        opts = ydl_opts
        opts["outtmpl"] = os.path.join(outdir, "%(id)s.%(ext)s")
    if use_proxy and GENERAL_PROXY:
        ydl_opts["proxy"] = GENERAL_PROXY

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        return filename, info


async def download_media(url: str, outdir: str, platform: str):
    loop = asyncio.get_running_loop()
    use_proxy = platform != "tiktok"
    return await loop.run_in_executor(None, _run_ytdlp_download, url, outdir, use_proxy)


# --- SoundCloud, Deezer & VK Music Helpers ---

def search_deezer_api(query: str, limit: int = 1) -> list[dict]:
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://api.deezer.com/search?q={encoded_query}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tracks = data.get("data", [])
            results = []
            for track in tracks:
                results.append({
                    "id": str(track.get("id")),
                    "title": track.get("title") or "Unknown",
                    "uploader": track.get("artist", {}).get("name", "Unknown"),
                    "duration": track.get("duration"),
                    "view_count": track.get("rank", 0),
                    "url": track.get("link")
                })
            return results
    except Exception as e:
        log.warning("Deezer API search failed: %s", e)
        return []


def search_vk_music(query: str, limit: int = 1) -> list[dict]:
    """VK Music orqali qidirish (zaxira manba)"""
    if not VK_TOKEN:
        return []
    try:
        url = "https://api.vk.com/method/audio.search"
        params = {
            "q": query,
            "count": limit,
            "access_token": VK_TOKEN,
            "v": "5.131"
        }
        req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(),
                                     headers={"User-Agent": DEFAULT_UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("response", {}).get("items", [])
            results = []
            for item in items:
                owner_id = item.get("owner_id")
                audio_id = item.get("id")
                if not owner_id or not audio_id:
                    continue
                # VK audio'ga to'g'ridan-to'g'ri link yt-dlp orqali ishlaydi
                audio_url = f"https://vk.com/audio{owner_id}_{audio_id}"
                results.append({
                    "id": str(audio_id),
                    "title": item.get("title", "Unknown"),
                    "uploader": item.get("artist", ""),
                    "duration": item.get("duration"),
                    "view_count": item.get("plays", 0),
                    "url": audio_url
                })
            return results
    except Exception as e:
        log.warning("VK Music search failed: %s", e)
        return []


def _run_music_search_download(query: str, outdir: str) -> tuple[str, str]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "http_headers": {"User-Agent": DEFAULT_UA},
        "socket_timeout": 30,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
    }
    
    # 1) SoundCloud
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch1:{query}", download=True)
            if info:
                if "entries" in info:
                    entries = [e for e in (info.get("entries") or []) if e]
                    if entries:
                        info = entries[0]
                filename = ydl.prepare_filename(info)
                web_url = info.get("webpage_url") or info.get("url", "")
                mp3_file = os.path.splitext(filename)[0] + ".mp3"
                if os.path.exists(mp3_file):
                    return mp3_file, web_url
    except Exception as e:
        log.warning("SoundCloud search download failed for query '%s': %s. Trying Deezer...", query, e)

    # 2) Deezer
    deezer_tracks = search_deezer_api(query, limit=1)
    if deezer_tracks:
        deezer_url = deezer_tracks[0]["url"]
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(deezer_url, download=True)
                filename = ydl.prepare_filename(info)
                mp3_file = os.path.splitext(filename)[0] + ".mp3"
                return mp3_file, deezer_url
        except Exception as e:
            log.warning("Deezer download failed for URL %s: %s. Trying VK...", deezer_url, e)

    # 3) VK Music (zaxira)
    vk_tracks = search_vk_music(query, limit=1)
    if not vk_tracks:
        raise RuntimeError(f"Music not found on SoundCloud, Deezer, or VK for query: {query}")
    vk_url = vk_tracks[0]["url"]
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(vk_url, download=True)
            filename = ydl.prepare_filename(info)
            mp3_file = os.path.splitext(filename)[0] + ".mp3"
            return mp3_file, vk_url
    except Exception as e:
        raise RuntimeError(f"VK download failed for URL {vk_url}: {e}")


async def search_and_download_song(query: str, outdir: str) -> tuple[str, str]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_music_search_download, query, outdir)


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


def _run_music_text_search(query: str, limit: int) -> list[dict]:
    results = []

    # 1) SoundCloud
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "skip_download": True,
            "http_headers": {"User-Agent": DEFAULT_UA},
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
            entries = [e for e in (info.get("entries") or []) if e]
            for e in entries:
                vid = e.get("id")
                results.append({
                    "id": str(vid),
                    "title": e.get("title") or "Unknown",
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "duration": e.get("duration"),
                    "view_count": e.get("view_count"),
                    "url": e.get("url") or f"https://soundcloud.com/{e.get('uploader_id')}/{e.get('id')}"
                })
    except Exception as e:
        log.warning("SoundCloud text search failed: %s", e)

    # 2) Deezer
    if len(results) < limit:
        deezer_results = search_deezer_api(query, limit=limit - len(results))
        results.extend(deezer_results)

    # 3) VK Music
    if len(results) < limit:
        vk_results = search_vk_music(query, limit=limit - len(results))
        results.extend(vk_results)

    return results[:limit]


async def text_search_music(query: str, limit: int = SEARCH_FETCH_LIMIT) -> list[dict]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run_music_text_search, query, limit)


def _run_ytdlp_download_audio_url(url: str, outdir: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "ffmpeg_location": FFMPEG_PATH,
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        "http_headers": {"User-Agent": DEFAULT_UA},
        "socket_timeout": 30,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return os.path.splitext(filename)[0] + ".mp3"


async def download_song_by_url(url: str, outdir: str) -> str:
    loop = asyncio.get_running_loop()
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
        log.warning("shazamio not installed - music recognition unavailable")
        return None
    if not audio_path or not os.path.exists(audio_path):
        log.warning("recognize_song: audio file not found: %s", audio_path)
        return None
    try:
        shazam = Shazam()
        result = await shazam.recognize(audio_path)
    except Exception as e:
        log.warning("shazamio recognition error: %s", e)
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

    # YouTube endi to'liq qo'llab-quvvatlanadi (blok olib tashlandi)
    # if platform == "youtube":
    #     await message.answer(t(lang, "youtube_unavailable"))
    #     return

    if platform == "tiktok":
        # TikTok uchun maxsus xabar, lekin yuklab olishga urinib ko'ramiz
        # Agar xohlasangiz bu yerda ham to'xtatib qo'yishingiz mumkin, lekin hozircha davom ettiramiz
        pass

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
    caption = t(lang, "caption")
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
        results = await text_search_music(query)
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
        mp3_path, source_url = await search_and_download_song(query, work_dir)
        await call.message.answer_audio(
            FSInputFile(mp3_path),
            title=song["title"],
            performer=song["artist"],
            caption=t(lang, "song_caption", title=song["title"], artist=song["artist"]),
            reply_markup=song_result_kb(lang, song["title"], song["artist"], source_url),
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
[file content end]
