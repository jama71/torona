import asyncio
import logging
import os
import re
import uuid
import shutil
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from yt_dlp import YoutubeDL

# ==============================================================
#  SOZLAMALAR (CONFIG)
# ==============================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "SIZNING_BOT_TOKENINGIZ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # o'zingizning telegram ID raqamingiz

DB_PATH = "bot.db"
DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("media-bot")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

# vaqtincha: yuklab olingan media fayllarni saqlab turish (musiqa aniqlash tugmasi uchun)
# key -> {"path": str, "user_id": int}
media_cache: dict[str, dict] = {}


class AdminStates(StatesGroup):
    waiting_broadcast = State()


# ==============================================================
#  TILLAR (I18N)
# ==============================================================
TEXTS = {
    "choose_lang": "Tilni tanlang / Выберите язык / Choose language:",
    "uz": {
        "start": (
            "👋 Salom! Men <b>MediaSaver Bot</b>man.\n\n"
            "Instagram, YouTube, TikTok, Pinterest va Snapchat'dan video/rasm yuklab beraman "
            "va videodagi musiqani aniqlab, uni MP3 shaklida topib beraman.\n\n"
            "Shunchaki menga havola (link) yuboring! 🔗"
        ),
        "lang_set": "✅ Til o'zbek tiliga o'rnatildi.",
        "send_link": "Iltimos, ijtimoiy tarmoq havolasini (link) yuboring.",
        "downloading": "⏳ Yuklab olinmoqda, biroz kuting...",
        "download_fail": "❌ Media yuklab bo'lmadi. Havolani tekshirib, qaytadan urinib ko'ring.",
        "tiktok_blocked": "⚠️ Hozircha TikTok xizmatlari ishlamayapti. Keyinroq qayta urinib ko'ring.",
        "caption": "✅ Botimizdan foydalanganingiz uchun rahmat!\n\n👇 Videodagi musiqani aniqlash uchun tugmani bosing.",
        "identify_btn": "🎵 Musiqani aniqlash",
        "identifying": "🎧 Musiqa aniqlanmoqda, kuting...",
        "not_found": "😔 Musiqa aniqlanmadi. Balki fon musiqasi yo'q yoki sifat past.",
        "found": "🎶 Topildi: <b>{title}</b> — {artist}\n⏳ MP3 tayyorlanmoqda...",
        "mp3_caption": "🎵 {title} — {artist}",
        "search_fail": "❌ Musiqa YouTube'da topilmadi.",
        "unsupported": "❌ Bu havola qo'llab-quvvatlanmaydi. Instagram, YouTube, TikTok, Pinterest yoki Snapchat havolasini yuboring.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Statistika",
        "broadcast": "📢 Xabar yuborish",
        "stats_text": "👥 Jami foydalanuvchilar: <b>{count}</b>",
        "broadcast_ask": "✍️ Barcha foydalanuvchilarga yuboriladigan xabarni kiriting:",
        "broadcast_done": "✅ Xabar {count} ta foydalanuvchiga yuborildi.",
        "no_access": "⛔ Sizda ruxsat yo'q.",
    },
    "ru": {
        "start": (
            "👋 Привет! Я <b>MediaSaver Bot</b>.\n\n"
            "Скачиваю видео/фото из Instagram, YouTube, TikTok, Pinterest и Snapchat, "
            "а также распознаю музыку из видео и отправляю её в формате MP3.\n\n"
            "Просто отправьте мне ссылку! 🔗"
        ),
        "lang_set": "✅ Язык установлен на русский.",
        "send_link": "Пожалуйста, отправьте ссылку на соцсеть.",
        "downloading": "⏳ Загрузка, подождите...",
        "download_fail": "❌ Не удалось скачать медиа. Проверьте ссылку и попробуйте снова.",
        "tiktok_blocked": "⚠️ Сервисы TikTok сейчас не работают. Попробуйте позже.",
        "caption": "✅ Спасибо, что пользуетесь нашим ботом!\n\n👇 Нажмите кнопку, чтобы распознать музыку в видео.",
        "identify_btn": "🎵 Распознать музыку",
        "identifying": "🎧 Распознаём музыку, подождите...",
        "not_found": "😔 Музыка не распознана. Возможно, нет фоновой музыки или низкое качество.",
        "found": "🎶 Найдено: <b>{title}</b> — {artist}\n⏳ Готовим MP3...",
        "mp3_caption": "🎵 {title} — {artist}",
        "search_fail": "❌ Музыка не найдена на YouTube.",
        "unsupported": "❌ Эта ссылка не поддерживается. Отправьте ссылку из Instagram, YouTube, TikTok, Pinterest или Snapchat.",
        "admin_panel": "🛠 Админ-панель",
        "stats": "📊 Статистика",
        "broadcast": "📢 Рассылка",
        "stats_text": "👥 Всего пользователей: <b>{count}</b>",
        "broadcast_ask": "✍️ Введите сообщение для рассылки всем пользователям:",
        "broadcast_done": "✅ Сообщение отправлено {count} пользователям.",
        "no_access": "⛔ У вас нет доступа.",
    },
    "en": {
        "start": (
            "👋 Hi! I'm <b>MediaSaver Bot</b>.\n\n"
            "I download videos/photos from Instagram, YouTube, TikTok, Pinterest and Snapchat, "
            "and I can recognize the music in a video and send it to you as MP3.\n\n"
            "Just send me a link! 🔗"
        ),
        "lang_set": "✅ Language set to English.",
        "send_link": "Please send a social media link.",
        "downloading": "⏳ Downloading, please wait...",
        "download_fail": "❌ Could not download media. Check the link and try again.",
        "tiktok_blocked": "⚠️ TikTok services are currently unavailable. Please try again later.",
        "caption": "✅ Thanks for using our bot!\n\n👇 Tap the button below to identify the music in this video.",
        "identify_btn": "🎵 Identify music",
        "identifying": "🎧 Identifying music, please wait...",
        "not_found": "😔 Music not recognized. Maybe there's no background music or quality is too low.",
        "found": "🎶 Found: <b>{title}</b> — {artist}\n⏳ Preparing MP3...",
        "mp3_caption": "🎵 {title} — {artist}",
        "search_fail": "❌ Song not found on YouTube.",
        "unsupported": "❌ This link is not supported. Send a link from Instagram, YouTube, TikTok, Pinterest or Snapchat.",
        "admin_panel": "🛠 Admin panel",
        "stats": "📊 Statistics",
        "broadcast": "📢 Broadcast",
        "stats_text": "👥 Total users: <b>{count}</b>",
        "broadcast_ask": "✍️ Enter the message to broadcast to all users:",
        "broadcast_done": "✅ Message sent to {count} users.",
        "no_access": "⛔ You don't have access.",
    },
}


