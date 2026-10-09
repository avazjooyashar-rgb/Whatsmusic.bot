import json, asyncio, sqlite3, datetime, os, re, tempfile, glob, time, hmac, hashlib, base64, uuid, subprocess, logging, html, shutil, sys, threading, array, unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse, parse_qs
import requests
import yt_dlp
from telegram import (Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          InlineQueryHandler, filters, ContextTypes)

try:  # python-telegram-bot >= 21
    from telegram import LinkPreviewOptions
    NO_PREVIEW = {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
except ImportError:  # older versions
    NO_PREVIEW = {"disable_web_page_preview": True}

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Whatsmuziccbot").lstrip("@")
ACR_HOST = os.environ.get("ACR_HOST")
ACR_KEY = os.environ.get("ACR_KEY")
ACR_SECRET = os.environ.get("ACR_SECRET")
AUDD_TOKEN = os.environ.get("AUDD_TOKEN")
ODESLI_KEY = os.environ.get("ODESLI_KEY")  # optional
ENGINE_ORDER = os.environ.get("ENGINES", "acr,audd,shazam").split(",")
# last resort when the melody is not recognized: transcribe the singing and search the words
LYRICS_ENGINE = os.environ.get("LYRICS_ENGINE", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")  # tiny = lighter, small = more accurate
MAX_SAMPLES = int(os.environ.get("MAX_SAMPLES", "5"))  # how many pieces of the audio may be tried
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")
COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_BYTES = 49 * 1024 * 1024
# aria2c (if installed) downloads with many connections at once: much faster
ARIA2 = bool(shutil.which("aria2c")) and os.environ.get("ARIA2", "1") == "1"
os.makedirs(SAMPLES_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
DIRECT_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "music.youtube.com")
CLIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com")
MP3_PREFIX = "🎵 "

esc = html.escape

# clips whose recognition + audio download are being prepared in the background
# sid -> {"task": Task, "src": url, "created": ts, "used": bool}
PREP = {}
# other possible answers for a recognized sample (used by the "wrong song" button)
# sid -> {"alts": [song, ...], "created": ts}
CANDS = {}


# ---------- Languages ----------
LANGS = {
    "fa": "🇮🇷 زبان فارسی",
    "en": "🇬🇧 English",
    "uz": "🇺🇿 O'zbek",
    "ru": "🇷🇺 Русский",
    "es": "🇪🇸 Español",
    "ar": "🇸🇦 اللغة العربية",
    "tr": "🇹🇷 Türkçe",
    "hi": "🇮🇳 हिंदी",
    "vi": "🇻🇳 Tiếng Việt",
    "uk": "🇺🇦 Українська",
    "id": "🇮🇩 Bahasa Indonesia",
    "ms": "🇲🇾 Melayu",
    "ko": "🇰🇷 한국인",
    "pt": "🇧🇷 Português",
    "zh": "🇨🇳 中國人",
    "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch",
    "it": "🇮🇹 Italiano",
    "he": "🇮🇱 עברית",
    "nl": "🇳🇱 Nederlands",
    "sv": "🇸🇪 Svenska",
}

LANG_PROMPT = "🌐 Language · زبان · Dil · اللغة · Язык"

STR = {
    "fa": {
        "welcome": (
            "🎧 <b>سلام! به بات تشخیص موسیقی خوش اومدی</b>\n\n"
            "آهنگی تو ذهنته ولی اسمش رو نمی‌دونی؟ من پیداش می‌کنم 👇\n\n"
            "🔗 <b>لینک اینستاگرام یا یوتیوب</b> بفرست؛ کلیپ رو می‌گیری و با یه دکمه آهنگش رو می‌شناسی\n"
            "🎙 <b>ویس یا فایل صوتی</b> بفرست؛ اسم آهنگ و خواننده رو می‌گم\n"
            "✍️ <b>اسم آهنگ</b> رو بنویس؛ فایلش رو برات می‌فرستم\n"
            "🔍 با دکمه‌ی جستجو، آهنگ‌های هر خواننده رو پیدا کن\n\n"
            "بعد از شناسایی، لینک Spotify و YouTube Music و متن ترانه هم همین‌جاست ✨\n\n"
            "🌐 زبان بات رو از دکمه‌های پایین عوض کن"),
        "lang_set": "✅ زبان بات شد فارسی",
        "working": "⏳ در حال پردازش...",
        "err": "❌ این فایل در دسترس نیست. یه لینک یا آهنگ دیگه امتحان کن یا چند دقیقه بعد دوباره بفرست.",
        "clip_missing": "⚠️ کلیپ پیدا نشد یا بزرگ‌تر از ۵۰ مگابایته.",
        "quota": "⛔ سقف تشخیص امروزت پر شده، فردا دوباره امتحان کن.",
        "quota_short": "⛔ سقف تشخیص امروزت پر شده.",
        "not_found": "😕 آهنگ رو نشناختم.",
        "no_more": "😕 موتور دیگه‌ای برای امتحان نمونده.",
        "expired": "نمونه منقضی شده، لینک یا فایل رو دوباره بفرست.",
        "no_result": "نتیجه‌ی دیگه‌ای نیست.",
        "identifying": "⏳ در حال شناسایی...",
        "wait_toast": "⏳ چند ثانیه صبر کن...",
        "lyrics_wait": "⏳ در حال پیدا کردن متن ترانه...",
        "lyrics_none": "😕 متن این آهنگ پیدا نشد.",
        "track_missing": "اطلاعات آهنگ پیدا نشد.",
        "btn_rec": "شناسایی موسیقی",
        "btn_artist": "جستجو بر اساس هنرمند",
        "btn_wrong": "آهنگ اشتباه",
        "btn_lyrics": "متن ترانه",
        "btn_more": "آهنگ‌های بیشتر",
    },
    "en": {
        "welcome": (
            "🎧 <b>Hi! Welcome to the music finder bot</b>\n\n"
            "Got a song stuck in your head but don't know its name? I'll find it 👇\n\n"
            "🔗 Send an <b>Instagram or YouTube link</b> — you get the clip, then identify its music with one tap\n"
            "🎙 Send a <b>voice message or audio file</b> — I'll tell you the song and the artist\n"
            "✍️ Type a <b>song name</b> — I'll send you the track\n"
            "🔍 Use the search button to browse any artist's songs\n\n"
            "After identifying, you also get Spotify and YouTube Music links and the lyrics ✨\n\n"
            "🌐 Change the bot language with the buttons below"),
        "lang_set": "✅ Language set to English",
        "working": "⏳ Working on it...",
        "err": "❌ This file isn't available right now. Try another link or song, or try again in a few minutes.",
        "clip_missing": "⚠️ Clip not found or larger than 50 MB.",
        "quota": "⛔ You've reached today's recognition limit. Try again tomorrow.",
        "quota_short": "⛔ You've reached today's recognition limit.",
        "not_found": "😕 I couldn't identify the song.",
        "no_more": "😕 No other engine left to try.",
        "expired": "This sample has expired. Please send the link or file again.",
        "no_result": "No other result.",
        "identifying": "⏳ Identifying...",
        "wait_toast": "⏳ Just a few seconds...",
        "lyrics_wait": "⏳ Looking for the lyrics...",
        "lyrics_none": "😕 Couldn't find lyrics for this song.",
        "track_missing": "Song info not found.",
        "btn_rec": "Identify music",
        "btn_artist": "Search by artist",
        "btn_wrong": "Wrong song",
        "btn_lyrics": "Lyrics",
        "btn_more": "More songs",
    },
    "tr": {
        "welcome": (
            "🎧 <b>Merhaba! Müzik tanıma botuna hoş geldin</b>\n\n"
            "Aklında bir şarkı var ama adını bilmiyor musun? Ben bulurum 👇\n\n"
            "🔗 <b>Instagram veya YouTube linki</b> gönder — klibi al, tek dokunuşla müziğini tanı\n"
            "🎙 <b>Sesli mesaj veya ses dosyası</b> gönder — şarkıyı ve sanatçıyı söyleyeyim\n"
            "✍️ <b>Şarkı adını</b> yaz — parçayı sana göndereyim\n"
            "🔍 Arama düğmesiyle bir sanatçının şarkılarına göz at\n\n"
            "Tanıdıktan sonra Spotify, YouTube Music bağlantıları ve şarkı sözleri de burada ✨\n\n"
            "🌐 Bot dilini aşağıdaki düğmelerle değiştirebilirsin"),
        "lang_set": "✅ Dil Türkçe olarak ayarlandı",
        "working": "⏳ İşleniyor...",
        "err": "❌ Bu dosya şu an kullanılamıyor. Başka bir bağlantı veya şarkı dene ya da birkaç dakika sonra tekrar dene.",
        "clip_missing": "⚠️ Klip bulunamadı veya 50 MB'dan büyük.",
        "quota": "⛔ Bugünkü tanıma hakkın doldu, yarın tekrar dene.",
        "quota_short": "⛔ Bugünkü tanıma hakkın doldu.",
        "not_found": "😕 Şarkıyı tanıyamadım.",
        "no_more": "😕 Denenecek başka motor kalmadı.",
        "expired": "Örnek süresi doldu, bağlantıyı veya dosyayı tekrar gönder.",
        "no_result": "Başka sonuç yok.",
        "identifying": "⏳ Tanınıyor...",
        "wait_toast": "⏳ Birkaç saniye...",
        "lyrics_wait": "⏳ Şarkı sözleri aranıyor...",
        "lyrics_none": "😕 Bu şarkının sözleri bulunamadı.",
        "track_missing": "Şarkı bilgisi bulunamadı.",
        "btn_rec": "Müziği tanı",
        "btn_artist": "Sanatçıya göre ara",
        "btn_wrong": "Yanlış şarkı",
        "btn_lyrics": "Şarkı sözü",
        "btn_more": "Daha fazla şarkı",
    },
    "ar": {
        "welcome": (
            "🎧 <b>مرحباً! أهلاً بك في بوت التعرّف على الموسيقى</b>\n\n"
            "لديك أغنية في بالك ولا تعرف اسمها؟ سأجدها لك 👇\n\n"
            "🔗 أرسل <b>رابط إنستغرام أو يوتيوب</b> — تحصل على المقطع وتتعرّف على موسيقاه بضغطة واحدة\n"
            "🎙 أرسل <b>رسالة صوتية أو ملفاً صوتياً</b> — سأخبرك باسم الأغنية والفنان\n"
            "✍️ اكتب <b>اسم الأغنية</b> — وسأرسل لك الملف\n"
            "🔍 استخدم زر البحث لتصفّح أغاني أي فنان\n\n"
            "بعد التعرّف تجد روابط Spotify وYouTube Music وكلمات الأغنية هنا ✨\n\n"
            "🌐 غيّر لغة البوت من الأزرار أدناه"),
        "lang_set": "✅ تم ضبط اللغة على العربية",
        "working": "⏳ جارٍ المعالجة...",
        "err": "❌ هذا الملف غير متاح حالياً. جرّب رابطاً أو أغنية أخرى، أو حاول مرة أخرى بعد قليل.",
        "clip_missing": "⚠️ لم يتم العثور على المقطع أو أنه أكبر من 50 ميغابايت.",
        "quota": "⛔ وصلت إلى حد التعرّف اليومي، حاول غداً.",
        "quota_short": "⛔ وصلت إلى حد التعرّف اليومي.",
        "not_found": "😕 لم أستطع التعرّف على الأغنية.",
        "no_more": "😕 لا توجد محركات أخرى لتجربتها.",
        "expired": "انتهت صلاحية العيّنة، أعد إرسال الرابط أو الملف.",
        "no_result": "لا توجد نتيجة أخرى.",
        "identifying": "⏳ جارٍ التعرّف...",
        "wait_toast": "⏳ بضع ثوانٍ...",
        "lyrics_wait": "⏳ جارٍ البحث عن الكلمات...",
        "lyrics_none": "😕 لم أجد كلمات هذه الأغنية.",
        "track_missing": "معلومات الأغنية غير موجودة.",
        "btn_rec": "التعرّف على الموسيقى",
        "btn_artist": "البحث عن الفنان",
        "btn_wrong": "أغنية خاطئة",
        "btn_lyrics": "كلمات الأغنية",
        "btn_more": "المزيد من الأغاني",
    },
    "ru": {
        "welcome": (
            "🎧 <b>Привет! Добро пожаловать в бот для распознавания музыки</b>\n\n"
            "Песня крутится в голове, а названия не знаешь? Я найду её 👇\n\n"
            "🔗 Отправь <b>ссылку на Instagram или YouTube</b> — получишь клип и узнаешь его музыку одним нажатием\n"
            "🎙 Отправь <b>голосовое или аудиофайл</b> — назову песню и исполнителя\n"
            "✍️ Напиши <b>название песни</b> — пришлю трек\n"
            "🔍 Кнопка поиска поможет найти песни любого исполнителя\n\n"
            "После распознавания здесь же ссылки на Spotify и YouTube Music и текст песни ✨\n\n"
            "🌐 Язык бота можно сменить кнопками ниже"),
        "lang_set": "✅ Язык бота: русский",
        "working": "⏳ Обрабатываю...",
        "err": "❌ Этот файл сейчас недоступен. Попробуй другую ссылку или песню или повтори через несколько минут.",
        "clip_missing": "⚠️ Клип не найден или больше 50 МБ.",
        "quota": "⛔ Дневной лимит распознавания исчерпан, попробуй завтра.",
        "quota_short": "⛔ Дневной лимит распознавания исчерпан.",
        "not_found": "😕 Не удалось распознать песню.",
        "no_more": "😕 Других движков для проверки не осталось.",
        "expired": "Образец устарел, отправь ссылку или файл заново.",
        "no_result": "Других результатов нет.",
        "identifying": "⏳ Распознаю...",
        "wait_toast": "⏳ Ещё несколько секунд...",
        "lyrics_wait": "⏳ Ищу текст песни...",
        "lyrics_none": "😕 Текст этой песни не найден.",
        "track_missing": "Информация о песне не найдена.",
        "btn_rec": "Узнать музыку",
        "btn_artist": "Поиск по исполнителю",
        "btn_wrong": "Не та песня",
        "btn_lyrics": "Текст песни",
        "btn_more": "Ещё песни",
    },
}

LANG_CACHE = {}


def detect_lang(code) -> str:
    c = (code or "").split("-")[0].lower()
    return c if c in LANGS else "en"


def get_lang(uid: int) -> str:
    if uid in LANG_CACHE:
        return LANG_CACHE[uid]
    with db() as c:
        r = c.execute("SELECT lang FROM users WHERE uid=?", (uid,)).fetchone()
    lang = r["lang"] if r and r["lang"] else "fa"  # old users keep Persian
    LANG_CACHE[uid] = lang
    return lang


def set_lang(uid: int, lang: str):
    with db() as c:
        c.execute("UPDATE users SET lang=? WHERE uid=?", (lang, uid))
    LANG_CACHE[uid] = lang


def tr(lang: str, key: str, **kw) -> str:
    s = STR.get(lang, STR["en"]).get(key) or STR["en"][key]
    return s.format(**kw) if kw else s


def lang_keyboard():
    btns = [Btn(label, callback_data=f"lang:{code}") for code, label in LANGS.items()]
    return Markup([btns[i:i + 2] for i in range(0, len(btns), 2)])


# ---------- Chat action (text under the bot name) ----------
@asynccontextmanager
async def chat_action(bot, chat_id, action=ChatAction.RECORD_VOICE):
    """Shows 'recording voice...' etc. under the bot name while the block runs."""
    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id, action)
            except Exception:
                pass
            await asyncio.sleep(4)  # Telegram clears it after ~5s

    t = asyncio.create_task(loop())
    try:
        yield
    finally:
        t.cancel()


# ---------- URL helpers ----------
def norm_url(url: str) -> str:
    """Clean a link so the same clip always gets the same cache key."""
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "youtu.be":
            return "https://www.youtube.com/watch?v=" + u.path.strip("/")
        if host.endswith("youtube.com"):
            if u.path == "/watch":
                v = parse_qs(u.query).get("v", [None])[0]
                if v:
                    return "https://www.youtube.com/watch?v=" + v
            if u.path.startswith("/shorts/"):
                return "https://www.youtube.com/watch?v=" + u.path.split("/")[2]
        if host.endswith("instagram.com"):
            return "https://www.instagram.com" + u.path.rstrip("/")
    except Exception:
        pass
    return url


# ---------- Song / artist matching (two singers, feat., remaster, ...) ----------
_SPLIT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_FEAT_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)", re.I)


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def title_key(t: str) -> str:
    """Same song, different spelling: '(feat. X)', '- Remastered 2011', accents... all become one key."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t or "")
    t = re.split(r"\s+[-–—]\s+", t)[0]
    return re.sub(r"[\W_]+", "", _fold(t))


def artist_set(artist: str, title: str = "") -> set:
    """Every singer of a song as a set of simple keys (includes 'feat.' singers from the title)."""
    parts = [p for p in _SPLIT_RE.split(artist or "") if p and p.strip()]
    for m in _FEAT_RE.findall(title or ""):
        parts += [p for p in _SPLIT_RE.split(m) if p and p.strip()]
    return {k for k in (re.sub(r"[\W_]+", "", _fold(p)) for p in parts) if k}


def first_artist(artist: str) -> str:
    parts = [p.strip() for p in _SPLIT_RE.split(artist or "") if p and p.strip()]
    return parts[0] if parts else (artist or "").strip()


def same_song(a: dict, b: dict) -> bool:
    ta, tb = title_key(a.get("title")), title_key(b.get("title"))
    if not ta or not tb:
        return False
    if ta != tb:
        short, long_ = sorted((ta, tb), key=len)
        if not (len(short) >= 6 and long_.startswith(short)):
            return False
    sa = artist_set(a.get("artist"), a.get("title"))
    sb = artist_set(b.get("artist"), b.get("title"))
    if not sa or not sb or (sa & sb):
        return True
    return any(x in y or y in x for x in sa for y in sb if len(x) >= 4 and len(y) >= 4)


# ---------- Engine 1: ACRCloud ----------
def acr_identify(path: str, acc):
    ts = str(int(time.time()))
    sig_str = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
    sig = base64.b64encode(
        hmac.new(acc["secret"].encode(), sig_str.encode(), hashlib.sha1).digest()
    ).decode()
    with open(path, "rb") as f:
        data = f.read()
    r = requests.post(
        f"https://{acc['host']}/v1/identify",
        data={"access_key": acc["key"], "data_type": "audio", "signature_version": "1",
              "signature": sig, "sample_bytes": len(data), "timestamp": ts},
        files={"sample": data},
        timeout=60,
    ).json()
    if r.get("status", {}).get("code") != 0:
        return None
    md = r.get("metadata") or {}
    music = md.get("music") or []
    if not music:  # e.g. only a humming / custom-file match: not a song we can name
        log.info("acr: no music match (metadata keys: %s)", list(md.keys()))
        return None
    m = music[0]
    ext = m.get("external_metadata", {})
    link = None
    yt = None
    if ext.get("spotify", {}).get("track", {}).get("id"):
        link = "https://open.spotify.com/track/" + ext["spotify"]["track"]["id"]
    if ext.get("youtube", {}).get("vid"):
        yt = "https://www.youtube.com/watch?v=" + ext["youtube"]["vid"]
        link = link or yt
    # keep EVERY singer of the track, not only the first one
    artist = ", ".join(a["name"] for a in (m.get("artists") or []) if a.get("name"))
    return {"title": m["title"], "artist": artist, "link": link, "yt": yt}


# ---------- Engine 2: AudD ----------
def audd_identify(path: str, acc):
    with open(path, "rb") as f:
        r = requests.post(
            "https://api.audd.io/",
            data={"api_token": acc["key"], "return": "spotify,apple_music"},
            files={"file": f},
            timeout=60,
        ).json()
    res = r.get("result")
    if not res:
        return None
    link = (res.get("spotify") or {}).get("external_urls", {}).get("spotify") or res.get("song_link")
    return {"title": res["title"], "artist": res["artist"], "link": link}


ENGINE_FN = {"acr": acr_identify, "audd": audd_identify}


# ---------- Odesli ----------
def odesli_to_youtube(link: str, timeout: int = 8):
    params = {"url": link}
    if ODESLI_KEY:
        params["key"] = ODESLI_KEY
    r = requests.get("https://api.song.link/v1-alpha.1/links", params=params, timeout=timeout)
    if not r.ok:
        return None, None
    j = r.json()
    plat = j.get("linksByPlatform", {})
    yt = (plat.get("youtube") or plat.get("youtubeMusic") or {}).get("url")
    ent = j.get("entitiesByUniqueId", {}).get(j.get("entityUniqueId"), {})
    name = f"{ent.get('artistName', '')} - {ent.get('title', '')}".strip(" -")
    return yt, name


def pick_target(song: dict) -> str:
    """Fastest way to the audio: known YouTube link > Odesli > search."""
    if song.get("yt"):
        return song["yt"]
    link = song.get("link") or ""
    if "youtube.com/watch" in link or "youtu.be/" in link:
        return link
    if link:
        try:
            yt, _ = odesli_to_youtube(link, timeout=3)
            if yt:
                return yt
        except Exception:
            pass
    return f"ytsearch1:{song['artist']} - {song['title']}"


# ---------- yt-dlp ----------
def ydl_base():
    o = {"quiet": True, "noplaylist": True,
         "concurrent_fragment_downloads": 8,
         "http_chunk_size": 10 * 1024 * 1024,
         "socket_timeout": 20, "retries": 3, "fragment_retries": 3}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def to_mp3(src: str) -> str:
    out = os.path.splitext(src)[0] + ".mp3"
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "libmp3lame", "-q:a", "3", out],
                   check=True, capture_output=True, timeout=300)
    try:
        os.remove(src)
    except OSError:
        pass
    return out


def download_audio(target: str, outdir: str, fallback=None):
    """Download audio as-is (m4a) without re-encoding. Only converts odd formats to mp3."""
    if not URL_RE.match(target) and not target.startswith(("ytsearch", "scsearch")):
        target = f"ytsearch1:{target}"
    plain = {**ydl_base(), "format": "bestaudio[ext=m4a]/bestaudio/best",
             "outtmpl": f"{outdir}/%(id)s.%(ext)s"}
    fast = dict(plain)
    if ARIA2:
        fast["external_downloader"] = {"default": "aria2c"}
        fast["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]}

    def run(t, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(t, download=True)
            if "entries" in info:
                if not info["entries"]:
                    raise RuntimeError("آهنگ توی SoundCloud پیدا نشد (یوتیوب هم بلاکه، کوکی لازمه)")
                info = info["entries"][0]
        return info

    def attempt(t):
        try:
            return run(t, fast)
        except Exception:
            if fast is plain or not ARIA2:
                raise
            log.warning("aria2c download failed, retrying with the normal downloader")
            for f in glob.glob(f"{outdir}/*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            return run(t, plain)

    try:
        info = attempt(target)
    except Exception as e:
        if not fallback:
            raise
        log.warning("youtube failed (%s), trying soundcloud", str(e)[:80])
        info = run(f"scsearch1:{fallback}", plain)
    files = [f for f in glob.glob(f"{outdir}/*") if not f.endswith((".part", ".ytdl", ".json", ".aria2"))]
    if not files:
        raise RuntimeError("فایل صوتی ساخته نشد")
    path = max(files, key=os.path.getmtime)
    if not path.lower().endswith((".m4a", ".mp3")):
        path = to_mp3(path)
    vid = info.get("id") if info.get("extractor_key") == "Youtube" else None
    dur = info.get("duration")
    return {"path": path, "title": info.get("title", "music"), "vid": vid,
            "url": info.get("webpage_url"),
            "duration": int(dur) if dur else None}


def download_clip(url: str, outdir: str):
    opts = {**ydl_base(),
            "format": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[height<=480]/b",
            "outtmpl": f"{outdir}/clip.%(ext)s",
            "merge_output_format": "mp4",
            "max_filesize": MAX_BYTES}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if "entries" in info:
            info = info["entries"][0]
    files = [f for f in glob.glob(f"{outdir}/clip.*") if not f.endswith(".part")]
    if not files or os.path.getsize(files[0]) > MAX_BYTES:
        return None, None, None
    meta = None
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    track = info.get("track")
    if artist and track:
        meta = {"artist": artist.strip(), "title": track}  # all singers, not only the first
    return files[0], info.get("title") or "clip", meta


def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def cut_sample(src: str, out: str, start: float, dur: int) -> bool:
    base = ["ffmpeg", "-y", "-ss", str(max(0, start)), "-i", src, "-t", str(dur),
            "-vn", "-ac", "1", "-ar", "44100"]
    for af in (["-af", "highpass=f=80,dynaudnorm=f=200:g=15"], []):
        try:
            subprocess.run(base + af + ["-b:a", "128k", out],
                           check=True, capture_output=True, timeout=120)
            return True
        except Exception:
            continue
    return False


def loud_offsets(src: str, win: int, n: int = 2) -> list:
    """Start times of the n loudest, non-overlapping windows (usually the chorus / the singing)."""
    try:
        sr = 2000
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-t", "600", "-i", src, "-vn", "-ac", "1", "-ar", str(sr),
             "-f", "s16le", "-"], capture_output=True, timeout=90).stdout
        pcm = array.array("h")
        pcm.frombytes(out[:len(out) // 2 * 2])
        secs = len(pcm) // sr
        if secs <= win + 2:
            return []
        energy = [sum(x * x for x in pcm[i * sr:(i + 1) * sr]) / sr for i in range(secs)]
        pref = [0.0]
        for v in energy:
            pref.append(pref[-1] + v)
        scores = sorted(((pref[i + win] - pref[i], i) for i in range(0, secs - win + 1)), reverse=True)
        picks = []
        for _, i in scores:
            if all(abs(i - j) >= win for j in picks):
                picks.append(i)
            if len(picks) >= n:
                break
        return picks
    except Exception as e:
        log.warning("loud_offsets failed: %s", e)
        return []


def make_samples(src: str, sid: str) -> list:
    d = probe_duration(src)
    win = 15
    if d and d > win + 3:
        loud = loud_offsets(src, win, 2)
        cand = ([loud[0]] if loud else []) + [d * 0.4 - win / 2] + loud[1:2] + [d * 0.05, d * 0.75 - win / 2]
        offs = []
        for o in cand:
            o = round(max(0, min(o, d - win)))
            if all(abs(o - x) >= 5 for x in offs):  # no near-duplicate pieces
                offs.append(o)
        offs = offs[:MAX_SAMPLES]
    else:
        offs = [0]
    jobs = [(os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3"), o) for i, o in enumerate(offs)]
    # cut all samples at the same time instead of one by one
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(lambda j: j[0] if cut_sample(src, j[0], j[1], win) else None, jobs))
    return [r for r in res if r]


def yt_search(query: str, n: int = 10):
    opts = {**ydl_base(), "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return info.get("entries") or []


SEARCH = {}  # sid -> {"items": [...], "created": ts}
PAGE = 8


def search_tracks(query: str, n: int = 30):
    """Song-like results only: no podcasts / long compilations, no duplicates."""
    out, seen = [], set()
    for it in yt_search(query, n):
        vid, title, dur = it.get("id"), it.get("title"), it.get("duration")
        if not vid or not title:
            continue
        if dur and not (60 <= dur <= 720):
            continue
        k = re.sub(r"\W+", "", title.lower())
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": vid, "title": title, "dur": int(dur) if dur else None})
    return out


def results_keyboard(sid: str, page: int, lang: str):
    items = SEARCH[sid]["items"]
    rows = []
    for it in items[page * PAGE:(page + 1) * PAGE]:
        d = f"{it['dur'] // 60}:{it['dur'] % 60:02d} · " if it["dur"] else ""
        rows.append([Btn((d + it["title"])[:60], callback_data=f"pick:{it['id']}")])
    nav = []
    if page > 0:
        nav.append(Btn("⬅️", callback_data=f"more:{sid}:{page - 1}"))
    if (page + 1) * PAGE < len(items):
        nav.append(Btn(f"➕ {tr(lang, 'btn_more')}", callback_data=f"more:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    return Markup(rows)


async def send_results(msg, query: str, lang: str):
    items = await asyncio.to_thread(search_tracks, query)
    if not items:
        return await msg.reply_text(tr(lang, "no_result"))
    sid = uuid.uuid4().hex[:8]
    SEARCH[sid] = {"items": items, "created": time.time()}
    await msg.reply_text(f"🔍 {query}", reply_markup=results_keyboard(sid, 0, lang))


def resolve(text: str):
    m = URL_RE.search(text)
    if m:
        url = m.group(0)
        if any(h in url for h in DIRECT_HOSTS):
            return url, None
        yt, name = odesli_to_youtube(url, timeout=20)
        if not yt and not name:
            raise ValueError("این لینک پشتیبانی نمیشه")
        return (yt or f"ytsearch1:{name}"), name
    return text, text


# ---------- Lyrics ----------
def clean_title(t: str) -> str:
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t or "").strip()


def get_lyrics(artist: str, title: str):
    key = f"{artist}|{title}".lower()
    with db() as c:
        r = c.execute("SELECT text FROM lyrics WHERE key=?", (key,)).fetchone()
    if r:
        return r[0]
    t = clean_title(title) or title
    a = first_artist(artist)  # "A & B" -> "A"
    text = None
    try:
        tries = []
        if a:
            tries.append({"track_name": t, "artist_name": a})
        tries.append({"q": f"{a} {t}".strip()})
        for params in tries:
            r = requests.get("https://lrclib.net/api/search", params=params, timeout=10)
            if r.ok:
                for it in r.json():
                    if it.get("plainLyrics"):
                        text = it["plainLyrics"]
                        break
            if text:
                break
    except Exception as e:
        log.warning("lrclib failed: %s", e)
    if not text and a:
        try:
            r = requests.get(f"https://api.lyrics.ovh/v1/{quote(a, safe='')}/{quote(t, safe='')}", timeout=10)
            if r.ok:
                text = r.json().get("lyrics")
        except Exception as e:
            log.warning("lyrics.ovh failed: %s", e)
    if text:
        with db() as c:
            c.execute("INSERT OR REPLACE INTO lyrics VALUES(?,?)", (key, text))
    return text


def split_text(t: str, n: int = 4000) -> list:
    out, cur = [], ""
    for line in t.splitlines(True):
        if len(cur) + len(line) > n:
            out.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        out.append(cur)
    return out


# ---------- SQLite ----------
_DB_READY = False


def db():
    global _DB_READY
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    if not _DB_READY:  # create the tables once, not on every single query
        c.execute("""CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, host TEXT, key TEXT, secret TEXT,
            enabled INTEGER DEFAULT 1, uses INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)""")
        c.execute("CREATE TABLE IF NOT EXISTS songs(key TEXT PRIMARY KEY, file_id TEXT, title TEXT, info TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS usage(uid INTEGER, day TEXT, n INTEGER, PRIMARY KEY(uid, day))")
        c.execute("CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, first_seen TEXT, lang TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
        c.execute("""CREATE TABLE IF NOT EXISTS samples(
            id TEXT PRIMARY KEY, path TEXT, tried TEXT DEFAULT '', last TEXT, created INTEGER, src TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS clips(url TEXT PRIMARY KEY, file_id TEXT, song TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, title TEXT, artist TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS lyrics(key TEXT PRIMARY KEY, text TEXT)")
        c.commit()
        _DB_READY = True
    return c


def migrate():
    """Add new columns to an old bot.db (safe to run every start)."""
    with db() as c:
        for table, col in (("songs", "info"), ("samples", "src"), ("users", "lang")):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")


def seed_from_env():
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            return
        if all([ACR_HOST, ACR_KEY, ACR_SECRET]):
            c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", (ACR_HOST, ACR_KEY, ACR_SECRET))
        if AUDD_TOKEN:
            c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (AUDD_TOKEN,))


def get_setting(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_setting(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))


def daily_limit() -> int:
    return int(get_setting("daily_limit", DAILY_LIMIT))


def touch_user(u):
    """u is a telegram User. New users get the language of their Telegram app."""
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid, first_seen, lang) VALUES(?,?,?)",
                  (u.id, datetime.date.today().isoformat(), detect_lang(u.language_code)))


