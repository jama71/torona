# Media Downloader + Music Recognition Telegram Bot

Instagram, YouTube, TikTok, Pinterest va Snapchat'dan media yuklab beruvchi,
videodagi musiqani Shazam orqali aniqlab, YouTube'dan topib MP3 shaklda
yuboruvchi Telegram bot. PostgreSQL bazasi bilan ishlaydi, Railway'ga deploy
qilishga tayyor.

## Imkoniyatlar

- Birinchi `/start`da bot nomi (tokendan avtomatik aniqlanadi) + 3 tilda
  (🇺🇿 O'zbek, 🇷🇺 Rus, 🇬🇧 Ingliz) tilni majburiy tanlash
- Instagram, YouTube, TikTok, Pinterest, Snapchat havolalaridan media yuklab
  berish (`yt-dlp` orqali)
- Video ostidagi **"🎵 Musiqani aniqlash"** tugmasi orqali:
  - `ffmpeg` bilan videodan audio ajratiladi
  - `shazamio` orqali qo'shiq aniqlanadi
  - Topilgan qo'shiq YouTube'dan qidirilib, MP3 formatda yuboriladi
  - Fayllar foydalanuvchiga yuborilgach (yoki musiqa tugmasi ishlatilgach)
    darhol diskdan o'chiriladi (bepul hostingga mos, cache saqlanmaydi)
- TikTok serverda bloklangan bo'lsa (yuklab bo'lmasa), proxy ishlatishning
  o'rniga foydalanuvchiga **"TikTok xizmatlari hozircha ishlamayapti"**
  degan xabar yuboriladi
- Oddiy foydalanuvchi uchun pastki tugmalar: **🌐 Til** va **❓ Yordam**
- Admin uchun qo'shimcha **🛠 Admin panel** tugmasi, ichida:
  - 📊 Statistika
  - 📢 Xabar yuborish (broadcast)
  - ➕ Majburiy obuna qo'shish
  - 📋 Majburiy obunalar ro'yxati (❌ tugmasi bilan olib tashlash)
- **Majburiy obuna (mandatory subscription):**
  - Kanal/gurux **ochiq** bo'lsa — foydalanuvchiga oddiy public link (`t.me/username`) beriladi
  - Kanal/gurux **yopiq** bo'lsa — bot o'sha kanalga/guruhga administrator
    qilib qo'yilgach, bot o'zi maxsus **join-request** private taklif havolasi
    yaratadi; foydalanuvchi shu link orqali qo'shilish so'rovini yuborgach,
    botdan foydalanishga ruxsat beriladi (qo'shilish so'rovi yuborilgani
    yetarli, admin tomonidan tasdiqlash shart emas)
  - Foydalanuvchi hali obuna bo'lmagan bo'lsa, link yuborsa, botdan foydalanish
    to'xtatilib, obuna bo'lish kerak bo'lgan kanallar ro'yxati va "✅ Tekshirish"
    tugmasi ko'rsatiladi

## Fayllar

- `main.py` — botning to'liq kodi (bitta faylda)
- `requirements.txt` — kerakli kutubxonalar
- `Procfile` — Railway/Heroku uchun ishga tushirish buyrug'i
- `nixpacks.toml` — Railway build vaqtida `ffmpeg`ni avtomatik o'rnatadi
- `runtime.txt` — Python versiyasi
- `.env.example` — qaysi environment variable'lar kerakligi namunasi

## Railway'ga deploy qilish

1. Ushbu papkani (yoki repo'ni) Railway'ga ulang / push qiling.
2. Railway loyihasiga **PostgreSQL** plaginini qo'shing (Railway avtomatik
   `DATABASE_URL` degan environment variable yaratib beradi).
3. Loyihaning **Variables** bo'limiga faqat quyidagilarni qo'shing:

   | Variable       | Qiymat                                              |
   |----------------|------------------------------------------------------|
   | `BOT_TOKEN`    | BotFather bergan token                               |
   | `DATABASE_URL` | Railway Postgres o'zi avtomatik beradi (link qo'yasiz) |
   | `ADMIN_IDS`    | Admin(lar)ning Telegram user ID'lari, vergul bilan   |

   Boshqa hech narsa kerak emas — bot nomi tokendan o'zi aniqlanadi.

4. Railway `nixpacks.toml` fayli orqali `ffmpeg`ni avtomatik o'rnatadi va
   `Procfile`dagi `python main.py` buyrug'i bilan botni ishga tushiradi.
5. Deploy tugagach, bot avtomatik ishga tushadi (`start_polling`).

## Mahalliy (lokal) ishga tushirish

```bash
# ffmpeg o'rnatilgan bo'lishi kerak
sudo apt update && sudo apt install -y ffmpeg

pip install -r requirements.txt

export BOT_TOKEN="123456:ABC-your-telegram-bot-token"
export DATABASE_URL="postgresql://user:password@host:port/dbname"
export ADMIN_IDS="123456789,987654321"

python main.py
```

## Majburiy obuna qanday qo'shiladi

1. Admin panelda **➕ Majburiy obuna qo'shish** tugmasini bosing.
2. Botni avval o'sha kanal/guruhga **administrator** qilib qo'ying.
3. Keyin botga o'sha kanal/guruhdagi istalgan xabarni **forward** qiling,
   yoki uning `@username`'ini, yoki chat ID sini yuboring.
4. Bot avtomatik ravishda:
   - kanal **ochiq** (username bor) bo'lsa — public link (`t.me/username`) saqlaydi;
   - kanal **yopiq** (username yo'q) bo'lsa — o'zi join-request turidagi maxsus
     taklif havola yaratadi va shuni saqlaydi.
5. Ro'yxatni **📋 Majburiy obunalar** tugmasidan ko'rish va ❌ orqali
   o'chirish mumkin.

## Eslatma

- Instagram va boshqa platformalarning ba'zi private/himoyalangan postlari
  `yt-dlp` orqali yuklab bo'lmasligi mumkin — bu saytlarning cheklovlariga
  bog'liq.
- Musiqa aniqlash internetga (Shazam va YouTube qidiruviga) ulanishni talab
  qiladi.
- Bepul (free) hostingda disk tejash uchun har bir so'rovdan keyingi
  vaqtinchalik fayllar avtomatik o'chiriladi.
