# MediaSaver Telegram Bot

Instagram, YouTube, TikTok, Pinterest va Snapchat'dan media (video/rasm) yuklab beruvchi
va videodagi musiqani Shazam orqali aniqlab, uni YouTube'dan MP3 shaklida topib beruvchi bot.

## Imkoniyatlari

- `/start` bosilganda majburiy til tanlash (🇺🇿 O'zbek / 🇷🇺 Rus / 🇬🇧 Ingliz)
- Instagram, YouTube, TikTok, Pinterest, Snapchat havolalaridan media yuklab berish
- Yuklangan video ostida **"🎵 Musiqani aniqlash"** tugmasi
- Shazam (shazamio) orqali musiqani aniqlash → YouTube'dan qidirib MP3 formatida yuborish
- TikTok bloklangan hududlarda proxy ishlatish o'rniga foydalanuvchiga
  "TikTok xizmatlari hozircha ishlamayapti" degan xabar chiqadi
- Standart admin panel: `/admin` — statistika va barcha foydalanuvchilarga xabar yuborish (broadcast)

## O'rnatish

1. Python 3.10+ va **ffmpeg** o'rnatilgan bo'lishi kerak:

   ```bash
   sudo apt update && sudo apt install ffmpeg -y
   ```

2. Kutubxonalarni o'rnating:

   ```bash
   pip install -r requirements.txt
   ```

3. Muhit o'zgaruvchilarini sozlang (terminalda yoki `.env` fayl orqali):

   ```bash
   export BOT_TOKEN="123456:ABC-YourBotTokenHere"
   export ADMIN_ID="123456789"   # sizning shaxsiy Telegram ID raqamingiz
   ```

   Botni @BotFather orqali yarating va tokenni oling.
   O'z Telegram ID raqamingizni bilish uchun @userinfobot ga yozing.

4. Botni ishga tushiring:

   ```bash
   python main.py
   ```

## Fayl tuzilishi

- `main.py` — botning to'liq kodi (aiogram 3, yt-dlp, shazamio, aiosqlite)
- `requirements.txt` — kerakli kutubxonalar
- `bot.db` — SQLite baza (avtomatik yaratiladi, foydalanuvchilar va tillarni saqlaydi)
- `downloads/` — vaqtincha yuklanган fayllar papkasi (avtomatik yaratiladi)

## Eslatmalar

- Instagram va musiqa aniqlash — botning asosiy va eng barqaror ishlaydigan qismi.
- TikTok ba'zi serverlarda bloklangan bo'lishi mumkin — bunday holatda proxy ishlatilmaydi,
  aksincha foydalanuvchiga tushunarli xabar ko'rsatiladi.
- Admin panelni faqat `ADMIN_ID` da ko'rsatilgan foydalanuvchi ochishi mumkin.
- Katta fayllarni yuborishda Telegram Bot API cheklovlariga (odatiy botlar uchun ~50MB) e'tibor bering.