# audio cache
def cache_get(key):
    with db() as c:
        return c.execute("SELECT file_id, title, info FROM songs WHERE key=?", (key,)).fetchone()


def cache_put(key, file_id, title, info=None):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO songs(key,file_id,title,info) VALUES(?,?,?,?)",
                  (key, file_id, title, info))


# clip cache (video file_id + recognized song)
def clip_get(url):
    with db() as c:
        return c.execute("SELECT file_id, song FROM clips WHERE url=? AND file_id IS NOT NULL AND song IS NOT NULL",
                         (url,)).fetchone()


def clip_put(url, file_id, song=None):
    """Upsert: a missing file_id or song never erases the one already stored."""
    data = None
    if song:
        data = json.dumps({k: song.get(k) for k in ("title", "artist", "link", "yt")}, ensure_ascii=False)
    with db() as c:
        c.execute("""INSERT INTO clips(url,file_id,song) VALUES(?,?,?)
                     ON CONFLICT(url) DO UPDATE SET
                       file_id=COALESCE(excluded.file_id, clips.file_id),
                       song=COALESCE(excluded.song, clips.song)""", (url, file_id, data))


def clip_set_song(url, song):
    clip_put(url, None, song)


def clip_delete(url):
    with db() as c:
        c.execute("DELETE FROM clips WHERE url=?", (url,))