def t(lang: str, key: str) -> str:
    return TEXTS.get(lang, TEXTS["uz"]).get(key, key)


# ==============================================================
#  DATABASE
# ==============================================================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                lang TEXT DEFAULT 'uz',
                joined_at TEXT
            )"""
        )
        await db.commit()


async def get_user_lang(user_id: int) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT lang FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def set_user_lang(user_id: int, lang: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, lang, joined_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def get_all_users() -> list[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_users_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0] if row else 0


# ==============================================================
#  YORDAMCHI FUNKSIYALAR
# ==============================================================
URL_PATTERNS = {
    "instagram": re.compile(r"instagram\.com", re.I),
    "youtube": re.compile(r"(youtube\.com|youtu\.be)", re.I),
    "tiktok": re.compile(r"tiktok\.com", re.I),
    "pinterest": re.compile(r"(pinterest\.com|pin\.it)", re.I),
    "snapchat": re.compile(r"snapchat\.com", re.I),
}


def detect_platform(url: str) -> str | None:
    for platform, pattern in URL_PATTERNS.items():
        if pattern.search(url):
            return platform
    return None


def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="lang:en"),
            ]
        ]
    )


def download_media(url: str, out_dir: str) -> str:
    """yt-dlp orqali media yuklab olish, fayl yo'lini qaytaradi."""
    file_id = str(uuid.uuid4())
    out_tmpl = os.path.join(out_dir, f"{file_id}.%(ext)s")
    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        # merge_output_format bo'lgani uchun kengaytma mp4 bo'lishi mumkin
        if not os.path.exists(filepath):
            base, _ = os.path.splitext(filepath)
            for ext in (".mp4", ".mkv", ".webm", ".jpg", ".png"):
                if os.path.exists(base + ext):
                    filepath = base + ext
                    break
        return filepath


