"""
Music recognition Telegram bot  -  v7 (accurate + fast)

  * own fingerprint DB (fp_engine) is asked FIRST: free and instant
  * ACRCloud + Shazam work TOGETHER: every piece of audio goes to both engines at the same time, the votes
    are added up, and the answer is sent the moment it is confirmed (no waiting for slow pieces)
  * a result is accepted only when it is confirmed (two engines agree, or two different pieces agree)
    and has no close competitor
  * the ORIGINAL file is downloaded: direct YouTube id from ACR first, otherwise YouTube results are
    scored (title, artist, "- Topic" / VEVO / official channel, duration vs the real duration,
    remix / live / cover / sped-up are rejected) and the best one is taken
  * while recognizing: ONLY the status at the top of the chat (typing / recording...) stays on until the
    song is found. Nothing is shown under the clip, for voice, clip link, lyrics text and song name
  * quota is refunded when nothing is recognized or something crashes
"""
import json, asyncio, sqlite3, datetime, os, re, tempfile, glob, time, hmac, hashlib, base64, uuid
import subprocess, logging, html, shutil, sys, threading, array, unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse, parse_qs
import requests
from requests.adapters import HTTPAdapter
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

try:  # optional: much faster loudness scan
    import numpy as np
except Exception:
    np = None

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

try:  # your own fingerprint database (optional)
    import fp_engine
except Exception:
    fp_engine = None
    log.warning("fp_engine not loaded: local database is off")


# ======================================================================================
# Config
# ======================================================================================
BOT_TOKEN = os.environ["BOT_TOKEN"]
if "BOT_USERNAME" not in os.environ:
    log.warning("BOT_USERNAME is not set in the environment, using the built-in default")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Whatsmuziccbot").lstrip("@")
ACR_HOST = os.environ.get("ACR_HOST")
ACR_KEY = os.environ.get("ACR_KEY")
ACR_SECRET = os.environ.get("ACR_SECRET")
ODESLI_KEY = os.environ.get("ODESLI_KEY")  # optional
ENGINE_ORDER = os.environ.get("ENGINES", "acr,shazam").split(",")
PRIMARY_ENGINES = ("acr", "shazam")
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", str(6 * 3600)))
LYRICS_ENGINE = os.environ.get("LYRICS_ENGINE", "1") == "1"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_WAIT = int(os.environ.get("WHISPER_WAIT", "25"))
HUM_MIN_SCORE = int(os.environ.get("HUM_MIN_SCORE", "30"))
MAX_SAMPLES = max(3, min(int(os.environ.get("MAX_SAMPLES", "10")), 12))
# STRICT=1: if the best answer is still not confirmed after all pieces, say "not found" instead of guessing
STRICT = os.environ.get("STRICT", "0") == "1"
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))
DL_CONCURRENCY = int(os.environ.get("DL_CONCURRENCY", "4"))
ENGINE_THREADS = int(os.environ.get("ENGINE_THREADS", "12"))
ENGINE_TIMEOUT = int(os.environ.get("ENGINE_TIMEOUT", "30"))  # max wait for one wave of engine answers
CLIP_MAX_SEC = int(os.environ.get("CLIP_MAX_SEC", "900"))
MAX_SRC_SEC = int(os.environ.get("MAX_SRC_SEC", str(3 * 3600)))
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")
DL_DIR = os.path.join(SAMPLES_DIR, "dl")
COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_BYTES = 49 * 1024 * 1024
ARIA2 = bool(shutil.which("aria2c")) and os.environ.get("ARIA2", "1") == "1"
os.makedirs(SAMPLES_DIR, exist_ok=True)
os.makedirs(DL_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
CLIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com")
DIRECT_HOSTS = CLIP_HOSTS
AUDIO_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")
YT_HOSTS = ("youtube.com", "youtu.be")
MP3_PREFIX = "🎵 "
SONG_KEYS = ("title", "artist", "link", "yt", "dur")

esc = html.escape

PREP = {}   # sid -> {"task", "src", "created", "used", "busy", "song"}
CANDS = {}  # sid -> {"alts": [...], "created": ts}
SEARCH = {}  # sid -> {"items": [...], "created": ts}
PAGE = 8

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=48))
HTTP.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8))

ENGINE_POOL = ThreadPoolExecutor(max_workers=ENGINE_THREADS, thread_name_prefix="engine")

_DL_SEM = None


def dl_sem() -> asyncio.Semaphore:
    global _DL_SEM
    if _DL_SEM is None:
        _DL_SEM = asyncio.Semaphore(max(1, DL_CONCURRENCY))
    return _DL_SEM


# ======================================================================================
# Languages
# ======================================================================================
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
        "err": "❌ دریافت رسانه از منبع ممکن نیست.",
        "clip_missing": "⚠️ کلیپ پیدا نشد یا بزرگ‌تر از ۵۰ مگابایته.",
        "quota": "⛔ سقف تشخیص امروزت پر شده، فردا دوباره امتحان کن.",
        "quota_short": "⛔ سقف تشخیص امروزت پر شده.",
        "not_found": "😕 آهنگ شناسایی نشد.\nبخش دیگری بفرست که موسیقی در آن واضح شنیده شود، بدون صحبت یا سروصدای زیاد.",
        "no_more": "😕 موتور دیگه‌ای برای امتحان نمونده.",
        "expired": "نمونه منقضی شده، لینک یا فایل رو دوباره بفرست.",
        "no_result": "نتیجه‌ی دیگه‌ای نیست.",
        "identifying": "⏳ در حال شناسایی آهنگ...",
        "wait_toast": "⏳ در حال شناسایی آهنگ...",
        "downloading": "⏳ در حال دریافت آهنگ...",
        "lyrics_wait": "⏳ در حال پیدا کردن متن ترانه...",
        "lyrics_none": "😕 متن این آهنگ پیدا نشد.",
        "track_missing": "اطلاعات آهنگ پیدا نشد.",
        "btn_rec": "شناسایی موسیقی",
        "btn_artist": "جستجو بر اساس هنرمند",
        "btn_wrong": "آهنگ اشتباه",
        "btn_lyrics": "متن ترانه",
        "btn_more": "آهنگ‌های بیشتر",
        "no_music": "⚠️ آهنگ این پست پیدا نشد.",
        "slow": "⏳ یکم آروم‌تر، چند ثانیه صبر کن.",
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
        "err": "❌ Couldn't get the media from the source.",
        "clip_missing": "⚠️ Clip not found or larger than 50 MB.",
        "quota": "⛔ You've reached today's recognition limit. Try again tomorrow.",
        "quota_short": "⛔ You've reached today's recognition limit.",
        "not_found": "😕 Couldn't identify the song.\nSend another part where the music is clearly audible, without speech or too much noise.",
        "no_more": "😕 No other engine left to try.",
        "expired": "This sample has expired. Please send the link or file again.",
        "no_result": "No other result.",
        "identifying": "⏳ Identifying the song...",
        "wait_toast": "⏳ Identifying the song...",
        "downloading": "⏳ Getting the song...",
        "lyrics_wait": "⏳ Looking for the lyrics...",
        "lyrics_none": "😕 Couldn't find lyrics for this song.",
        "track_missing": "Song info not found.",
        "btn_rec": "Identify music",
        "btn_artist": "Search by artist",
        "btn_wrong": "Wrong song",
        "btn_lyrics": "Lyrics",
        "btn_more": "More songs",
        "no_music": "⚠️ Couldn't find this post's music.",
        "slow": "⏳ Slow down a little, wait a few seconds.",
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
        "no_music": "⚠️ Bu gönderinin müziği bulunamadı.",
        "slow": "⏳ Biraz yavaş, birkaç saniye bekle.",
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
        "no_music": "⚠️ لم يتم العثور على موسيقى هذا المنشور.",
        "slow": "⏳ على مهل قليلاً، انتظر بضع ثوانٍ.",
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
        "no_music": "⚠️ Не удалось найти музыку этого поста.",
        "slow": "⏳ Помедленнее, подожди несколько секунд.",
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


# ======================================================================================
# Small helpers: chat action (the status at the top of the chat), rate limit, URL / host handling
# ======================================================================================
@asynccontextmanager
async def chat_action(bot, chat_id, action=ChatAction.RECORD_VOICE):
    """Keeps the status at the top of the chat ("recording voice..." / "typing...") alive while the
    block runs. This is the ONLY progress indicator: nothing is posted under the clip."""
    async def loop():
        while True:
            try:
                await bot.send_chat_action(chat_id, action)
            except Exception:
                pass
            await asyncio.sleep(4)

    t = asyncio.create_task(loop())
    try:
        yield
    finally:
        t.cancel()


_RATE = {}


def rate_ok(uid: int, limit: int = None, window: int = 60) -> bool:
    limit = RATE_LIMIT if limit is None else limit
    if limit <= 0 or uid in ADMIN_IDS:
        return True
    now = time.time()
    dq = _RATE.setdefault(uid, deque())
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def first_url(text: str):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(").,;>]}\"'") if m else None


def host_of(url: str) -> str:
    try:
        h = (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def host_in(url: str, hosts) -> bool:
    h = host_of(url)
    return bool(h) and any(h == d or h.endswith("." + d) for d in hosts)


def norm_url(url: str) -> str:
    try:
        u = urlparse(url)
        host = host_of(url)
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


def normalize_target(target: str) -> str:
    """Whitelist for everything handed to yt-dlp."""
    target = (target or "").strip()
    if URL_RE.match(target):
        if not host_in(target, AUDIO_HOSTS):
            raise ValueError("unsupported link")
        return target
    if re.match(r"(?:yt|sc)search1:", target):
        return target
    return f"ytsearch1:{target}"


# ======================================================================================
# Song / artist matching
# ======================================================================================
_SPLIT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_FEAT_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)", re.I)

_BAD_TITLE_RE = re.compile(
    r"mash\s?-?up|remix|medley|megamix|nightcore|slowed|sped\s?up|speed\s?up|reverb|"
    r"karaoke|8d\s?audio|bass\s?boosted|ringtone|"
    r"\bcover\b|[\(\[\-–]\s*live\b|\blive\s+(?:at|from|in|on|session|version)\b|"
    r"ریمیکس|ریمیک|مشاپ|مدلی|بازخوانی|کاور", re.I)