# tracks (short id for callback buttons)
def track_put(song: dict) -> str:
    title, artist = song.get("title") or "", song.get("artist") or ""
    tid = hashlib.md5(f"{artist}|{title}".lower().encode()).hexdigest()[:10]
    with db() as c:
        c.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?)", (tid, title, artist))
    return tid


def use_quota(uid: int) -> bool:
    limit = daily_limit()
    if limit <= 0 or uid in ADMIN_IDS:
        return True
    day = datetime.date.today().isoformat()
    with db() as c:
        row = c.execute("SELECT n FROM usage WHERE uid=? AND day=?", (uid, day)).fetchone()
        n = row[0] if row else 0
        if n >= limit:
            return False
        c.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?)", (uid, day, n + 1))
    return True


def add_sample(sid: str, path: str, src=None):
    with db() as c:
        c.execute("INSERT INTO samples(id,path,src,created) VALUES(?,?,?,?)", (sid, path, src, int(time.time())))


def set_last(sid: str, song: dict):
    """Remember which engine + which song was shown last (the 'wrong song' button needs both)."""
    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}"
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (val, sid))


def remember_alts(sid: str, song: dict):
    """Keep the runner-up answers in memory so 'wrong song' can show the next one instantly."""
    CANDS[sid] = {"alts": song.pop("alts", None) or [], "created": time.time()}


