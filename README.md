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

## Yangilanishlar (2026-07-21)

- **YouTube cookies endi kodga o'rnatilgan (default)** — endi hech qanday
  environment variable qo'ymasangiz ham, `main.py` ichidagi
  `DEFAULT_YOUTUBE_COOKIES` degan o'zgaruvchida saqlangan cookies avtomatik
  ishlatiladi. Agar kelajakda `YOUTUBE_COOKIES` yoki `YOUTUBE_COOKIES_B64`
  environment variable qo'ysangiz, ular ustunlik qiladi. Cookie eskirsa,
  shunchaki `main.py` faylidagi `DEFAULT_YOUTUBE_COOKIES` qatorini yangi
  eksport qilingan cookies.txt matni bilan almashtiring.
- **Majburiy obuna endi faqat haqiqiy a'zolikni hisobga oladi** — avval
  yopiq kanalga shunchaki qo'shilish so'rovi yuborilgani botdan foydalanish
  uchun yetarli edi. Endi bu olib tashlandi: foydalanuvchi **kanal
  egasi/administratori tomonidan tasdiqlanib**, haqiqatan a'zo bo'lgandagina
  botdan foydalana oladi.
- **Foydalanuvchi endi istalgan xabar bilan ro'yxatga qo'shiladi** — avval
  faqat `/start` bosilganda foydalanuvchi bazaga yozilardi. Endi har qanday
  xabar yoki tugma bosilishi bilan (agar u hali bazada bo'lmasa) avtomatik
  qo'shiladi — bu eski (oldingi koddan qolgan) obunachilar uchun ham ishlaydi.
- **Musiqa yuborilgandan keyin ikkita tugma qo'shildi:**
  - **📜 Lyrics** — qo'shiq matnini qidiruv sahifasiga (Genius) olib boradi.
    Mualliflik huquqi sabab, bot to'liq qo'shiq matnini o'zi ko'rsata olmaydi
    (bu ko'plab qo'shiqlar bo'yicha ruxsatsiz tarqatish hisoblanadi) — shu
    sabab havola orqali qonuniy manbaga yo'naltiradi.
  - **🔍 YouTube'da ochish** — musiqa qaysi YouTube videosidan olinganini
    ochib beradi.

## Loglardagi xatolarga tuzatish (2026-07-19)

- **`No such file or directory: 'ffmpeg'`** — `ffmpeg` endi tizimga
  o'rnatilishiga bog'liq emas: `imageio-ffmpeg` kutubxonasi orqali o'z-o'zidan
  ishlaydigan ffmpeg binary avtomatik yuklab olinadi va shu ishlatiladi. Hech
  qanday qo'shimcha sozlash kerak emas.
- **`Sign in to confirm you're not a bot` (YouTube)** — bot endi bir nechta
  `player_client` (`android`, `ios`, `tv_embedded`, `mweb`) bilan ketma-ket
  urinib ko'radi, shu xato chiqqanda avtomatik boshqasiga o'tadi. Agar baribir
  muammo davom etsa, ixtiyoriy ravishda `COOKIES_FILE` environment variable
  orqali (Netscape formatidagi) YouTube cookies fayl yo'lini ko'rsatishingiz
  mumkin — bu eng ishonchli yechim.
- **Audiosiz media (masalan GIF)** — endi xato sifatida ko'rsatilmaydi, buning
  o'rniga "musiqa aniqlanmadi" degan oddiy xabar chiqadi.
- **"Musiqa qidirilyapti / yuklanmoqda" oraliq xabarlari** — musiqa muvaffaqiyatli
  topilib yuborilgach, bu oraliq statusi avtomatik o'chiriladi — chatda faqat
  yakuniy MP3 qoladi.

## Majburiy obuna: a'zolar sonini hisoblash

Bot har bir majburiy kanal/guruhda **administrator** bo'lgach, Telegram unga
o'sha chat bo'yicha barcha a'zolik o'zgarishlari (`chat_member` update)
haqida xabar bera boshlaydi. Bot buni kuzatib, har bir kanal uchun **botimiz
orqali qo'shilgan a'zolar sonini** DB'da saqlaydi va **📋 Majburiy obunalar**
ro'yxatida har bir kanal nomi yonida ko'rsatadi, masalan:

> ❌ Mening kanalim (yopiq kanal/gurux, 128 a'zo)

**Eslatma:** bu hisoblagich faqat kanal **botga admin qilib qo'shilgandan
keyin** sodir bo'lgan qo'shilish/chiqishlarni sanaydi — kanalga avvaldan a'zo
bo'lganlar avtomatik hisoblanmaydi (Telegram bunday tarixiy ma'lumotni
bermaydi). Ular keyingi safar chatga yozganda yoki qayta tekshiruv
("✅ Tekshirish" tugmasi) orqali a'zo deb aniqlanadi, lekin hisoblagichga
faqat haqiqiy qo'shilish/chiqish hodisasi bo'yicha qo'shiladi.

## Yangi: matn orqali musiqa qidirish (video shart emas)

Foydalanuvchi endi shunchaki qo'shiq yoki ijrochi nomini yozib yuborishi
mumkin (masalan `tame impala`) — video havolasi yuborish shart emas. Bot:

1. YouTube'dan shu nom bo'yicha qidiradi;
2. Natijalarni 8 tadan sahifalab, har biri nomi + davomiyligi + ko'rishlar
   soni bilan ko'rsatadi;
3. Har bir natija uchun 1–8 raqamli tugmalar chiqadi — bosilgan qo'shiq
   avtomatik YouTube'dan yuklab olinib, **MP3** shaklda yuboriladi;
4. Pastda ◀️ (oldingi sahifa), ❌ (bekor qilish) va ▶️ (keyingi sahifa)
   tugmalari bor.

Eslatma: yuborilgan skrinshotdagi pastki qatordagi 4 ta tugma (🎨, `BR: *`,
`? Lossless`, `Title`) boshqa (uchinchi tomon) musiqa botiga tegishli va
ularning vazifasi screenshot orqali aniq emas edi — shuning uchun ular
qo'shilmadi. Kerak bo'lsa, ularning aniq vazifasini tushuntirib bersangiz
(masalan bitrate tanlash, lossless format tanlash va h.k.), xohlagan
funksiyani alohida qo'shib beraman.

## YouTube "Sign in to confirm you're not a bot" davom etsa (2026-07-20)

Loglardan ko'rinishicha, bu xato **barcha** `player_client` variantlarida
(`android`, `ios`, `tv_embedded+web`, `mweb`) bir xilda chiqyapti. Bu shuni
bildiradiki, muammo client turida emas — **Railway serverining IP manzili
YouTube tomonidan butunlay bloklangan/shubhali deb belgilangan**. Bunday
holatda client turini almashtirish yordam bermaydi — yagona ishonchli yechim
**haqiqiy brauzer cookies**idan foydalanish.

### Cookies qanday qo'shiladi (Railway'da eng oson yo'l)

1. Kompyuteringizda YouTube'ga (oddiy, botsiz) hisobingiz bilan kiring.
2. Brauzeringizga cookies eksport qiluvchi kengaytma o'rnating, masalan
   **"Get cookies.txt LOCALLY"** (Chrome/Firefox uchun mavjud).
3. youtube.com sahifasida turib, kengaytma orqali cookies.txt faylini
   yuklab oling (Netscape formatida bo'ladi).
4. Ushbu faylning **butun matnini** nusxalab, Railway'ning **Variables**
   bo'limida yangi environment variable yarating:
   - Nomi: `YOUTUBE_COOKIES`
   - Qiymati: cookies.txt faylining to'liq matni (bir nechta qatordan iborat
     bo'ladi — Railway buni to'liq qabul qiladi)

   Agar Railway ko'p qatorli qiymatni qabul qilmasa, buning o'rniga faylni
   base64'ga o'girib, `YOUTUBE_COOKIES_B64` nomli variable sifatida
   qo'yishingiz mumkin:
   ```bash
   base64 -w0 cookies.txt
   ```
   Natijani `YOUTUBE_COOKIES_B64` qiymatiga qo'ying.

5. Deploy qilgach, bot avtomatik shu cookie'lardan foydalanadi — hech qanday
   qo'shimcha sozlash kerak emas.

**Muhim:** cookies vaqti-vaqti bilan eskiradi (odatda bir necha oyda) —
agar xato qayta paydo bo'lsa, cookies.txt faylini yangidan eksport qilib,
environment variable'ni yangilang.

Cookies qo'yilmagan holatda ham bot ishlayveradi (chunki `player_client`
fallback hali ham ba'zi hostlarda yordam beradi), lekin agar serveringiz
IP manzili YouTube tomonidan bloklangan bo'lsa, faqat cookies muammoni
butunlay hal qiladi.

### Nusxa ko'chirishda xato bo'lsa (masalan "Invalid base64" xatosi)

Agar `YOUTUBE_COOKIES_B64` qiymatini nusxalashda bir nechta belgi tushib
qolsa (uzun matnni ko'chirishda tez-tez bo'ladi), bot endi:
- avtomatik tozalaydi (bo'sh joy/qatorlarni olib tashlaydi) va padding'ni
  to'g'irlashga harakat qiladi;
- agar baribir noto'g'ri bo'lsa, xizmat **yiqilib qolmaydi** — shunchaki
  cookies'siz davom etadi va loglarga aniq sabab yozadi (masalan "cut off
  while copy-pasting - re-copy the FULL base64 string");
- muvaffaqiyatli yuklansa, loglarda "YouTube cookies loaded from ... (N
  cookie lines)" deb yozadi — shu qatorni ko'rsangiz, cookies to'g'ri
  ishlagani tasdiqlanadi.

Xato takrorlansa: cookies.txt faylini **yana bir bor to'liq** eksport
qiling va base64 qiymatini oxirigacha (kesilmasdan) nusxalab qo'ying, yoki
`YOUTUBE_COOKIES` orqali xom (raw) matn sifatida joylashtirib ko'ring.

## Eslatma

- Instagram va boshqa platformalarning ba'zi private/himoyalangan postlari
  `yt-dlp` orqali yuklab bo'lmasligi mumkin — bu saytlarning cheklovlariga
  bog'liq.
- Musiqa aniqlash internetga (Shazam va YouTube qidiruviga) ulanishni talab
  qiladi.
- Bepul (free) hostingda disk tejash uchun har bir so'rovdan keyingi
  vaqtinchalik fayllar avtomatik o'chiriladi.