def title_penalty(song: dict) -> float:
    return 0.5 if _BAD_TITLE_RE.search(song.get("title") or "") else 1.0


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def title_key(t: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t or "")
    t = re.split(r"\s+[-–—]\s+", t)[0]
    return re.sub(r"[\W_]+", "", _fold(t))


def artist_set(artist: str, title: str = "") -> set:
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
    if title_penalty(a) != title_penalty(b):  # original vs remix / cover / live are different songs
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


# ======================================================================================
# SQLite
# ======================================================================================
class _Conn(sqlite3.Connection):
    def __exit__(self, *exc):
        try:
            return super().__exit__(*exc)
        finally:
            self.close()


_DB_READY = False
_DB_LOCK = threading.Lock()


def db():
    global _DB_READY
    c = sqlite3.connect(DB_PATH, timeout=30, factory=_Conn)
    c.row_factory = sqlite3.Row
    if not _DB_READY:
        with _DB_LOCK:
            if not _DB_READY:
                c.execute("PRAGMA journal_mode=WAL")
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
                c.execute("CREATE TABLE IF NOT EXISTS media_songs(uid TEXT PRIMARY KEY, song TEXT, sid TEXT)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_samples_created ON samples(created)")
                c.commit()
                _DB_READY = True
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def migrate():
    with db() as c:
        for table, col in (("songs", "info"), ("samples", "src"), ("users", "lang")):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # AudD is not used anymore: drop its old accounts
        c.execute("DELETE FROM accounts WHERE type='audd'")


def seed_from_env():
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            return
        if all([ACR_HOST, ACR_KEY, ACR_SECRET]):
            c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", (ACR_HOST, ACR_KEY, ACR_SECRET))


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
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid, first_seen, lang) VALUES(?,?,?)",
                  (u.id, datetime.date.today().isoformat(), detect_lang(u.language_code)))


def cache_get(key):
    with db() as c:
        return c.execute("SELECT file_id, title, info FROM songs WHERE key=?", (key,)).fetchone()


def cache_put(key, file_id, title, info=None):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO songs(key,file_id,title,info) VALUES(?,?,?,?)",
                  (key, file_id, title, info))


def clip_get(url):
    with db() as c:
        return c.execute("SELECT file_id, song FROM clips WHERE url=? AND file_id IS NOT NULL AND song IS NOT NULL",
                         (url,)).fetchone()


def clip_put(url, file_id, song=None):
    data = None
    if song:
        data = json.dumps({k: song.get(k) for k in SONG_KEYS}, ensure_ascii=False)
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


def media_cache_get(uid: str):
    with db() as c:
        r = c.execute("SELECT song FROM media_songs WHERE uid=?", (uid,)).fetchone()
    return json.loads(r[0]) if r else None


def media_cache_put(uid: str, song: dict, sid: str):
    data = json.dumps({k: song.get(k) for k in SONG_KEYS}, ensure_ascii=False)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO media_songs VALUES(?,?,?)", (uid, data, sid))


def media_cache_drop(sid: str):
    with db() as c:
        c.execute("DELETE FROM media_songs WHERE sid=?", (sid,))


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
        c.execute("INSERT OR IGNORE INTO usage VALUES(?,?,0)", (uid, day))
        cur = c.execute("UPDATE usage SET n=n+1 WHERE uid=? AND day=? AND n<?", (uid, day, limit))
        return cur.rowcount > 0


def refund_quota(uid: int):
    if uid in ADMIN_IDS or daily_limit() <= 0:
        return
    day = datetime.date.today().isoformat()
    with db() as c:
        c.execute("UPDATE usage SET n=n-1 WHERE uid=? AND day=? AND n>0", (uid, day))


def add_sample(sid: str, path: str, src=None):
    with db() as c:
        c.execute("INSERT INTO samples(id,path,src,created) VALUES(?,?,?,?)", (sid, path, src, int(time.time())))


def set_last(sid: str, song: dict):
    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}"
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (val, sid))


def remember_alts(sid: str, song: dict):
    CANDS[sid] = {"alts": song.pop("alts", None) or [], "created": time.time()}


# ======================================================================================
# Background housekeeping
# ======================================================================================
def cleanup_samples():
    now = time.time()
    cutoff = int(now) - 86400
    with db() as c:
        rows = c.execute("SELECT id FROM samples WHERE created<?", (cutoff,)).fetchall()
        for r in rows:
            for p in glob.glob(os.path.join(SAMPLES_DIR, r["id"] + "*")):
                try:
                    os.remove(p)
                except OSError:
                    pass
        c.execute("DELETE FROM samples WHERE created<?", (cutoff,))
        old_day = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
        c.execute("DELETE FROM usage WHERE day<?", (old_day,))
    try:
        for name in os.listdir(SAMPLES_DIR):
            p = os.path.join(SAMPLES_DIR, name)
            if os.path.isfile(p) and now - os.path.getmtime(p) > 86400:
                os.remove(p)
    except OSError:
        pass
    try:
        for name in os.listdir(DL_DIR):
            p = os.path.join(DL_DIR, name)
            if now - os.path.getmtime(p) > 3 * 3600:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


def _rm_audio_of(task):
    if task.cancelled():
        return
    try:
        a = (task.result() or {}).get("audio")
        if a and a.get("dir"):
            shutil.rmtree(a["dir"], ignore_errors=True)
    except Exception:
        pass


def drop_entry(sid: str):
    e = PREP.pop(sid, None)
    if not e:
        return
    t = e["task"]
    if t.done():
        _rm_audio_of(t)
    else:
        t.add_done_callback(_rm_audio_of)


def cleanup_prep():
    now = time.time()
    for sid in [s for s, e in PREP.items() if now - e["created"] > 3600]:
        drop_entry(sid)
    for sid in [s for s, e in SEARCH.items() if now - e["created"] > 3 * 3600]:
        SEARCH.pop(sid, None)
    for sid in [s for s, e in CANDS.items() if now - e["created"] > 24 * 3600]:
        CANDS.pop(sid, None)
    for uid in [u for u, dq in _RATE.items() if not dq or now - dq[-1] > 120]:
        _RATE.pop(uid, None)
    for k in [k for k, (ts, _v) in _YT_CACHE.items() if now - ts > 300]:
        _YT_CACHE.pop(k, None)


async def maintenance_loop():
    while True:
        try:
            await asyncio.to_thread(cleanup_samples)
            cleanup_prep()
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(600)


# ======================================================================================
# Persistent background event loop (Shazam)
# ======================================================================================
class _BgLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="bg-loop").start()

    def run(self, coro, timeout: float = 60):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


_BG = None
_BG_LOCK = threading.Lock()
_SHAZAM = {}


def bg_loop() -> _BgLoop:
    global _BG
    with _BG_LOCK:
        if _BG is None:
            _BG = _BgLoop()
    return _BG


def _shazam_client():
    from shazamio import Shazam
    if "obj" not in _SHAZAM:
        _SHAZAM["obj"] = Shazam()
    return _SHAZAM["obj"]


# ======================================================================================
# Account protection
# ======================================================================================
class QuotaError(Exception):
    """The engine account is out of quota or its key is rejected."""


_COOLDOWN = {}