def cleanup_samples():
    cutoff = int(time.time()) - 86400
    with db() as c:
        rows = c.execute("SELECT id FROM samples WHERE created<?", (cutoff,)).fetchall()
        for r in rows:
            for p in glob.glob(os.path.join(SAMPLES_DIR, r["id"] + "*")):
                try:
                    os.remove(p)
                except OSError:
                    pass
        c.execute("DELETE FROM samples WHERE created<?", (cutoff,))


# ---------- Background preparation bookkeeping ----------
def drop_entry(sid: str):
    """Forget a prepared clip and delete its downloaded audio."""
    e = PREP.pop(sid, None)
    if not e:
        return
    t = e["task"]
    if not t.done():
        t.cancel()
        return
    if t.cancelled():
        return
    try:
        a = (t.result() or {}).get("audio")
        if a and a.get("dir"):
            shutil.rmtree(a["dir"], ignore_errors=True)
    except Exception:
        pass


def cleanup_prep():
    now = time.time()
    for sid in [s for s, e in PREP.items() if now - e["created"] > 3 * 3600]:
        drop_entry(sid)
    for sid in [s for s, e in SEARCH.items() if now - e["created"] > 3 * 3600]:
        SEARCH.pop(sid, None)
    for sid in [s for s, e in CANDS.items() if now - e["created"] > 24 * 3600]:
        CANDS.pop(sid, None)