def extract_audio_for_recognition(video_path: str, out_dir: str) -> str:
    """Shazam uchun videodan audio (m4a) ajratib olish."""
    audio_path = os.path.join(out_dir, f"{uuid.uuid4()}.m4a")
    ydl_opts = {
        "quiet": True,
    }
    # ffmpeg orqali to'g'ridan-to'g'ri konvertatsiya
    cmd = f'ffmpeg -y -i "{video_path}" -vn -acodec aac -t 40 "{audio_path}" -loglevel error'
    os.system(cmd)
    return audio_path


async def recognize_song(audio_path: str) -> tuple[str, str] | None:
    try:
        from shazamio import Shazam

        shazam = Shazam()
        result = await shazam.recognize(audio_path)
        track = result.get("track")
        if not track:
            return None
        title = track.get("title", "Unknown")
        artist = track.get("subtitle", "Unknown")
        return title, artist
    except Exception as e:
        logger.warning(f"Shazam xatolik: {e}")
        return None


def download_song_mp3(query: str, out_dir: str) -> str | None:
    """YouTube'dan qo'shiqni topib mp3 formatida yuklab olish."""
    file_id = str(uuid.uuid4())
    out_tmpl = os.path.join(out_dir, f"{file_id}.%(ext)s")
    ydl_opts = {
        "outtmpl": out_tmpl,
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1",
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }
    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if "entries" in info:
                info = info["entries"][0]
            base, _ = os.path.splitext(ydl.prepare_filename(info))
            mp3_path = base + ".mp3"
            if os.path.exists(mp3_path):
                return mp3_path
    except Exception as e:
        logger.warning(f"MP3 yuklab olishda xatolik: {e}")
    return None


def cleanup_file(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ==============================================================
#  HANDLERLAR
# ==============================================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(TEXTS["choose_lang"], reply_markup=lang_keyboard())


@dp.callback_query(F.data.startswith("lang:"))
async def cb_set_lang(call: CallbackQuery):
    lang = call.data.split(":")[1]
    await set_user_lang(call.from_user.id, lang)
    await call.message.edit_text(t(lang, "lang_set"))
    await call.message.answer(t(lang, "start"))
    await call.answer()


@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    lang = await get_user_lang(message.from_user.id) or "uz"
    if message.from_user.id != ADMIN_ID:
        await message.answer(t(lang, "no_access"))
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t(lang, "stats"), callback_data="admin:stats")],
            [InlineKeyboardButton(text=t(lang, "broadcast"), callback_data="admin:broadcast")],
        ]
    )
    await message.answer(t(lang, "admin_panel"), reply_markup=kb)


@dp.callback_query(F.data == "admin:stats")
async def cb_admin_stats(call: CallbackQuery):
    lang = await get_user_lang(call.from_user.id) or "uz"
    if call.from_user.id != ADMIN_ID:
        await call.answer(t(lang, "no_access"), show_alert=True)
        return
    count = await get_users_count()
    await call.message.answer(t(lang, "stats_text").format(count=count))
    await call.answer()