def cool_down(acc_id: int, why: str, sec: int = None):
    sec = sec or COOLDOWN_SEC
    _COOLDOWN[acc_id] = time.time() + sec
    log.warning("account #%s paused for %d min: %s", acc_id, sec // 60, why)


def cooling(acc_id: int) -> bool:
    until = _COOLDOWN.get(acc_id)
    if until and until > time.time():
        return True
    _COOLDOWN.pop(acc_id, None)
    return False


# ======================================================================================
# Local fingerprint database (fp_engine): free + instant
# ======================================================================================
def local_identify(paths):
    """Own fingerprint DB. Returns a song dict or None.
    NOTE: fp_engine.identify(path) must return {"title","artist"[,"link","yt"]} or None."""
    if not fp_engine:
        return None
    use = paths[:3]
    found = []
    for p in use:
        try:
            r = fp_engine.identify(p)  # <- change the function name here if yours differs
        except Exception as e:
            log.warning("local fp failed: %s", e)
            continue
        if r and r.get("title"):
            found.append({"title": r["title"], "artist": r.get("artist") or "",
                          "link": r.get("link"), "yt": r.get("yt"), "engine": "local#0"})
    # a single loose hit is not trusted when several pieces were asked: two pieces must agree
    need = 2 if len(use) >= 2 else 1
    for f in found:
        if sum(1 for g in found if same_song(f, g)) >= need:
            return f
    return None


def quick_local(src: str, sid: str):
    """Asks the local DB with two different raw pieces (start + middle) and accepts the answer only if both
    agree. Very short sources use one piece. Needs no quota, no engines."""
    if not fp_engine:
        return None
    d = probe_duration(src)
    starts = [0] if d < 30 else [0, max(0, d / 2 - 6)]
    outs, found = [], []
    try:
        for i, st in enumerate(starts):
            out = os.path.join(SAMPLES_DIR, f"{sid}_q{i}.mp3")
            outs.append(out)
            found.append(local_identify([out]) if cut_sample(src, out, st, 12, True) else None)
        if not found or any(f is None for f in found):
            return None
        if len(found) > 1 and not same_song(found[0], found[1]):
            return None
        return found[0]
    except Exception as e:
        log.warning("quick local failed: %s", e)
        return None
    finally:
        for out in outs:
            try:
                os.remove(out)
            except OSError:
                pass


# ======================================================================================
# Engines: ACRCloud, Shazam
# ======================================================================================
def _hum_score(h: dict) -> float:
    s = h.get("score") or 0
    try:
        s = float(s)
    except (TypeError, ValueError):
        return 0.0
    return s * 100 if s <= 1 else s


def _acr_dur(m: dict):
    try:
        ms = float(m.get("duration_ms"))
        return ms / 1000.0 if ms > 0 else None
    except (TypeError, ValueError):
        return None


def acr_identify(path: str, acc):
    ts = str(int(time.time()))
    sig_str = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
    sig = base64.b64encode(
        hmac.new(acc["secret"].encode(), sig_str.encode(), hashlib.sha1).digest()
    ).decode()
    with open(path, "rb") as f:
        data = f.read()
    r = HTTP.post(
        f"https://{acc['host']}/v1/identify",
        data={"access_key": acc["key"], "data_type": "audio", "signature_version": "1",
              "signature": sig, "sample_bytes": len(data), "timestamp": ts},
        files={"sample": data},
        timeout=25,
    ).json()
    st = r.get("status", {})
    code = st.get("code")
    if code in (3001, 3003, 3014):
        raise QuotaError(f"acr {code}: {st.get('msg', '')}")
    if code != 0:
        if code not in (1001, 2004):
            log.info("acr status %s: %s", code, st.get("msg", ""))
        return None
    md = r.get("metadata") or {}
    music = md.get("music") or []
    if not music:
        hum = [h for h in (md.get("humming") or []) if h.get("title")]
        hum.sort(key=_hum_score, reverse=True)
        log.info("acr humming: %s", [(h.get("title"), round(_hum_score(h), 1)) for h in hum[:5]])
        good = []
        for h in hum[:5]:
            score = _hum_score(h)
            if score < HUM_MIN_SCORE:
                continue
            artist = ", ".join(a["name"] for a in (h.get("artists") or []) if a.get("name"))
            good.append({"title": h["title"], "artist": artist, "link": None, "yt": None,
                         "wmul": min(0.9, 0.4 + score / 150)})
        if good:
            top = good[0]
            top["more"] = [dict(g, wmul=g["wmul"] * 0.6) for g in good[1:]]
            return top
        log.info("acr: no music match (metadata keys: %s)", list(md.keys()))
        return None
    cands = []
    for m in music[:3]:
        if not m.get("title"):
            continue
        ext = m.get("external_metadata", {})
        link = None
        yt = None
        if ext.get("spotify", {}).get("track", {}).get("id"):
            link = "https://open.spotify.com/track/" + ext["spotify"]["track"]["id"]
        if ext.get("youtube", {}).get("vid"):
            yt = "https://www.youtube.com/watch?v=" + ext["youtube"]["vid"]
            link = link or yt
        artist = ", ".join(a["name"] for a in (m.get("artists") or []) if a.get("name"))
        try:
            sc = float(m.get("score"))
        except (TypeError, ValueError):
            sc = 100.0
        cands.append({"title": m["title"], "artist": artist, "link": link, "yt": yt,
                      "dur": _acr_dur(m),
                      "wmul": max(0.5, min(1.0, sc / 100.0))})
    if not cands:
        return None
    # the original (non remix / cover / live) result goes first, then ACR's own score order
    cands.sort(key=lambda c: (title_penalty(c) < 1, -c["wmul"]))
    top = cands[0]
    top["more"] = [dict(c, wmul=c["wmul"] * 0.5) for c in cands[1:]]
    return top


ENGINE_FN = {"acr": acr_identify}


def _shazam_lyrics_text(track: dict) -> str:
    for sec in (track or {}).get("sections") or []:
        if sec.get("type") == "LYRICS" and sec.get("text"):
            return "\n".join(sec["text"])
    return ""


def shazam_identify(path: str):
    try:
        import shazamio  # noqa: F401
    except Exception:
        return None

    async def go():
        sh = _shazam_client()
        fn = getattr(sh, "recognize", None) or getattr(sh, "recognize_song")
        return await fn(path)

    out = bg_loop().run(go(), timeout=25)
    t = (out or {}).get("track")
    if not t:
        return None
    try:
        lyr = _shazam_lyrics_text(t)
        if lyr:
            _store_lyrics(t.get("subtitle", ""), t.get("title", ""), lyr)
    except Exception as e:
        log.warning("saving shazam lyrics failed: %s", e)
    return {"title": t.get("title", ""), "artist": t.get("subtitle", ""), "link": None}


# ======================================================================================
# Odesli / yt-dlp
# ======================================================================================
_ODESLI = {}


def odesli_to_youtube(link: str, timeout: int = 8):
    hit = _ODESLI.get(link)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    params = {"url": link}
    if ODESLI_KEY:
        params["key"] = ODESLI_KEY
    j = None
    for attempt in range(2):
        try:
            r = HTTP.get("https://api.song.link/v1-alpha.1/links", params=params, timeout=timeout)
            if r.ok:
                j = r.json()
                break
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        except (requests.RequestException, ValueError) as e:
            log.warning("odesli request failed: %s", e)
        if attempt == 0:
            time.sleep(1)
    if not isinstance(j, dict):
        return None, None
    plat = j.get("linksByPlatform") or {}
    yt = (plat.get("youtube") or plat.get("youtubeMusic") or {}).get("url")
    ent = (j.get("entitiesByUniqueId") or {}).get(j.get("entityUniqueId")) or {}
    name = f"{ent.get('artistName', '')} - {ent.get('title', '')}".strip(" -")
    if len(_ODESLI) > 500:
        _ODESLI.clear()
    _ODESLI[link] = (time.time(), (yt, name))
    return yt, name


# --- Fallback without Odesli: read artist/title from the music page itself ---------------
META_HOSTS = ("spotify.com", "spotify.link", "music.apple.com", "itunes.apple.com", "deezer.com",
              "link.deezer.com", "deezer.page.link", "tidal.com", "music.amazon.com",
              "pandora.com", "anghami.com", "napster.com", "audiomack.com")
_META = {}
_UA = {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
       "Accept-Language": "en-US,en;q=0.8"}
_SITE_RE = r"(?:Spotify|Deezer|TIDAL|Apple Music|Amazon Music|Pandora|Anghami|Napster|Audiomack)"


def _meta_tags(page: str) -> dict:
    out = {}
    for tag in re.findall(r"<meta\s[^>]*>", page, re.I):
        attrs = {k.lower(): (a or b) for k, a, b in re.findall(r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            out.setdefault(key.lower(), attrs["content"])
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    if m:
        out.setdefault("title", m.group(1))
    return out


def _clean_meta(title: str, desc: str) -> str:
    t = html.unescape(title or "").strip()
    t = re.sub(r"^Listen to\s+", "", t, flags=re.I)
    t = re.sub(r"\s*[|\u2022\u00b7-]\s*" + _SITE_RE + r"\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s+on\s+" + _SITE_RE + r"\b.*$", "", t, flags=re.I)
    t = re.sub(r"\s*-\s*(?:song|single|album|EP)\b(?:\s+and lyrics)?\s+by\s+", " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -\u2013|")
    d = html.unescape(desc or "").strip()
    d = re.sub(r"^Listen to .*?\.\s+", "", d, flags=re.I)
    artist = ""
    for part in (x.strip() for x in d.split("\u00b7")):
        if part and not re.fullmatch(r"(?i)song|single|album|ep|track|playlist|\d{4}", part) \
                and part.lower() not in t.lower() and len(part) < 80:
            artist = part
            break
    return f"{artist} - {t}".strip(" -") if artist else t


def _fetch_page(url: str, timeout: int) -> str:
    """GET with manual redirects; every hop must stay on a whitelisted music host."""
    for _ in range(5):
        if not url.lower().startswith("https://") or not host_in(url, META_HOSTS):
            return ""
        r = HTTP.get(url, headers=_UA, timeout=timeout, allow_redirects=False, stream=True)
        try:
            if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                nxt = r.headers.get("Location") or ""
                url = requests.compat.urljoin(url, nxt)
                continue
            if not r.ok:
                return ""
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) > 600_000:
                    break
            return raw.decode(r.encoding or "utf-8", errors="replace")
        finally:
            r.close()
    return ""


def _apple_name(link: str, timeout: int) -> str:
    u = urlparse(link)
    q = parse_qs(u.query)
    parts = [x for x in u.path.split("/") if x]
    tid = (q.get("i") or [None])[0] or next((x for x in reversed(parts) if x.isdigit()), None)
    if not tid:
        return ""
    cc = parts[0] if parts and re.fullmatch(r"[a-z]{2}", parts[0]) else "us"
    r = HTTP.get("https://itunes.apple.com/lookup", params={"id": tid, "country": cc}, timeout=timeout)
    if not r.ok:
        return ""
    res = (r.json().get("results") or [None])[0] or {}
    return f"{res.get('artistName', '')} - {res.get('trackName') or res.get('collectionName') or ''}".strip(" -")


def link_meta_name(link: str, timeout: int = 8) -> str:
    """Artist - title for a streaming link without Odesli (iTunes lookup / og tags). '' if unknown."""
    hit = _META.get(link)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    if not host_in(link, META_HOSTS):
        return ""
    path = urlparse(link).path
    if host_in(link, ("spotify.com",)) and re.match(r"/(?:intl-[a-z]+/)?(?:artist|playlist|user|show|episode)/", path):
        return ""
    name = ""
    if host_in(link, ("music.apple.com", "itunes.apple.com")):
        name = _apple_name(link, timeout)
    if not name:
        tags = _meta_tags(_fetch_page(link, timeout))
        name = _clean_meta(tags.get("og:title") or tags.get("twitter:title") or tags.get("title"),
                           tags.get("og:description") or tags.get("description"))
    if len(_META) > 500:
        _META.clear()
    if name:
        _META[link] = (time.time(), name)
    return name


def link_to_youtube(link: str, timeout: int = 8):
    """Odesli first; if it fails or finds nothing, fall back to reading the page. -> (yt_url|None, name|None)"""
    try:
        yt, name = odesli_to_youtube(link, timeout=timeout)
        if yt or name:
            return yt, name
    except Exception as e:
        log.warning("odesli failed: %s", e)
    try:
        return None, (link_meta_name(link, timeout=timeout) or None)
    except Exception as e:
        log.warning("link meta failed: %s", e)
        return None, None


# ======================================================================================
# Picking the ORIGINAL file on YouTube
# ======================================================================================
_JUNK_YT_RE = re.compile(r"full\s+album|playlist|compilation|non-?stop|jukebox|\b\d+\s*hours?\b|"
                         r"1\s*hour|reaction|tutorial|how to play|instrumental|acoustic", re.I)


def _ck(s: str) -> str:
    return re.sub(r"[\W_]+", "", _fold(s or ""))


def yt_match_score(item: dict, song: dict) -> float:
    """Higher = more likely the original studio track of this song."""
    t = item.get("title") or ""
    ch = item.get("channel") or item.get("uploader") or ""
    tl, chl = t.lower(), ch.lower()
    tc, chc = _ck(t), _ck(ch)
    s = 0.0
    want = _ck(clean_title(song.get("title")) or song.get("title"))
    if want and want in tc:
        s += 4
    else:
        s -= 3
    arts = artist_set(song.get("artist"), song.get("title"))
    if arts:
        hit = sum(1 for a in arts if a in tc or a in chc)
        s += (2 + 0.5 * min(hit - 1, 2)) if hit else -1.5
    if _BAD_TITLE_RE.search(t) and not _BAD_TITLE_RE.search(song.get("title") or ""):
        s -= 6
    if _JUNK_YT_RE.search(t):
        s -= 5
    if chl.endswith("- topic"):
        s += 3
    if "vevo" in chl:
        s += 1.5
    if "official" in tl:
        s += 1
    if "audio" in tl:
        s += 0.5
    if "lyric" in tl:
        s -= 0.5
    d, v = song.get("dur"), item.get("duration")
    if d and v:
        diff = abs(float(v) - float(d))
        s += 3 if diff <= 3 else 1.5 if diff <= 8 else 0 if diff <= 20 else -3
    elif v:
        if v > 600 or v < 60:
            s -= 3
    return s


def rank_youtube(song: dict, n: int = 8) -> list:
    """[(score, video_id)] best first, for the original recording of this song."""
    artist = (song.get("artist") or "")[:80]
    q = f"{artist} - {song.get('title') or ''}".strip(" -")
    ranked = []
    for query in (q, q + " official audio"):
        try:
            items = yt_search(query, n)
        except Exception as e:
            log.warning("yt rank search failed: %s", e)
            items = []
        seen = {v for _, v in ranked}
        for it in items:
            vid = it.get("id")
            if vid and vid not in seen and it.get("title"):
                ranked.append((yt_match_score(it, song), vid))
                seen.add(vid)
        ranked.sort(reverse=True)
        if ranked and ranked[0][0] >= 6:  # a clear winner: no second search needed
            break
    return ranked


def pick_targets(song: dict) -> list:
    """Download candidates, best first. The caller tries the next one if the duration is clearly wrong."""
    out = []

    def add(t):
        if t and t not in out:
            out.append(t)

    if song.get("yt"):
        add(norm_url(song["yt"]))
    link = song.get("link") or ""
    if host_in(link, YT_HOSTS):
        add(norm_url(link))
    elif link and not out:
        try:
            yt, _ = link_to_youtube(link, timeout=4)
            if yt and host_in(yt, YT_HOSTS):
                add(norm_url(yt))
        except Exception:
            pass
    # without a known duration a direct id cannot be verified, so skip the extra search
    if not out or song.get("dur"):
        for score, vid in rank_youtube(song)[:3]:
            if score >= 2:
                add(f"https://www.youtube.com/watch?v={vid}")
    add(f"ytsearch1:{song.get('artist', '')} - {song.get('title', '')}")
    return out[:4]


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
    target = normalize_target(target)
    plain = {**ydl_base(), "format": "bestaudio[ext=m4a][abr<=130]/bestaudio[ext=m4a]/bestaudio/best",
             "outtmpl": f"{outdir}/%(id)s.%(ext)s", "max_filesize": 120 * 1024 * 1024}
    fast = dict(plain)
    if ARIA2:
        fast["external_downloader"] = {"default": "aria2c"}
        fast["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]}

    def run(t, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(t, download=True)
            if "entries" in info:
                if not info["entries"]:
                    raise RuntimeError("track not found on SoundCloud (YouTube is blocked too, cookies needed)")
                info = info["entries"][0]
        return info

    def attempt(t):
        try:
            return run(t, fast)
        except Exception:
            if not ARIA2:
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
        raise RuntimeError("audio file was not created")
    path = max(files, key=os.path.getmtime)
    if not path.lower().endswith((".m4a", ".mp3")):
        path = to_mp3(path)
    vid = info.get("id") if info.get("extractor_key") == "Youtube" else None
    dur = info.get("duration")
    return {"path": path, "title": info.get("title", "music"), "vid": vid,
            "url": info.get("webpage_url"),
            "duration": int(dur) if dur else None}


def _meta_from_info(info):
    if not info:
        return None
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    track = info.get("track")
    if artist and track:
        return {"artist": artist.strip(), "title": track}
    return None


def _len_filter(max_sec: int):
    return yt_dlp.utils.match_filter_func(f"duration <=? {int(max_sec)} & !is_live")


def download_clip(url: str, outdir: str):
    """Returns (path, title, meta). path is None when too long / too big."""
    if not host_in(url, CLIP_HOSTS):
        raise ValueError("unsupported link")
    opts = {**ydl_base(),
            "format": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[height<=480]/b",
            "outtmpl": f"{outdir}/clip.%(ext)s",
            "merge_output_format": "mp4",
            "max_filesize": MAX_BYTES,
            "match_filter": _len_filter(CLIP_MAX_SEC)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and "entries" in info:
            info = (info["entries"] or [None])[0]
    files = [f for f in glob.glob(f"{outdir}/clip.*") if not f.endswith((".part", ".ytdl"))]
    if not info or not files or os.path.getsize(files[0]) > MAX_BYTES:
        for f in files:
            try:
                os.remove(f)
            except OSError:
                pass
        return None, (info or {}).get("title"), _meta_from_info(info)
    return files[0], info.get("title") or "clip", _meta_from_info(info)


def download_source_audio(url: str, outdir: str):
    if not host_in(url, CLIP_HOSTS):
        raise ValueError("unsupported link")
    opts = {**ydl_base(),
            "format": "bestaudio[abr<=70]/worstaudio/bestaudio/worst",
            "outtmpl": f"{outdir}/src.%(ext)s",
            "max_filesize": 250 * 1024 * 1024,
            "match_filter": _len_filter(MAX_SRC_SEC)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and "entries" in info:
            info = (info["entries"] or [None])[0]
    files = [f for f in glob.glob(f"{outdir}/src.*") if not f.endswith((".part", ".ytdl", ".aria2"))]
    if not info or not files:
        raise RuntimeError("source audio was not downloaded (too long / too big / unavailable)")
    return files[0], _meta_from_info(info)


# ======================================================================================
# Instagram photo / carousel posts
# ======================================================================================
IG_APP_ID = "936619743392459"
IG_MOBILE_UA = ("Instagram 275.0.0.27.98 Android (33/13; 420dpi; 1080x2400; samsung; SM-S908B; "
                "b0q; qcom; en_US; 458229237)")
_IG_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_IG_CODE_RE = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([\w-]+)")


def ig_media_id(code: str) -> int:
    n = 0
    for ch in code[:11]:
        n = n * 64 + _IG_ALPHA.index(ch)
    return n


def ig_post_audio(url: str, outdir: str, want_audio: bool = True):
    """Returns {"path": audio file or None, "meta": {...} or None} or None.
    Needs a logged-in instagram.com cookie in cookies.txt."""
    m = _IG_CODE_RE.search(url)
    if not m or not os.path.exists(COOKIES):
        return None
    try:
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        s = requests.Session()
        s.cookies = jar
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "X-IG-App-ID": IG_APP_ID, "X-Requested-With": "XMLHttpRequest", "Referer": url})
        if not any(c.name == "sessionid" and "instagram" in c.domain for c in jar):
            log.warning("instagram: cookies.txt has no instagram.com sessionid cookie (login cookie needed)")
            return None
        csrf = next((c.value for c in jar if c.name == "csrftoken" and "instagram" in c.domain), "")
        mid = ig_media_id(m.group(1))
        tries = [
            (f"https://www.instagram.com/api/v1/media/{mid}/info/", {"X-CSRFToken": csrf}),
            (f"https://i.instagram.com/api/v1/media/{mid}/info/", {"User-Agent": IG_MOBILE_UA}),
        ]
        data = None
        for u, hdr in tries:
            r = s.get(u, headers=hdr, timeout=20)
            try:
                data = r.json()
                break
            except ValueError:
                log.warning("instagram info is not JSON: HTTP %s %s | %s", r.status_code, r.url[:90],
                            r.text[:80].replace("\n", " "))
        items = (data or {}).get("items") or []
        if not items:
            log.warning("instagram info has no items: %s", str(data)[:120])
            return None
        it = items[0]
        cm = it.get("clips_metadata") or {}
        asset = (((it.get("music_metadata") or {}).get("music_info") or {}).get("music_asset_info")
                 or (cm.get("music_info") or {}).get("music_asset_info") or {})
        audio_url = asset.get("progressive_download_url") or asset.get("fast_start_progressive_download_url")
        meta = None
        if not asset:
            log.info("instagram: this post has no music-library track (original sound or no music)")
        if asset.get("title") and asset.get("display_artist"):
            meta = {"title": asset["title"], "artist": asset["display_artist"], "link": None,
                    "engine": "meta#0", "w": 0.8}
        if not want_audio:
            return {"path": None, "meta": meta}
        if not audio_url:
            audio_url = (cm.get("original_sound_info") or {}).get("progressive_download_url")
        if not audio_url:
            return None
        a = s.get(audio_url, timeout=60)
        if not a.ok or not a.content:
            return None
        path = os.path.join(outdir, "post_audio.m4a")
        with open(path, "wb") as f:
            f.write(a.content)
        return {"path": path, "meta": meta}
    except Exception as e:
        log.warning("instagram post audio failed: %s", e)
        return None


# ======================================================================================
# Audio sampling (ffmpeg)
# ======================================================================================
def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def cut_sample(src: str, out: str, start: float, dur: int, raw: bool = False) -> bool:
    base = ["ffmpeg", "-y", "-ss", str(max(0, start)), "-i", src, "-t", str(dur),
            "-vn", "-ac", "1", "-ar", "44100"]
    filters = [[]] if raw else [["-af", "highpass=f=80,dynaudnorm=f=200:g=15"], []]
    for af in filters:
        try:
            subprocess.run(base + af + ["-b:a", "128k", out],
                           check=True, capture_output=True, timeout=120)
            return True
        except Exception:
            continue
    return False


def loud_offsets(src: str, win: int, n: int = 2, maxsec: int = 600) -> list:
    try:
        sr = 2000
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-t", str(int(maxsec)), "-i", src, "-vn", "-ac", "1", "-ar", str(sr),
             "-f", "s16le", "-"], capture_output=True, timeout=60 + int(maxsec) // 30).stdout
        if np is not None:
            pcm = np.frombuffer(out[:len(out) // 2 * 2], dtype=np.int16).astype(np.float32)
            secs = len(pcm) // sr
            if secs <= win + 2:
                return []
            energy = (pcm[:secs * sr].reshape(secs, sr) ** 2).mean(axis=1)
            del pcm
            pref = np.concatenate(([0.0], np.cumsum(energy, dtype=np.float64)))
            window_sum = pref[win:] - pref[:len(pref) - win]
            order = np.argsort(-window_sum)
            ranked = [int(i) for i in order if i <= secs - win]
        else:
            pcm = array.array("h")
            pcm.frombytes(out[:len(out) // 2 * 2])
            secs = len(pcm) // sr
            if secs <= win + 2:
                return []
            energy = [sum(x * x for x in pcm[i * sr:(i + 1) * sr]) / sr for i in range(secs)]
            pref = [0.0]
            for v in energy:
                pref.append(pref[-1] + v)
            ranked = [i for _, i in sorted(((pref[i + win] - pref[i], i) for i in range(0, secs - win + 1)),
                                           reverse=True)]
        picks = []
        for i in ranked:
            if all(abs(i - j) >= win for j in picks):
                picks.append(i)
            if len(picks) >= n:
                break
        return picks
    except Exception as e:
        log.warning("loud_offsets failed: %s", e)
        return []


def pick_offsets(src: str, d: float, win: int = 15) -> list:
    """More pieces than before: accuracy matters more than speed."""
    if not d or d <= win + 3:
        return [0]
    n = 4 if d <= 60 else 6 if d <= 300 else 8
    loud = loud_offsets(src, win, 2, int(min(d, MAX_SRC_SEC)))
    even = [d * (k + 0.5) / n - win / 2 for k in range(n)]
    mid = even[n // 2]
    rest = [e for i, e in enumerate(even) if i != n // 2]
    cand = loud[:1] + [mid] + loud[1:2] + rest
    offs = []
    for o in cand:
        o = round(max(0, min(o, d - win)))
        if all(abs(o - x) >= 5 for x in offs):
            offs.append(o)
    return offs[:n]


def make_samples(src: str, sid: str) -> list:
    d = probe_duration(src)
    win = 15
    offs = pick_offsets(src, d, win)
    # RAW pieces only: filters like dynaudnorm change the audio and hurt fingerprint matching
    jobs = [(os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3"), o) for i, o in enumerate(offs)]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        res = list(ex.map(lambda j: j[0] if cut_sample(src, j[0], j[1], win, True) else None, jobs))
    return [r for r in res if r]


def make_variants(src: str) -> list:
    base = re.sub(r"_[^_/]*$", "", os.path.splitext(src)[0])
    outs = []
    for i, af in enumerate(("asetrate=44100*1.12,aresample=44100", "asetrate=44100*0.89,aresample=44100")):
        out = f"{base}v{i}.mp3"
        try:
            subprocess.run(["ffmpeg", "-y", "-i", src, "-af", af, "-b:a", "128k", out],
                           check=True, capture_output=True, timeout=60)
            outs.append(out)
        except Exception:
            pass
    return outs


# ======================================================================================
# YouTube search
# ======================================================================================
_YT_CACHE = {}


def yt_search(query: str, n: int = 10):
    key = (query.strip().lower(), n)
    hit = _YT_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    opts = {**ydl_base(), "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    items = info.get("entries") or []
    if len(_YT_CACHE) > 300:
        _YT_CACHE.clear()
    _YT_CACHE[key] = (time.time(), items)
    return items


def search_tracks(query: str, n: int = 30):
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
    url = first_url(text)
    if url:
        if host_in(url, DIRECT_HOSTS) or host_in(url, ("soundcloud.com",)):
            return url, None
        yt, name = link_to_youtube(url, timeout=20)
        if not yt and not name:
            raise ValueError("unsupported link")
        return (yt or f"ytsearch1:{name}"), name
    return text, text


# ======================================================================================
# Lyrics
# ======================================================================================
def clean_title(t: str) -> str:
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t or "").strip()


def _lyr_keys(artist: str, title: str) -> list:
    tk = title_key(title)
    return [f"tk:{tk}|{a}" for a in sorted(artist_set(artist, title))[:4]]


def _store_lyrics(artist: str, title: str, text: str):
    with db() as c:
        for k in _lyr_keys(artist, title):
            c.execute("INSERT OR REPLACE INTO lyrics VALUES(?,?)", (k, text))


def shazam_lyrics(artist: str, title: str):
    try:
        import shazamio  # noqa: F401
    except Exception:
        return None
    tk = title_key(title)

    async def go():
        sh = _shazam_client()
        res = await sh.search_track(query=f"{title} {first_artist(artist)}".strip(), limit=5)
        for h in (((res or {}).get("tracks") or {}).get("hits") or []):
            t = h.get("track") or {}
            if title_key(t.get("title")) != tk or not t.get("key"):
                continue
            about = await sh.track_about(track_id=int(t["key"]))
            about = (about or {}).get("track", about) or {}
            lyr = _shazam_lyrics_text(about)
            if lyr:
                return lyr
        return None

    try:
        return bg_loop().run(go(), timeout=30)
    except Exception as e:
        log.warning("shazam lyrics failed: %s", e)
        return None


def _lrclib_lyrics(artist: str, title: str):
    t = clean_title(title) or title
    a = first_artist(artist)
    want = {"title": title, "artist": artist}
    tries = []
    if a:
        tries.append({"track_name": t, "artist_name": a})
    tries.append({"q": f"{a} {t}".strip()})
    for params in tries:
        try:
            r = HTTP.get("https://lrclib.net/api/search", params=params, timeout=10)
        except Exception as e:
            log.warning("lrclib failed: %s", e)
            continue
        if not r.ok:
            continue
        for it in r.json():
            got = {"title": it.get("trackName") or "", "artist": it.get("artistName") or ""}
            if it.get("plainLyrics") and same_song(got, want):
                return it["plainLyrics"]
    return None


def get_lyrics(artist: str, title: str):
    key = f"{artist}|{title}".lower()
    with db() as c:
        r = c.execute("SELECT text FROM lyrics WHERE key=?", (key,)).fetchone()
        if not r:
            for k in _lyr_keys(artist, title):
                r = c.execute("SELECT text FROM lyrics WHERE key=?", (k,)).fetchone()
                if r:
                    break
    if r:
        return r[0]
    text = _lrclib_lyrics(artist, title) or shazam_lyrics(artist, title)
    if not text:
        a = first_artist(artist)
        t = clean_title(title) or title
        if a:
            try:
                r = HTTP.get(
                    f"https://api.lyrics.ovh/v1/{quote(a, safe='')}/{quote(t, safe='')}", timeout=10)
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


# ======================================================================================
# Recognition: ACRCloud + Shazam vote TOGETHER
# ======================================================================================
# lyrics weight is lower than ACR / Shazam: a text-only match must be confirmed by another vote
ENGINE_W = {"acr": 1.0, "shazam": 1.0, "lyrics": 0.8}
META_W = 0.6


def _base(tag: str) -> str:
    return (tag or "").split("#")[0]


def _active_engines(skip, names=None) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM accounts WHERE enabled=1 ORDER BY id").fetchall()
    jobs = []
    for name in [n.strip() for n in ENGINE_ORDER]:
        if names is not None and name not in names:
            continue
        if name == "shazam":
            if "shazam#0" not in skip:
                jobs.append(("shazam", []))
        elif name in ENGINE_FN:
            accs = [r for r in rows
                    if r["type"] == name and f'{name}#{r["id"]}' not in skip and not cooling(r["id"])]
            if accs:
                jobs.append((name, accs))
    return jobs


def call_engine(name: str, accs, path: str):
    if name == "shazam":
        for attempt in (1, 2):
            try:
                res = shazam_identify(path)
                if res and res.get("title"):
                    res["engine"] = "shazam#0"
                    return res
                return None
            except Exception as e:
                log.warning("shazam failed (try %s): %s", attempt, e)
        return None
    attempts = 2
    for r in accs:
        tag = f'{name}#{r["id"]}'
        for attempt in range(1, attempts + 1):
            try:
                res = ENGINE_FN[name](path, r)
                with db() as c:
                    c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (r["id"],))
                if res and res.get("title"):
                    res["engine"] = tag
                    return res
                return None
            except QuotaError as e:
                cool_down(r["id"], str(e))
                with db() as c:
                    c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
                break
            except Exception as e:
                log.warning("account %s failed (try %s): %s", r["id"], attempt, e)
                if attempt == attempts:
                    with db() as c:
                        c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
    return None


def _group(hits: list) -> list:
    """Groups the answers of all engines / pieces by song. Votes of different engines add up, so when ACR and
    Shazam agree the song wins clearly. Details (link, yt id, real duration) are merged from every engine."""
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
        song["artist"] = max((h["song"].get("artist") or "" for h in hs),
                             key=lambda a: (len(artist_set(a)), len(a)))
        for k in ("link", "yt", "dur"):
            song[k] = next((h["song"].get(k) for h in hs if h["song"].get(k)), None)
        per = {}
        for h in hs:
            per.setdefault(_base(h["engine"]), []).append(h["w"])
        score = sum(max(v) + 0.5 * (len(v) - 1) for v in per.values())
        mult = title_penalty(song)
        score *= mult
        solid = len(per) >= 2 or len({h["sample"] for h in hs}) >= 2
        ranked.append({"song": song, "score": score, "solid": solid, "bad": mult < 1})
    ranked.sort(key=lambda r: r["score"] + (1.0 if r["solid"] else 0.0), reverse=True)
    return ranked


def _confident(ranked: list) -> bool:
    """Accuracy first. The top answer must be confirmed (two engines agree, or two different pieces of
    the audio agree), must not look like a remix / mashup, and must have no close competitor.
    A single answer from one engine on one piece is never enough."""
    if not ranked or ranked[0]["bad"] or not ranked[0]["solid"]:
        return False
    if len(ranked) > 1:
        second = ranked[1]
        if second["solid"] or second["score"] >= ranked[0]["score"] * 0.7:
            return False
    return True


# ---------- Last resort: what the singer says ----------
_WHISPER = {"model": None}
_WHISPER_LOCK = threading.Lock()


def transcribe(path: str) -> str:
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return ""
    if not _WHISPER_LOCK.acquire(timeout=WHISPER_WAIT):
        log.info("whisper busy, skipping the lyrics stage for this request")
        return ""
    try:
        if _WHISPER["model"] is None:
            _WHISPER["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _info = _WHISPER["model"].transcribe(
            path, vad_filter=False, beam_size=3, temperature=0.0,
            condition_on_previous_text=False, no_speech_threshold=0.4,
            compression_ratio_threshold=2.2)
        out, last = [], None
        for sg in segs:
            t = sg.text.strip()
            if not t or sg.no_speech_prob > 0.6 or sg.avg_logprob < -1.0:
                continue
            if t != last:
                out.append(t)
            last = t
        return " ".join(out).strip()
    finally:
        _WHISPER_LOCK.release()


def lyric_overlap(words, lyrics: str) -> float:
    ws = {_fold(w) for w in words if len(w) > 2}
    if not ws:
        return 0.0
    lw = set(re.findall(r"\w+", _fold(lyrics)))
    return len(ws & lw) / len(ws)


def _parse_yt_title(title: str, channel: str):
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")
    t = re.sub(r"(?i)\b(official|lyrics?|lyric video|audio|music video|video|hd|4k)\b|متن( آهنگ)?|با متن",
               " ", t)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    ch = re.sub(r"(?i)\s*-\s*topic$", "", channel or "").strip()
    return ch, (parts[0] if parts else "")


def yt_lyrics_search(words):
    slices = [words[:12]]
    if len(words) >= 20:
        slices.append(words[10:22])
    seen = set()
    for sl in slices:
        try:
            items = yt_search(" ".join(sl) + " lyrics", 6)
        except Exception as e:
            log.warning("yt lyrics search failed: %s", e)
            continue
        for it in items[:5]:
            if it.get("id") in seen:
                continue
            seen.add(it.get("id"))
            a, t = _parse_yt_title(it.get("title"), it.get("channel") or it.get("uploader") or "")
            for artist, name in ((a, t), (t, a)):
                if not artist or not name:
                    continue
                try:
                    ly = get_lyrics(artist, name)
                except Exception:
                    ly = None
                if ly and lyric_overlap(words, ly) >= 0.35:
                    return {"title": name, "artist": artist, "link": None}
                if ly:
                    break
    return None


def lyrics_identify(paths):
    texts, empty = [], 0
    for p in paths[:3]:
        t = transcribe(p)
        if t:
            texts.append(t)
        else:
            empty += 1
            if empty >= 2 and not texts:
                return None
    if not texts:
        return None
    words = re.findall(r"\w+", " ".join(texts).lower())
    if len(words) < 6:
        return None
    best = max(texts, key=len)
    return yt_lyrics_search(re.findall(r"\w+", best.lower()) or words)


def find_song_by_words(words):
    return yt_lyrics_search(words)


def recognize(paths, skip=(), extra=None):
    """Order (accuracy first, but fast):
    0) own fingerprint DB (free, instant)
    1) ACRCloud + Shazam on every piece AT THE SAME TIME, in waves (3, 3, 4 pieces). Answers are voted as they
       arrive; the moment the vote is confirmed the answer is returned and the slow rest is ignored.
       A new wave starts only while the answer is not confirmed yet
    2) lyrics (what the singer says) as an extra vote, 3) slowed / sped-up copies as the last try.
    The result has an "alts" list with the runner-up answers.
    STRICT=1: an unconfirmed answer is dropped ("not found") instead of being guessed."""
    if isinstance(paths, str):
        paths = [paths]
    skip = tuple(skip)
    rejected = {t[2:] for t in skip if t.startswith("k:")}

    if "local#0" not in skip:
        loc = local_identify(paths)
        if loc and title_key(loc["title"]) not in rejected:
            loc["alts"] = []
            return loc

    primary = _active_engines(skip, PRIMARY_ENGINES)
    order = {n.strip(): i for i, n in enumerate(ENGINE_ORDER)}
    hits = []
    if extra and extra.get("title") and "meta#0" not in skip and title_key(extra["title"]) not in rejected:
        hits.append({"song": dict(extra, engine="meta#0"), "engine": "meta#0", "sample": 98,
                     "w": extra.get("w", META_W)})

    def absorb(name, idx, res):
        if not res or title_key(res.get("title")) in rejected:
            return
        more = res.pop("more", [])
        base_w = ENGINE_W.get(name, 0.8) - 0.01 * order.get(name, 0)
        hits.append({"song": res, "engine": res["engine"], "sample": idx,
                     "w": base_w * res.get("wmul", 1.0)})
        for mo in more:
            if title_key(mo.get("title")) in rejected:
                continue
            mo["engine"] = res["engine"]
            hits.append({"song": mo, "engine": mo["engine"], "sample": idx,
                         "w": base_w * mo.get("wmul", 0.5)})

    def run_wave(batch, jobs) -> bool:
        """Sends every piece of the wave to BOTH engines at once. Votes are counted as answers arrive and the
        wave stops early as soon as the answer is confirmed. Returns True when confirmed."""
        futs = {ENGINE_POOL.submit(call_engine, name, accs, p): (name, idx)
                for idx, p in batch for name, accs in jobs}
        if not futs:
            return False
        pending = set(futs)
        deadline = time.time() + ENGINE_TIMEOUT
        confirmed = False
        while pending and time.time() < deadline and not confirmed:
            done, pending = wait(pending, timeout=min(max(deadline - time.time(), 0.05), 1.0),
                                 return_when=FIRST_COMPLETED)
            for f in done:
                name, idx = futs[f]
                try:
                    absorb(name, idx, f.result())
                except Exception as e:
                    log.warning("engine %s crashed: %s", name, e)
            if done and _confident(_group(hits)):
                confirmed = True
        for f in pending:
            f.cancel()  # not started yet -> never sent to the engine, no quota used
        return confirmed

    pieces = list(enumerate(paths[:MAX_SAMPLES]))
    pos = 0
    for size in (3, 3, 4):
        batch = pieces[pos:pos + size]
        pos += size
        if not batch:
            break
        if run_wave(batch, primary):
            break
    ranked = _group(hits)

    if LYRICS_ENGINE and "lyrics#0" not in skip and not _confident(ranked):
        try:
            ly = lyrics_identify(paths)
            if ly and title_key(ly.get("title")) not in rejected:
                ly["engine"] = "lyrics#0"
                hits.append({"song": ly, "engine": "lyrics#0", "sample": 99, "w": ENGINE_W["lyrics"]})
                ranked = _group(hits)
        except Exception as e:
            log.warning("lyrics stage failed: %s", e)

    if not _confident(ranked) and paths and primary:
        try:
            vs = make_variants(paths[0])
            if vs:
                run_wave(list(enumerate(vs, start=50)), primary)
                ranked = _group(hits)
        except Exception as e:
            log.warning("variant stage failed: %s", e)

    if not ranked:
        return None
    log.info("recognize: %s", [(r["song"].get("title"), r["song"].get("artist"), round(r["score"], 2), r["solid"])
                               for r in ranked[:3]])
    if STRICT and not ranked[0]["solid"]:
        log.info("recognize: best answer is not confirmed, STRICT is on -> not found")
        return None
    top = ranked[0]["song"]
    top["alts"] = [r["song"] for r in ranked[1:4]]
    return top


# ======================================================================================
# Captions & keyboards
# ======================================================================================
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


# ======================================================================================
# Sending audio
# ======================================================================================
def song_keys(song: dict) -> list:
    tk = title_key(song.get("title"))
    arts = sorted(artist_set(song.get("artist"), song.get("title")))[:4] or [""]
    keys = [f"s2:{tk}|{a}" for a in arts]
    keys.append("s:" + f"{song.get('artist', '')}|{song.get('title', '')}".lower())
    return keys


def info_link(a: dict, song=None) -> str:
    if a.get("info"):
        return a["info"]
    if a.get("vid"):
        return f"https://song.link/y/{a['vid']}"
    if a.get("url"):
        return a["url"]
    q = quote(f"{(song or {}).get('artist', '')} {(song or {}).get('title') or a.get('title', '')}".strip())
    return f"https://music.youtube.com/search?q={q}"


async def fetch_audio(target: str, song=None) -> dict:
    keys = [target] + (song_keys(song) if song else [])
    for k in keys:
        hit = await asyncio.to_thread(cache_get, k)
        if hit:
            return {"file_id": hit["file_id"], "title": hit["title"], "info": hit["info"]}
    tmp = tempfile.mkdtemp(dir=DL_DIR)
    fb = None
    if song:
        fb = f"{song['artist']} - {song['title']}"
    elif target.startswith("ytsearch1:"):
        fb = target[10:]
    try:
        async with dl_sem():
            a = await asyncio.to_thread(download_audio, target, tmp, fb)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    a["dir"] = tmp
    a["keys"] = keys
    return a


async def prepare_song_audio(song: dict) -> dict:
    """Downloads the ORIGINAL recording. Candidates come from pick_targets (best first). If the downloaded
    length is clearly different from the real song length, the next candidate is tried."""
    targets = await asyncio.to_thread(pick_targets, song)
    want = song.get("dur")
    err = None
    for i, t in enumerate(targets):
        last = i == len(targets) - 1
        try:
            a = await fetch_audio(t, song)
        except Exception as e:
            err = e
            if last:
                raise
            continue
        got = a.get("duration")
        if want and got and not last and "file_id" not in a and abs(got - want) > 20:
            log.info("wrong length (%ss vs %ss) for %s, trying the next candidate", got, int(want), t)
            shutil.rmtree(a["dir"], ignore_errors=True)
            continue
        return a
    raise err or RuntimeError("no download candidate")


async def deliver_audio(msg, a: dict, song=None, lang: str = "fa"):
    title = (song or {}).get("title") or a["title"]
    performer = (song or {}).get("artist") or None
    tsong = {"title": title, "artist": performer or ""}
    kb = audio_keyboard(await asyncio.to_thread(track_put, tsong), tsong, lang)
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
            await asyncio.to_thread(cache_put, k, sent.audio.file_id, title, info)


async def send_mp3(msg, target: str, title=None, performer=None, lang: str = "fa"):
    song = {"title": title, "artist": performer or ""} if title else None
    a = await fetch_audio(normalize_target(target), song)
    await deliver_audio(msg, a, song, lang)


# ======================================================================================
# Clips
# ======================================================================================
async def upload_video(msg, path: str, caption: str, kb=None):
    with open(path, "rb") as f:
        return await msg.reply_video(f, caption=caption, parse_mode="HTML", reply_markup=kb,
                                     supports_streaming=True, read_timeout=300, write_timeout=300)


async def identify_audio_and_reply(msg, src: str, audio_path: str, meta, lang: str):
    uid = msg.from_user.id
    trusted = bool(meta) and host_in(src, YT_HOSTS)
    sid = uuid.uuid4().hex[:10]
    charged = False
    song = None
    try:
        if trusted:
            song = {"title": meta["title"], "artist": meta["artist"], "link": src, "engine": "meta#0"}
            with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                json.dump(dict(song), f, ensure_ascii=False)
            await asyncio.to_thread(add_sample, sid, "", src)
        else:
            await asyncio.to_thread(add_sample, sid, "", src)
            song = await asyncio.to_thread(quick_local, audio_path, sid)  # own DB first: no quota
            if not song:
                if not await asyncio.to_thread(use_quota, uid):
                    return await msg.reply_text(tr(lang, "quota"))
                charged = True
                outs = await asyncio.to_thread(make_samples, audio_path, sid)
                if not outs:
                    await asyncio.to_thread(refund_quota, uid)
                    return await msg.reply_text(tr(lang, "no_music"))
                song = await asyncio.to_thread(recognize, outs, (), meta)
                if not song:
                    await asyncio.to_thread(refund_quota, uid)
                    return await msg.reply_text(tr(lang, "not_found"))
    except Exception:
        if charged and not song:
            await asyncio.to_thread(refund_quota, uid)
        raise
    remember_alts(sid, song)
    set_last(sid, song)
    clip_set_song(src, song)
    audio_task = asyncio.create_task(prepare_song_audio(song))
    await msg.reply_text(result_caption(song, src), parse_mode="HTML",
                         reply_markup=result_keyboard(sid, song, lang), **NO_PREVIEW)
    try:
        a = await audio_task
        await deliver_audio(msg, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await msg.reply_text(tr(lang, "err"))


async def do_ig_post(msg, src: str, tmp: str, lang: str):
    got = await asyncio.to_thread(ig_post_audio, src, tmp)
    if not got:
        return await msg.reply_text(tr(lang, "no_music"))
    await identify_audio_and_reply(msg, src, got["path"], got.get("meta"), lang)


async def do_audio_only(msg, src: str, tmp: str, lang: str, meta=None):
    try:
        async with dl_sem():
            path, meta2 = await asyncio.to_thread(download_source_audio, src, tmp)
    except Exception as e:
        log.warning("audio-only download failed: %s", str(e)[:150])
        return await msg.reply_text(tr(lang, "clip_missing"))
    await identify_audio_and_reply(msg, src, path, meta or meta2, lang)


async def identify_clip(uid: int, sid: str, meta, src: str, samples_task, quick_task=None):
    """Returns (song or None, quota_blocked). The own DB is checked first and costs no quota."""
    charged = False
    try:
        meta_song = None
        if meta:
            meta_song = {"title": meta["title"], "artist": meta["artist"],
                         "link": src if "youtu" in src else None, "engine": "meta#0",
                         "w": meta.get("w", META_W)}
            if host_in(src, YT_HOSTS):
                meta["link"] = src
                with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
                return meta_song, False
        if quick_task:
            try:
                qs = await quick_task
            except Exception:
                qs = None
            if qs:
                return qs, False
        if not await asyncio.to_thread(use_quota, uid):
            if meta_song:
                return meta_song, False
            return None, True
        charged = True
        outs = await samples_task
        if not outs:
            if not meta_song:
                await asyncio.to_thread(refund_quota, uid)
            return meta_song, False
        song = await asyncio.to_thread(recognize, outs, (), meta_song)
        if not song:
            await asyncio.to_thread(refund_quota, uid)
        return song, False
    except Exception:
        log.exception("identify_clip failed")
        if charged:
            try:
                await asyncio.to_thread(refund_quota, uid)
            except Exception:
                pass
        return None, False


async def prep_clip(uid: int, sid: str, meta, src: str, samples_task, quick_task=None) -> dict:
    """Background job: recognize, then download the mp3. The song name is published in PREP[sid]["song"]
    as soon as it is known, so the button does not wait for the mp3."""
    res = {"song": None, "audio": None, "blocked": False}
    try:
        song, blocked = await identify_clip(uid, sid, meta, src, samples_task, quick_task)
        res["song"], res["blocked"] = song, blocked
        if song and sid in PREP:
            PREP[sid]["song"] = song
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

    PREP[sid] = {"task": asyncio.create_task(prep()), "src": src, "created": time.time(),
                 "used": False, "busy": False, "song": song}
    try:
        await msg.reply_video(hit["file_id"], caption=footer(src), parse_mode="HTML",
                              reply_markup=clip_keyboard(sid, lang), supports_streaming=True)
    except Exception:
        drop_entry(sid)
        raise


async def do_clip(msg, url: str):
    uid = msg.from_user.id
    lang = await asyncio.to_thread(get_lang, uid)
    src = norm_url(url)

    hit = await asyncio.to_thread(clip_get, src)
    if hit:
        try:
            return await clip_from_cache(msg, src, hit, lang)
        except Exception:
            log.exception("clip cache send failed, re-downloading")
            await asyncio.to_thread(clip_delete, src)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            async with dl_sem():
                path, title, meta = await asyncio.to_thread(download_clip, src, tmp)
        except Exception as e:
            if "instagram.com" in src:
                log.warning("instagram clip download failed (%s): trying it as a photo post", str(e)[:100])
                return await do_ig_post(msg, src, tmp, lang)
            log.warning("clip download failed (%s): trying audio only", str(e)[:100])
            return await do_audio_only(msg, src, tmp, lang)
        if not path:
            return await do_audio_only(msg, src, tmp, lang, meta)
        if not meta and "instagram.com" in src:
            try:
                got = await asyncio.to_thread(ig_post_audio, src, tmp, False)
                meta = (got or {}).get("meta")
            except Exception:
                log.exception("instagram music name failed")
        sid = uuid.uuid4().hex[:10]
        await asyncio.to_thread(add_sample, sid, "", src)

        # background: quick own-DB check + cut samples run side by side, then recognize + mp3
        quick_task = asyncio.create_task(asyncio.to_thread(quick_local, path, sid))
        samples_task = asyncio.create_task(asyncio.to_thread(make_samples, path, sid))
        prep_task = asyncio.create_task(prep_clip(uid, sid, meta, src, samples_task, quick_task))
        PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False,
                     "busy": False, "song": None}
        try:
            sent = await upload_video(msg, path, footer(src), clip_keyboard(sid, lang))
            fid = _msg_video_id(sent)
            if fid:
                await asyncio.to_thread(clip_put, src, fid)
        except Exception:
            drop_entry(sid)
            raise
        finally:
            # the clip file lives in tmp: sample cutting must finish before we leave
            await asyncio.gather(samples_task, quick_task, return_exceptions=True)


async def show_prepared(q, sid: str, entry: dict, lang: str):
    """The user tapped 'identify'. The ONLY progress indicator is the status at the top of the chat; the button
    under the clip is left untouched. The result appears as soon as the name is known."""
    if entry.get("used") or entry.get("busy"):
        return await q.answer()
    entry["busy"] = True
    await q.answer()  # clears the button spinner, no text
    task = entry["task"]
    t0 = time.time()
    async with chat_action(q.get_bot(), q.message.chat_id):  # top-of-chat status stays on until found
        while not (entry.get("song") or task.done()) and time.time() - t0 < 240:
            await asyncio.sleep(0.3)
    res = task.result() if task.done() and not task.cancelled() else {}
    song = entry.get("song") or res.get("song")
    if not song:
        drop_entry(sid)  # the next tap recognizes again
        key = "quota_short" if res.get("blocked") else "not_found"
        return await q.message.reply_text(tr(lang, key))
    if entry.get("used"):
        return
    entry["used"] = True
    src = entry["src"]
    try:
        await q.edit_message_caption(caption=result_caption(song, src), parse_mode="HTML",
                                     reply_markup=result_keyboard(sid, song, lang))
    except Exception:
        log.warning("edit caption failed", exc_info=True)
    try:
        async with chat_action(q.get_bot(), q.message.chat_id):  # keep the status while the mp3 is prepared
            r2 = await asyncio.wait_for(asyncio.shield(task), timeout=240)
            a = (r2 or {}).get("audio") or await prepare_song_audio(song)
        await deliver_audio(q.message, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))
    PREP.pop(sid, None)


# ======================================================================================
# Voice / audio / typed text from the user
# ======================================================================================
async def handle_media(msg, media, lang: str):
    """Voice / audio / video note. No status message is posted: handle() keeps the top-of-chat status on
    until the song is found."""
    uid = msg.from_user.id
    fid = media.file_unique_id
    sid = uuid.uuid4().hex[:10]
    song = await asyncio.to_thread(media_cache_get, fid)
    charged = False
    try:
        if song:
            await asyncio.to_thread(add_sample, sid, "")
            set_last(sid, {**song, "engine": "cache#0"})
        else:
            size = getattr(media, "file_size", 0) or 0
            if size > 20 * 1024 * 1024:
                return await msg.reply_text(tr(lang, "err"))
            if not await asyncio.to_thread(use_quota, uid):
                return await msg.reply_text(tr(lang, "quota"))
            charged = True
            raw = os.path.join(SAMPLES_DIR, sid + ".raw")
            await (await media.get_file()).download_to_drive(raw)

            # 1) own database first: usually under a second, costs no quota
            song = await asyncio.to_thread(quick_local, raw, sid)
            if song:
                await asyncio.to_thread(refund_quota, uid)
                charged = False
                song["alts"] = []
                try:
                    os.remove(raw)
                except OSError:
                    pass
                await asyncio.to_thread(add_sample, sid, "")
                remember_alts(sid, song)
                set_last(sid, song)
            else:
                # 2) full recognition (ACR + Shazam together)
                outs = await asyncio.to_thread(make_samples, raw, sid)
                if outs:
                    os.remove(raw)
                else:
                    fallback = os.path.join(SAMPLES_DIR, f"{sid}_0.ogg")
                    os.replace(raw, fallback)
                    outs = [fallback]
                await asyncio.to_thread(add_sample, sid, outs[0])
                song = await asyncio.to_thread(recognize, outs)
                if not song:
                    await asyncio.to_thread(refund_quota, uid)
                    charged = False
                    return await msg.reply_text(tr(lang, "not_found"))
                charged = False
                remember_alts(sid, song)
                set_last(sid, song)
    except Exception:
        if charged:
            await asyncio.to_thread(refund_quota, uid)
        raise
    await asyncio.to_thread(media_cache_put, fid, song, sid)
    audio_task = asyncio.create_task(prepare_song_audio(song))
    await msg.reply_text(result_caption(song), parse_mode="HTML",
                         reply_markup=result_keyboard(sid, song, lang), **NO_PREVIEW)
    try:
        a = await audio_task
        await deliver_audio(msg, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await msg.reply_text(tr(lang, "err"))


async def try_lyrics_search(msg, text: str, lang: str) -> bool:
    words = re.findall(r"\w+", _fold(text))
    song = await asyncio.to_thread(find_song_by_words, words)
    if not song:
        return False
    audio_task = asyncio.create_task(prepare_song_audio(song))
    kb = Markup(result_keyboard("x", song, lang).inline_keyboard[:-1])
    await msg.reply_text(result_caption(song), parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
    try:
        a = await audio_task
        await deliver_audio(msg, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await msg.reply_text(tr(lang, "err"))
    return True


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    uid = msg.from_user.id
    await asyncio.to_thread(touch_user, msg.from_user)
    lang = await asyncio.to_thread(get_lang, uid)
    if not rate_ok(uid):
        return await msg.reply_text(tr(lang, "slow"))
    media = msg.voice or msg.audio or msg.video_note
    text = (msg.text or "").strip()
    url = first_url(text)
    is_clip = bool(url and host_in(url, CLIP_HOSTS) and host_of(url) != "music.youtube.com")
    action = ChatAction.UPLOAD_VIDEO if is_clip else ChatAction.RECORD_VOICE
    try:
        # the status at the top of the chat stays on for the WHOLE request (voice, clip link, lyrics text,
        # song name) until the answer is sent
        async with chat_action(ctx.bot, msg.chat_id, action):
            if media:
                await handle_media(msg, media, lang)
            elif text.startswith(MP3_PREFIX):
                await send_mp3(msg, text[len(MP3_PREFIX):].strip(), lang=lang)
            elif is_clip:
                await do_clip(msg, url)
            elif url:
                target, _ = await asyncio.to_thread(resolve, text)
                await send_mp3(msg, target, lang=lang)
            else:
                if len(re.findall(r"\w+", text)) >= 5 and await try_lyrics_search(msg, text, lang):
                    return
                await send_results(msg, text, lang)
    except Exception:
        log.exception("handle failed")
        await msg.reply_text(tr(lang, "err"))


# ======================================================================================
# Buttons
# ======================================================================================
async def on_rec(q, data: str):
    action, sid = data.split(":", 1)
    uid = q.from_user.id
    lang = await asyncio.to_thread(get_lang, uid)

    if action == "rec":
        entry = PREP.get(sid)
        if entry:
            return await show_prepared(q, sid, entry, lang)
    else:
        drop_entry(sid)
        await asyncio.to_thread(media_cache_drop, sid)

    with db() as c:
        row = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    if row and action == "bad" and row["src"]:
        await asyncio.to_thread(clip_delete, row["src"])
    paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, f"{sid}_*")))
    mpath = os.path.join(SAMPLES_DIR, f"{sid}.json")
    if not row or not (paths or os.path.exists(mpath) or CANDS.get(sid)):
        return await q.answer(tr(lang, "expired"), show_alert=True)
    tried = [t for t in (row["tried"] or "").split(",") if t]
    if action == "bad" and row["last"]:
        eng, _, key = row["last"].partition("||")
        if key:
            tried.append("k:" + key)
        if eng and (not key or eng in ("meta#0", "lyrics#0", "cache#0", "local#0")):
            tried.append(eng)
        with db() as c:
            c.execute("UPDATE samples SET tried=? WHERE id=?", (",".join(tried), sid))
    rejected = {t[2:] for t in tried if t.startswith("k:")}
    song = None
    if action == "bad":
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
        if not await asyncio.to_thread(use_quota, uid):
            return await q.answer(tr(lang, "quota_short"), show_alert=True)
        await q.answer()  # no text: the status at the top of the chat shows the progress
        try:
            async with chat_action(q.get_bot(), q.message.chat_id):
                song = await asyncio.to_thread(recognize, paths, tuple(tried))
        except Exception:
            await asyncio.to_thread(refund_quota, uid)
            raise
        if song:
            remember_alts(sid, song)
        else:
            await asyncio.to_thread(refund_quota, uid)
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
        await asyncio.to_thread(clip_set_song, src, song)
        vid = getattr(q.message, "video", None)
        if vid:
            await asyncio.to_thread(clip_put, src, vid.file_id, song)
    try:
        async with chat_action(q.get_bot(), q.message.chat_id):
            a = await audio_task
        await deliver_audio(q.message, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))


async def on_lyrics(q, data: str):
    lang = await asyncio.to_thread(get_lang, q.from_user.id)
    tid = data.split(":", 1)[1]
    with db() as c:
        row = c.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        return await q.answer(tr(lang, "track_missing"), show_alert=True)
    await q.answer()  # no text: the status at the top of the chat shows the progress
    async with chat_action(q.get_bot(), q.message.chat_id, ChatAction.TYPING):
        text = await asyncio.to_thread(get_lyrics, row["artist"], row["title"])
    if not text:
        return await q.message.reply_text(tr(lang, "lyrics_none"))
    for chunk in split_text(f"🎤 {row['title']}\n\n{text}"):
        await q.message.reply_text(chunk)


_VID_RE = re.compile(r"^[\w-]{6,20}$")


async def on_pick(q, ctx, data: str):
    lang = await asyncio.to_thread(get_lang, q.from_user.id)
    vid = data.split(":", 1)[1]
    if not _VID_RE.match(vid):
        return await q.answer()
    if not rate_ok(q.from_user.id, limit=RATE_LIMIT * 2):
        return await q.answer(tr(lang, "slow"), show_alert=True)
    await q.answer()
    try:
        async with chat_action(ctx.bot, q.message.chat_id, ChatAction.RECORD_VOICE):
            await send_mp3(q.message, f"https://www.youtube.com/watch?v={vid}", lang=lang)
    except Exception:
        log.exception("pick failed")
        await q.message.reply_text(tr(lang, "err"))


async def on_more(q, data: str):
    lang = await asyncio.to_thread(get_lang, q.from_user.id)
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
    await asyncio.to_thread(set_lang, q.from_user.id, code)
    toast = tr(code, "lang_set") if code in STR else "✅ " + LANGS[code]
    await q.answer(toast)
    text = tr(code, "welcome").rsplit("\n\n", 1)[0] + "\n\n🌐 /lang"
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=None, **NO_PREVIEW)
    except Exception:
        pass


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await asyncio.to_thread(touch_user, q.from_user)
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


# ======================================================================================
# Inline search by artist
# ======================================================================================
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


# ======================================================================================
# Commands / lifecycle
# ======================================================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(touch_user, update.effective_user)
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_keyboard())


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(touch_user, update.effective_user)
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_keyboard())


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("update error", exc_info=ctx.error)


async def post_init(app: Application):
    app.bot_data["maintenance"] = asyncio.create_task(maintenance_loop())


async def post_shutdown(app: Application):
    t = app.bot_data.get("maintenance")
    if t:
        t.cancel()
    for sid in list(PREP):
        drop_entry(sid)
    ENGINE_POOL.shutdown(wait=False, cancel_futures=True)
    if _BG:
        _BG.stop()


def main():
    migrate()
    seed_from_env()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    for name, fn in [("start", cmd_start), ("lang", cmd_lang)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
        handle,
    ))
    app.add_error_handler(on_error)
    try:
        from admin import register as register_admin
        register_admin(app, sys.modules[__name__])
    except Exception:
        log.exception("admin module not loaded - the bot continues without it")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