# ---------- Recognition ----------
def shazam_identify(path: str):
    try:
        from shazamio import Shazam
    except Exception:
        return None

    async def go():
        sh = Shazam()
        fn = getattr(sh, "recognize", None) or getattr(sh, "recognize_song")
        return await fn(path)

    out = asyncio.run(go())
    t = (out or {}).get("track")
    if not t:
        return None
    return {"title": t.get("title", ""), "artist": t.get("subtitle", ""), "link": None}


ENGINE_W = {"acr": 1.0, "shazam": 1.0, "audd": 0.9, "lyrics": 1.2}


def _base(tag: str) -> str:
    return (tag or "").split("#")[0]


def _active_engines(skip) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY id").fetchall()
    jobs = []
    for name in [n.strip() for n in ENGINE_ORDER]:
        if name == "shazam":
            if "shazam#0" not in skip:
                jobs.append(("shazam", []))
        elif name in ENGINE_FN:
            accs = [r for r in rows if r["type"] == name and f'{name}#{r["id"]}' not in skip]
            if accs:
                jobs.append((name, accs))
    return jobs


def call_engine(name: str, accs, path: str):
    """Ask ONE engine about ONE piece of audio. Returns a song dict or None."""
    if name == "shazam":
        try:
            res = shazam_identify(path)
        except Exception as e:
            log.warning("shazam failed: %s", e)
            return None
        if res and res.get("title"):
            res["engine"] = "shazam#0"
            return res
        return None
    for r in accs:
        tag = f'{name}#{r["id"]}'
        try:
            res = ENGINE_FN[name](path, r)
            with db() as c:
                c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (r["id"],))
            if res and res.get("title"):
                res["engine"] = tag
                return res
            return None  # answered "no match": don't burn the other accounts on the same audio
        except Exception as e:
            log.warning("account %s failed: %s", r["id"], e)
            with db() as c:
                c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
    return None


def _group(hits: list) -> list:
    """Votes -> ranked answers. The same song found by different engines / pieces of audio
    (even with a different spelling or a different number of singers) counts together."""
    clusters = []
    for h in hits:
        for cl in clusters:
            if same_song(cl[0]["song"], h["song"]):
                cl.append(h)
                break
        else:
            clusters.append([h])
    ranked = []
    for hs in clusters:
        best = max(hs, key=lambda h: h["w"])
        song = dict(best["song"])
        # the answer with the most singers wins (one engine may know only the first singer)
        song["artist"] = max((h["song"].get("artist") or "" for h in hs),
                             key=lambda a: (len(artist_set(a)), len(a)))
        for k in ("link", "yt"):
            song[k] = next((h["song"].get(k) for h in hs if h["song"].get(k)), None)
        per = {}
        for h in hs:
            per.setdefault(_base(h["engine"]), []).append(h["w"])
        score = sum(max(v) + 0.5 * (len(v) - 1) for v in per.values())
        solid = len(per) >= 2 or len({h["sample"] for h in hs}) >= 2
        ranked.append({"song": song, "score": score, "solid": solid})
    ranked.sort(key=lambda r: (r["solid"], r["score"]), reverse=True)
    return ranked


# ---------- Last resort: what the singer says (speech -> words -> lyrics search) ----------
_WHISPER = {"model": None}
_WHISPER_LOCK = threading.Lock()  # one transcription at a time keeps RAM/CPU under control


def transcribe(path: str) -> str:
    """Speech-to-text with faster-whisper (optional: returns '' if it is not installed)."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return ""
    with _WHISPER_LOCK:
        if _WHISPER["model"] is None:
            _WHISPER["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _info = _WHISPER["model"].transcribe(path, vad_filter=True, beam_size=1,
                                                   condition_on_previous_text=False)
        return " ".join(sg.text.strip() for sg in segs).strip()


def lyric_overlap(words, lyrics: str) -> float:
    """How many of the transcribed words really appear in the candidate song's lyrics."""
    ws = {w for w in words if len(w) > 2}
    if not ws:
        return 0.0
    lw = set(re.findall(r"\w+", (lyrics or "").lower()))
    return len(ws & lw) / len(ws)