@dp.callback_query(F.data == "admin:broadcast")
async def cb_admin_broadcast(call: CallbackQuery, state: FSMContext):
    lang = await get_user_lang(call.from_user.id) or "uz"
    if call.from_user.id != ADMIN_ID:
        await call.answer(t(lang, "no_access"), show_alert=True)
        return
    await call.message.answer(t(lang, "broadcast_ask"))
    await state.set_state(AdminStates.waiting_broadcast)
    await call.answer()


@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    lang = await get_user_lang(message.from_user.id) or "uz"
    users = await get_all_users()
    sent = 0
    for uid in users:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
            sent += 1
        except Exception:
            pass
    await message.answer(t(lang, "broadcast_done").format(count=sent))
    await state.clear()


@dp.callback_query(F.data.startswith("identify:"))
async def cb_identify_music(call: CallbackQuery):
    key = call.data.split(":", 1)[1]
    lang = await get_user_lang(call.from_user.id) or "uz"
    cache = media_cache.get(key)
    if not cache or not os.path.exists(cache["path"]):
        await call.answer(t(lang, "not_found"), show_alert=True)
        return

    await call.answer()
    status_msg = await call.message.answer(t(lang, "identifying"))

    audio_path = extract_audio_for_recognition(cache["path"], DOWNLOADS_DIR)
    result = await recognize_song(audio_path)
    cleanup_file(audio_path)

    if not result:
        await status_msg.edit_text(t(lang, "not_found"))
        return

    title, artist = result
    await status_msg.edit_text(t(lang, "found").format(title=title, artist=artist))

    query = f"ytsearch1:{title} {artist} audio"
    mp3_path = await asyncio.to_thread(download_song_mp3, query, DOWNLOADS_DIR)

    if not mp3_path:
        await call.message.answer(t(lang, "search_fail"))
        return

    try:
        audio_file = FSInputFile(mp3_path)
        await call.message.answer_audio(
            audio_file,
            title=title,
            performer=artist,
            caption=t(lang, "mp3_caption").format(title=title, artist=artist),
        )
    finally:
        cleanup_file(mp3_path)


@dp.message(F.text.regexp(r"https?://\S+"))
async def handle_link(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not lang:
        await message.answer(TEXTS["choose_lang"], reply_markup=lang_keyboard())
        return

    url = message.text.strip()
    platform = detect_platform(url)

    if not platform:
        await message.answer(t(lang, "unsupported"))
        return

    status_msg = await message.answer(t(lang, "downloading"))

    try:
        filepath = await asyncio.to_thread(download_media, url, DOWNLOADS_DIR)
    except Exception as e:
        logger.warning(f"Yuklab olishda xatolik ({platform}): {e}")
        if platform == "tiktok":
            await status_msg.edit_text(t(lang, "tiktok_blocked"))
        else:
            await status_msg.edit_text(t(lang, "download_fail"))
        return

    await status_msg.delete()

    key = str(uuid.uuid4())[:16]
    media_cache[key] = {"path": filepath, "user_id": message.from_user.id}

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t(lang, "identify_btn"), callback_data=f"identify:{key}")]]
    )

    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext in (".jpg", ".jpeg", ".png", ".webp"):
            await message.answer_photo(FSInputFile(filepath), caption=t(lang, "caption"))
        else:
            await message.answer_video(
                FSInputFile(filepath), caption=t(lang, "caption"), reply_markup=kb, supports_streaming=True
            )
    except Exception as e:
        logger.warning(f"Fayl yuborishda xatolik: {e}")
        await message.answer(t(lang, "download_fail"))


@dp.message()
async def fallback(message: Message):
    lang = await get_user_lang(message.from_user.id)
    if not lang:
        await message.answer(TEXTS["choose_lang"], reply_markup=lang_keyboard())
        return
    await message.answer(t(lang, "send_link"))


# ==============================================================
#  ISHGA TUSHIRISH
# ==============================================================
async def main():
    await init_db()
    logger.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