def audd_find_lyrics(words, acc):
    q = " ".join(words[:40])
    r = requests.get("https://api.audd.io/findLyrics/", params={"q": q, "api_token": acc["key"]},
                     timeout=30).json()
    for it in (r.get("result") or [])[:3]:
        if not (it.get("title") and it.get("artist")):
            continue
        lyrics = it.get("lyrics")
        if not lyrics:  # check the candidate's real lyrics ourselves
            try:
                lyrics = get_lyrics(it["artist"], it["title"])
            except Exception:
                lyrics = None
        if lyrics and lyric_overlap(words, lyrics) < 0.35:
            continue
        return {"title": it["title"], "artist": it["artist"], "link": it.get("song_link")}
    return None


def lyrics_identify(paths):
    with db() as c:
        accs = c.execute("SELECT * FROM accounts WHERE enabled=1 AND type='audd' ORDER BY id").fetchall()
    if not accs:
        return None
    for p in paths[:2]:
        text = transcribe(p)
        words = re.findall(r"\w+", text.lower())
        if len(words) < 6:  # instrumental, humming or too little singing
            continue
        for acc in accs:
            try:
                res = audd_find_lyrics(words, acc)
                with db() as c:
                    c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (acc["id"],))
                if res:
                    return res
                break  # no match: don't spend the other accounts on the same words
            except Exception as e:
                log.warning("lyrics search failed: %s", e)
                with db() as c:
                    c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (acc["id"],))
    return None


def recognize(paths, skip=()):
    """Listens like a human would, with all the 'ears' at once:
    1) every engine (ACRCloud, AudD, Shazam) listens at the same time, on several pieces of the audio
    2) answers are voted: the same song from 2+ engines / pieces wins, with ALL its singers merged
    3) if nobody agrees, the singing is transcribed and matched with real lyrics
    The result has an "alts" list with the runner-up answers."""
    if isinstance(paths, str):
        paths = [paths]
    skip = tuple(skip)
    rejected = {t[2:] for t in skip if t.startswith("k:")}  # songs the user said are wrong
    jobs = _active_engines(skip)
    order = {n: i for i, (n, _a) in enumerate(jobs)}
    hits = []

    def run_batch(batch):
        tasks = [(name, accs, idx, p) for idx, p in batch for name, accs in jobs]
        if not tasks:
            return
        with ThreadPoolExecutor(max_workers=min(len(tasks), 8)) as ex:
            futs = [ex.submit(call_engine, name, accs, p) for name, accs, _i, p in tasks]
            for f, (name, _accs, idx, _p) in zip(futs, tasks):
                try:
                    res = f.result(timeout=100)
                except Exception as e:
                    log.warning("engine %s crashed: %s", name, e)
                    continue
                if not res or title_key(res.get("title")) in rejected:
                    continue
                w = ENGINE_W.get(name, 0.8) - 0.01 * order.get(name, 0)
                hits.append({"song": res, "engine": res["engine"], "sample": idx, "w": w})

    pieces = list(enumerate(paths[:MAX_SAMPLES]))
    pos = 0
    for size in (1, 2, 2, 2):  # best piece first; only listen to more if the engines don't agree yet
        batch = pieces[pos:pos + size]
        pos += size
        if not batch:
            break
        run_batch(batch)
        ranked = _group(hits)
        if ranked and ranked[0]["solid"]:
            break
    ranked = _group(hits)

    if LYRICS_ENGINE and "lyrics#0" not in skip and not (ranked and ranked[0]["solid"]):
        try:
            ly = lyrics_identify(paths)
            if ly and title_key(ly.get("title")) not in rejected:
                ly["engine"] = "lyrics#0"
                hits.append({"song": ly, "engine": "lyrics#0", "sample": 99, "w": ENGINE_W["lyrics"]})
                ranked = _group(hits)
        except Exception as e:
            log.warning("lyrics stage failed: %s", e)

    if not ranked:
        return None
    log.info("recognize: %s", [(r["song"].get("title"), r["song"].get("artist"), round(r["score"], 2), r["solid"])
                               for r in ranked[:3]])
    top = ranked[0]["song"]
    top["alts"] = [r["song"] for r in ranked[1:4]]
    return top


# ---------- Captions & keyboards ----------
def footer(src=None) -> str:
    f = f'<a href="https://t.me/{BOT_USERNAME}">{esc(BOT_USERNAME)}</a>'
    if src:
        f += f' | <a href="{esc(src, quote=True)}">source</a>'
    return f


def result_caption(song: dict, src=None) -> str:
    return f"<code>{esc(song['title'])} — {esc(song['artist'])}</code>\n\n{footer(src)}"


def audio_caption(info=None) -> str:
    if info:
        return f'@{esc(BOT_USERNAME)} | <a href="{esc(info, quote=True)}">info</a>'
    return f"@{esc(BOT_USERNAME)}"


def clip_keyboard(sid: str, lang: str):
    """What the user sees under a fresh clip: identify button + manual search."""
    return Markup([
        [Btn(f"🎵 {tr(lang, 'btn_rec')} 🎵", callback_data=f"rec:{sid}")],
        [Btn("🔍", switch_inline_query_current_chat="")],
    ])


def result_keyboard(sid: str, song: dict, lang: str = "fa"):
    q = quote(f"{song['artist']} - {song['title']}")
    link = song.get("link") or ""
    sp = link if link.startswith("https://open.spotify.com") else f"https://open.spotify.com/search/{q}"
    return Markup([
        [Btn("Google", url=f"https://www.google.com/search?q={q}"),
         Btn("YouTube Music", url=f"https://music.youtube.com/search?q={q}"),
         Btn("Spotify", url=sp)],
        [Btn(f"🔍 {tr(lang, 'btn_artist')}", switch_inline_query_current_chat=first_artist(song["artist"]))],
        [Btn(f"❌ {tr(lang, 'btn_wrong')} ❌", callback_data=f"bad:{sid}")],
    ])


def audio_keyboard(tid: str, song: dict, lang: str = "fa"):
    q = f"{song.get('artist', '')} {song.get('title', '')}".strip()[:200]
    return Markup([[
        Btn(f"{tr(lang, 'btn_lyrics')} 🔤", callback_data=f"lyr:{tid}"),
        Btn("🔍", switch_inline_query_current_chat=q),
    ]])


# ---------- Sending audio ----------
def song_keys(song: dict) -> list:
    """Cache keys for a song. Engines spell titles/singers differently, so one key per singer."""
    tk = title_key(song.get("title"))
    arts = sorted(artist_set(song.get("artist"), song.get("title")))[:4] or [""]
    keys = [f"s2:{tk}|{a}" for a in arts]
    keys.append("s:" + f"{song.get('artist', '')}|{song.get('title', '')}".lower())  # old format
    return keys


def info_link(a: dict, song=None) -> str:
    """The 'info' link under every audio: song.link for YouTube, the page itself for others."""
    if a.get("info"):
        return a["info"]
    if a.get("vid"):
        return f"https://song.link/y/{a['vid']}"
    if a.get("url"):
        return a["url"]
    q = quote(f"{(song or {}).get('artist', '')} {(song or {}).get('title') or a.get('title', '')}".strip())
    return f"https://music.youtube.com/search?q={q}"


async def fetch_audio(target: str, song=None) -> dict:
    """Download step. On a cache hit returns the file_id right away."""
    keys = [target] + (song_keys(song) if song else [])
    for k in keys:
        hit = cache_get(k)
        if hit:
            return {"file_id": hit["file_id"], "title": hit["title"], "info": hit["info"]}
    tmp = tempfile.mkdtemp()
    fb = None
    if song:
        fb = f"{song['artist']} - {song['title']}"
    elif target.startswith("ytsearch1:"):
        fb = target[10:]
    try:
        a = await asyncio.to_thread(download_audio, target, tmp, fb)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    a["dir"] = tmp
    a["keys"] = keys
    return a


async def prepare_song_audio(song: dict) -> dict:
    target = await asyncio.to_thread(pick_target, song)
    return await fetch_audio(target, song)


async def deliver_audio(msg, a: dict, song=None, lang: str = "fa"):
    title = (song or {}).get("title") or a["title"]
    performer = (song or {}).get("artist") or None
    tsong = {"title": title, "artist": performer or ""}
    kb = audio_keyboard(track_put(tsong), tsong, lang)
    info = info_link(a, tsong)
    if "file_id" in a:
        return await msg.reply_audio(a["file_id"], title=title, performer=performer,
                                     caption=audio_caption(info), parse_mode="HTML",
                                     reply_markup=kb)
    try:
        with open(a["path"], "rb") as f:
            sent = await msg.reply_audio(f, title=title, performer=performer, duration=a.get("duration"),
                                         caption=audio_caption(info), parse_mode="HTML", reply_markup=kb,
                                         read_timeout=300, write_timeout=300)
    finally:
        shutil.rmtree(a["dir"], ignore_errors=True)
    if sent.audio:
        for k in a["keys"]:
            cache_put(k, sent.audio.file_id, title, info)


async def send_mp3(msg, target: str, title=None, performer=None, lang: str = "fa"):
    song = {"title": title, "artist": performer or ""} if title else None
    a = await fetch_audio(target, song)
    await deliver_audio(msg, a, song, lang)


# ---------- Clips ----------
async def upload_video(msg, path: str, caption: str, kb=None):
    with open(path, "rb") as f:
        return await msg.reply_video(f, caption=caption, parse_mode="HTML", reply_markup=kb,
                                     supports_streaming=True, read_timeout=300, write_timeout=300)


async def identify_clip(uid: int, sid: str, meta, src: str, samples_task):
    """Returns (song or None, quota_blocked)."""
    try:
        if meta:
            if "youtu" in src:
                meta["link"] = src
            with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            return {"title": meta["title"], "artist": meta["artist"],
                    "link": meta.get("link"), "engine": "meta#0"}, False
        if not use_quota(uid):
            return None, True
        outs = await samples_task
        if not outs:
            return None, False
        return await asyncio.to_thread(recognize, outs), False
    except Exception:
        log.exception("identify_clip failed")
        return None, False


async def prep_clip(uid: int, sid: str, meta, src: str, samples_task) -> dict:
    """Background job: recognize the music and download its audio, so the
    'identify' button can answer instantly."""
    res = {"song": None, "audio": None, "blocked": False}
    try:
        song, blocked = await identify_clip(uid, sid, meta, src, samples_task)
        res["song"], res["blocked"] = song, blocked
        if song:
            remember_alts(sid, song)
            set_last(sid, song)
            clip_set_song(src, song)
            try:
                res["audio"] = await prepare_song_audio(song)
            except Exception:
                log.exception("prepare audio failed")
    except Exception:
        log.exception("prep_clip failed")
    return res


def _msg_video_id(sent):
    for attr in ("video", "animation", "document"):
        obj = getattr(sent, attr, None)
        if obj:
            return obj.file_id
    return None


async def clip_from_cache(msg, src: str, hit, lang: str):
    song = json.loads(hit["song"])
    sid = uuid.uuid4().hex[:10]
    add_sample(sid, "", src)
    set_last(sid, {**song, "engine": "cache#0"})

    async def prep():
        res = {"song": song, "audio": None, "blocked": False}
        try:
            res["audio"] = await prepare_song_audio(song)
        except Exception:
            log.exception("cached clip audio prepare failed")
        return res

    PREP[sid] = {"task": asyncio.create_task(prep()), "src": src, "created": time.time(), "used": False}
    try:
        await msg.reply_video(hit["file_id"], caption=footer(src), parse_mode="HTML",
                              reply_markup=clip_keyboard(sid, lang), supports_streaming=True)
    except Exception:
        drop_entry(sid)
        raise


async def do_clip(msg, url: str):
    uid = msg.from_user.id
    lang = get_lang(uid)
    src = norm_url(url)
    cleanup_samples()
    cleanup_prep()

    hit = clip_get(src)
    if hit:
        try:
            return await clip_from_cache(msg, src, hit, lang)
        except Exception:
            log.exception("clip cache send failed, re-downloading")
            clip_delete(src)

    with tempfile.TemporaryDirectory() as tmp:
        path, title, meta = await asyncio.to_thread(download_clip, src, tmp)
        if not path:
            return await msg.reply_text(tr(lang, "clip_missing"))
        sid = uuid.uuid4().hex[:10]
        add_sample(sid, "", src)

        # background: cut samples -> recognize -> download audio (not awaited)
        samples_task = asyncio.create_task(asyncio.to_thread(make_samples, path, sid))
        prep_task = asyncio.create_task(prep_clip(uid, sid, meta, src, samples_task))
        PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False}
        try:
            # the clip goes out right away with the identify + search buttons
            sent = await upload_video(msg, path, footer(src), clip_keyboard(sid, lang))
            fid = _msg_video_id(sent)
            if fid:
                clip_put(src, fid)
        except Exception:
            drop_entry(sid)
            raise
        finally:
            # the clip file lives in tmp, so the sample cutting must finish before we leave
            await asyncio.gather(samples_task, return_exceptions=True)


async def show_prepared(q, sid: str, entry: dict, lang: str):
    """The user tapped 'identify': reveal what the background job already prepared."""
    if entry.get("used"):
        return await q.answer()
    task = entry["task"]
    if task.done():
        await q.answer()
    else:
        await q.answer(tr(lang, "wait_toast"))
    try:
        res = await asyncio.wait_for(asyncio.shield(task), timeout=240)
    except Exception:
        log.exception("waiting for prepared result failed")
        return await q.message.reply_text(tr(lang, "not_found"))
    song = res.get("song")
    if not song:
        key = "quota_short" if res.get("blocked") else "not_found"
        return await q.message.reply_text(tr(lang, key))
    if entry.get("used"):  # double tap while waiting
        return
    entry["used"] = True
    src = entry["src"]
    try:
        await q.edit_message_caption(caption=result_caption(song, src), parse_mode="HTML",
                                     reply_markup=result_keyboard(sid, song, lang))
    except Exception:
        log.warning("edit caption failed", exc_info=True)
    try:
        a = res.get("audio") or await prepare_song_audio(song)
        await deliver_audio(q.message, a, song, lang)
    except Exception as e:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err", e=str(e)[:200]))
    PREP.pop(sid, None)


# ---------- Voice / audio from user ----------
async def handle_media(msg, media, lang: str):
    if not use_quota(msg.from_user.id):
        return await msg.reply_text(tr(lang, "quota"))
    cleanup_samples()
    sid = uuid.uuid4().hex[:10]
    raw = os.path.join(SAMPLES_DIR, sid + ".raw")
    await (await media.get_file()).download_to_drive(raw)
    outs = await asyncio.to_thread(make_samples, raw, sid)
    if outs:
        os.remove(raw)
    else:
        fallback = os.path.join(SAMPLES_DIR, f"{sid}_0.ogg")
        os.replace(raw, fallback)
        outs = [fallback]
    add_sample(sid, outs[0])
    song = await asyncio.to_thread(recognize, outs)
    if not song:
        return await msg.reply_text(tr(lang, "not_found"))
    remember_alts(sid, song)
    set_last(sid, song)
    audio_task = asyncio.create_task(prepare_song_audio(song))
    await msg.reply_text(result_caption(song), parse_mode="HTML",
                         reply_markup=result_keyboard(sid, song, lang), **NO_PREVIEW)
    try:
        a = await audio_task
        await deliver_audio(msg, a, song, lang)
    except Exception as e:
        log.exception("mp3 failed")
        await msg.reply_text(tr(lang, "err", e=str(e)[:200]))


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    uid = msg.from_user.id
    touch_user(msg.from_user)
    lang = get_lang(uid)
    cleanup_prep()
    media = msg.voice or msg.audio or msg.video_note
    text = (msg.text or "").strip()
    m = URL_RE.search(text)
    is_clip = bool(m and any(h in m.group(0) for h in CLIP_HOSTS))
    action = ChatAction.UPLOAD_VIDEO if is_clip else ChatAction.RECORD_VOICE
    try:
        # no "working..." message: the status shows under the bot name instead
        async with chat_action(ctx.bot, msg.chat_id, action):
            if media:
                await handle_media(msg, media, lang)
            elif text.startswith(MP3_PREFIX):
                await send_mp3(msg, text[len(MP3_PREFIX):].strip(), lang=lang)
            elif is_clip:
                await do_clip(msg, m.group(0))
            elif URL_RE.search(text):  # other links (Spotify, ...)
                target, _ = await asyncio.to_thread(resolve, text)
                await send_mp3(msg, target, lang=lang)
            else:  # artist or song name -> list of tracks to choose from
                await send_results(msg, text, lang)
    except Exception as e:
        log.exception("handle failed")
        await msg.reply_text(tr(lang, "err", e=str(e)[:200]))


# ---------- Buttons ----------
async def on_rec(q, data: str):
    action, sid = data.split(":", 1)
    uid = q.from_user.id
    lang = get_lang(uid)

    if action == "rec":
        entry = PREP.get(sid)
        if entry:
            return await show_prepared(q, sid, entry, lang)
        # no prepared result (bot restarted / expired): recognize now, below
    else:
        drop_entry(sid)  # "wrong song": the prepared result is wrong, throw it away

    with db() as c:
        row = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    if row and action == "bad" and row["src"]:
        clip_delete(row["src"])  # wrong result must not stay in the clip cache
    paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, f"{sid}_*")))
    mpath = os.path.join(SAMPLES_DIR, f"{sid}.json")
    if not row or not (paths or os.path.exists(mpath) or CANDS.get(sid)):
        return await q.answer(tr(lang, "expired"), show_alert=True)
    tried = [t for t in (row["tried"] or "").split(",") if t]
    if action == "bad" and row["last"]:
        eng, _, key = row["last"].partition("||")
        if key:
            tried.append("k:" + key)  # this song is wrong, whoever suggested it
        if eng and (not key or eng in ("meta#0", "lyrics#0", "cache#0")):
            tried.append(eng)
        with db() as c:
            c.execute("UPDATE samples SET tried=? WHERE id=?", (",".join(tried), sid))
    rejected = {t[2:] for t in tried if t.startswith("k:")}
    song = None
    if action == "bad":  # next best answer we already have: instant, costs no quota
        alts = (CANDS.get(sid) or {}).get("alts") or []
        while alts:
            c_ = alts.pop(0)
            if title_key(c_.get("title")) not in rejected:
                song = c_
                break
    if not song and "meta#0" not in tried and os.path.exists(mpath):
        try:
            with open(mpath, encoding="utf-8") as f:
                m = json.load(f)
            song = {"title": m["title"], "artist": m["artist"], "link": m.get("link"), "engine": "meta#0"}
        except Exception:
            song = None
    if song:
        await q.answer()
    else:
        if not paths:
            return await q.answer(tr(lang, "no_result"), show_alert=True)
        if not use_quota(uid):
            return await q.answer(tr(lang, "quota_short"), show_alert=True)
        await q.answer(tr(lang, "identifying"))
        song = await asyncio.to_thread(recognize, paths, tuple(tried))
        if song:
            remember_alts(sid, song)
    if not song:
        txt = tr(lang, "not_found") if not tried else tr(lang, "no_more")
        return await q.message.reply_text(txt)
    set_last(sid, song)
    src = row["src"]
    kb = result_keyboard(sid, song, lang)
    cap = result_caption(song, src)
    audio_task = asyncio.create_task(prepare_song_audio(song))
    try:
        if q.message.text:
            await q.edit_message_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
        else:
            await q.edit_message_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await q.message.reply_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
    if src:
        clip_set_song(src, song)
        vid = getattr(q.message, "video", None)
        if vid:
            clip_put(src, vid.file_id, song)
    try:
        a = await audio_task
        await deliver_audio(q.message, a, song, lang)
    except Exception as e:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err", e=str(e)[:200]))


async def on_lyrics(q, data: str):
    lang = get_lang(q.from_user.id)
    tid = data.split(":", 1)[1]
    with db() as c:
        row = c.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        return await q.answer(tr(lang, "track_missing"), show_alert=True)
    await q.answer(tr(lang, "lyrics_wait"))
    text = await asyncio.to_thread(get_lyrics, row["artist"], row["title"])
    if not text:
        return await q.message.reply_text(tr(lang, "lyrics_none"))
    for chunk in split_text(f"🎤 {row['title']}\n\n{text}"):
        await q.message.reply_text(chunk)


async def on_pick(q, ctx, data: str):
    lang = get_lang(q.from_user.id)
    vid = data.split(":", 1)[1]
    await q.answer(tr(lang, "wait_toast"))
    try:
        async with chat_action(ctx.bot, q.message.chat_id, ChatAction.RECORD_VOICE):
            await send_mp3(q.message, f"https://www.youtube.com/watch?v={vid}", lang=lang)
    except Exception as e:
        log.exception("pick failed")
        await q.message.reply_text(tr(lang, "err", e=str(e)[:200]))


async def on_more(q, data: str):
    lang = get_lang(q.from_user.id)
    _, sid, page = data.split(":")
    if sid not in SEARCH:
        return await q.answer(tr(lang, "expired"), show_alert=True)
    await q.answer()
    try:
        await q.edit_message_reply_markup(reply_markup=results_keyboard(sid, int(page), lang))
    except Exception:
        pass


async def on_lang_button(q, data: str):
    code = data.split(":", 1)[1]
    if code not in LANGS:
        return await q.answer()
    set_lang(q.from_user.id, code)
    toast = tr(code, "lang_set") if code in STR else "✅ " + LANGS[code]
    await q.answer(toast)
    # the language list disappears: the message becomes the welcome text, no buttons
    text = tr(code, "welcome").rsplit("\n\n", 1)[0] + "\n\n🌐 /lang"
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=None, **NO_PREVIEW)
    except Exception:
        pass


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    touch_user(q.from_user)
    if data.startswith(("rec:", "bad:")):
        return await on_rec(q, data)
    if data.startswith("pick:"):
        return await on_pick(q, ctx, data)
    if data.startswith("more:"):
        return await on_more(q, data)
    if data.startswith("lyr:"):
        return await on_lyrics(q, data)
    if data.startswith("lang:"):
        return await on_lang_button(q, data)
    await q.answer()


# ---------- Inline search by artist ----------
async def on_inline(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    text = (q.query or "").strip()
    if len(text) < 2:
        return await q.answer([], cache_time=5)
    try:
        items = await asyncio.to_thread(yt_search, text)
    except Exception as e:
        log.warning("inline search failed: %s", e)
        return await q.answer([], cache_time=5)
    results = []
    for it in items:
        vid = it.get("id")
        if not vid:
            continue
        title = it.get("title") or vid
        dur = it.get("duration")
        desc = it.get("channel") or it.get("uploader") or ""
        if dur:
            desc += f" • {int(dur) // 60}:{int(dur) % 60:02d}"
        results.append(InlineQueryResultArticle(
            id=vid, title=title[:100], description=desc,
            thumbnail_url=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            input_message_content=InputTextMessageContent(f"{MP3_PREFIX}https://www.youtube.com/watch?v={vid}"),
        ))
    await q.answer(results, cache_time=60)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    # only the language list is shown first; after a choice it turns into the welcome text
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_keyboard())


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_keyboard())


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("update error", exc_info=ctx.error)


def main():
    migrate()
    seed_from_env()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    for name, fn in [("start", cmd_start), ("lang", cmd_lang)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
        handle,
    ))
    app.add_error_handler(on_error)
    # Admin features live in ./admin and are optional: if that folder is missing or broken,
    # the bot still starts and recognition works as usual.
    try:
        from admin import register as register_admin
        register_admin(app, sys.modules[__name__])
    except Exception:
        log.exception("admin module not loaded - the bot continues without it")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
