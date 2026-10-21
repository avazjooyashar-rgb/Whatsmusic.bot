"""
Music recognition Telegram bot  -  v4 (quota-friendly)

  * clips of ANY length: short ones are used whole, long ones are sampled over the whole file,
    too long / too big for Telegram -> audio-only identification
  * quota is refunded when nothing is recognized or something crashes
  * DB calls on the hot paths run in threads (no event-loop blocking)
  * audio download folders cannot leak (own folder + periodic cleanup)
  * speed: shared thread pool, vote stops when two engines agree, one Shazam client
  * safety: exact host matching, download whitelist, atomic quota, rate limit
  * NEW: ACRCloud + Shazam are asked first; AudD (small quota) is only the backup: ONE request on ONE
         short piece, and only when the first two are not sure
  * NEW: an account that reports "limit reached" / bad key is paused automatically (no more wasted requests)
  * NEW: AudD lyrics search only after the free YouTube lyrics search failed
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
AUDD_TOKEN = os.environ.get("AUDD_TOKEN")
ODESLI_KEY = os.environ.get("ODESLI_KEY")  # optional
ENGINE_ORDER = os.environ.get("ENGINES", "acr,audd,shazam").split(",")
PRIMARY_ENGINES = ("acr", "shazam")  # generous engines: always asked first, on several pieces
BACKUP_ENGINES = ("audd",)  # small quota: asked ONLY when the primary engines are not sure, on ONE piece
COOLDOWN_SEC = int(os.environ.get("COOLDOWN_SEC", str(6 * 3600)))  # pause of an account that is out of quota
AUDD_SEC = int(os.environ.get("AUDD_SEC", "12"))  # length of the piece sent to AudD (short = cheap)
LYRICS_ENGINE = os.environ.get("LYRICS_ENGINE", "1") == "1"  # last resort: transcribe singing, search words
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")  # tiny = lighter, base = fast, small = accurate
WHISPER_WAIT = int(os.environ.get("WHISPER_WAIT", "25"))  # seconds a request may wait for the Whisper lock
HUM_MIN_SCORE = int(os.environ.get("HUM_MIN_SCORE", "30"))  # ACRCloud humming matches below this (0-100) are ignored
MAX_SAMPLES = max(int(os.environ.get("MAX_SAMPLES", "7")), 7)  # recognize() listens to at most 7 pieces
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "10"))  # messages per user per minute (0 = off)
DL_CONCURRENCY = int(os.environ.get("DL_CONCURRENCY", "4"))  # parallel yt-dlp downloads
ENGINE_THREADS = int(os.environ.get("ENGINE_THREADS", "12"))
ENGINE_TIMEOUT = int(os.environ.get("ENGINE_TIMEOUT", "45"))  # max wait for one batch of engine answers
CLIP_MAX_SEC = int(os.environ.get("CLIP_MAX_SEC", "900"))  # longer videos are not sent, only identified
MAX_SRC_SEC = int(os.environ.get("MAX_SRC_SEC", str(3 * 3600)))  # longest source we are willing to scan
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")
DL_DIR = os.path.join(SAMPLES_DIR, "dl")  # temp folders of audio downloads (cleaned periodically)
COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_BYTES = 49 * 1024 * 1024
ARIA2 = bool(shutil.which("aria2c")) and os.environ.get("ARIA2", "1") == "1"
os.makedirs(SAMPLES_DIR, exist_ok=True)
os.makedirs(DL_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
# hosts are matched EXACTLY (host == d or host endswith .d), never by substring
CLIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com")
DIRECT_HOSTS = CLIP_HOSTS
AUDIO_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")  # the only places yt-dlp may download audio from
MP3_PREFIX = "🎵 "

esc = html.escape

PREP = {}   # sid -> {"task", "src", "created", "used"}  (clips being prepared in the background)
CANDS = {}  # sid -> {"alts": [...], "created": ts}      (runner-up answers for the "wrong song" button)
SEARCH = {}  # sid -> {"items": [...], "created": ts}
PAGE = 8

# shared HTTP session (keep-alive + bigger pool: many threads talk to the same APIs)
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
# Small helpers: chat action, rate limit, URL / host handling
# ======================================================================================
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


_RATE = {}


def rate_ok(uid: int, limit: int = None, window: int = 60) -> bool:
    """Sliding window per user. Admins are never limited."""
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
    """Exact host match: 'music.youtube.com' is OK for 'youtube.com', 'evil.com/?youtube.com' is not."""
    h = host_of(url)
    return bool(h) and any(h == d or h.endswith("." + d) for d in hosts)


def norm_url(url: str) -> str:
    """Clean a link so the same clip always gets the same cache key."""
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
    """Whitelist for everything that is handed to yt-dlp. A user can never make the server
    open an arbitrary / internal URL: only YouTube / SoundCloud links or a search query pass."""
    target = (target or "").strip()
    if URL_RE.match(target):
        if not host_in(target, AUDIO_HOSTS):
            raise ValueError("unsupported link")
        return target
    if re.match(r"(?:yt|sc)search1:", target):
        return target
    return f"ytsearch1:{target}"


# ======================================================================================
# Song / artist matching (two singers, feat., remaster, ...)
# ======================================================================================
_SPLIT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_FEAT_RE = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\s+([^\)\]]+)", re.I)

# titles of versions that are NOT the original studio song: they lose against a normal answer
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


# ======================================================================================
# SQLite  (connections are really closed, WAL, one-time schema creation)
# ======================================================================================
class _Conn(sqlite3.Connection):
    """`with db() as c:` commits / rolls back AND closes the connection (plain sqlite3 only commits)."""

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
                c.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
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


# same voice / audio file -> same answer (keyed by Telegram's file_unique_id)
def media_cache_get(uid: str):
    with db() as c:
        r = c.execute("SELECT song FROM media_songs WHERE uid=?", (uid,)).fetchone()
    return json.loads(r[0]) if r else None


def media_cache_put(uid: str, song: dict, sid: str):
    data = json.dumps({k: song.get(k) for k in ("title", "artist", "link", "yt")}, ensure_ascii=False)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO media_songs VALUES(?,?,?)", (uid, data, sid))


def media_cache_drop(sid: str):
    """'Wrong song' pressed: forget the cached answer for that file."""
    with db() as c:
        c.execute("DELETE FROM media_songs WHERE sid=?", (sid,))


def track_put(song: dict) -> str:
    """Short id for callback buttons."""
    title, artist = song.get("title") or "", song.get("artist") or ""
    tid = hashlib.md5(f"{artist}|{title}".lower().encode()).hexdigest()[:10]
    with db() as c:
        c.execute("INSERT OR REPLACE INTO tracks VALUES(?,?,?)", (tid, title, artist))
    return tid


def use_quota(uid: int) -> bool:
    """ATOMIC: the check and the increment are one UPDATE, so parallel requests cannot slip past the limit."""
    limit = daily_limit()
    if limit <= 0 or uid in ADMIN_IDS:
        return True
    day = datetime.date.today().isoformat()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO usage VALUES(?,?,0)", (uid, day))
        cur = c.execute("UPDATE usage SET n=n+1 WHERE uid=? AND day=? AND n<?", (uid, day, limit))
        return cur.rowcount > 0


def refund_quota(uid: int):
    """Give back one recognition when it produced nothing (bad audio, crash...)."""
    if uid in ADMIN_IDS or daily_limit() <= 0:
        return
    day = datetime.date.today().isoformat()
    with db() as c:
        c.execute("UPDATE usage SET n=n-1 WHERE uid=? AND day=? AND n>0", (uid, day))


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


# ======================================================================================
# Background housekeeping (runs every 10 minutes, not on every message)
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
    # orphan files (bot restarted in the middle of a job)
    try:
        for name in os.listdir(SAMPLES_DIR):
            p = os.path.join(SAMPLES_DIR, name)
            if os.path.isfile(p) and now - os.path.getmtime(p) > 86400:
                os.remove(p)
    except OSError:
        pass
    # audio download folders nobody cleaned (cancelled / crashed jobs)
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
    """Forget a prepared clip. A job that is still running is NOT cancelled half-way:
    its audio folder is removed as soon as it finishes (no leaked files)."""
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
# Persistent background event loop (Shazam keeps ONE client instead of a new loop per call)
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
# Account protection: an account that is out of quota / has a bad key is paused automatically
# ======================================================================================
class QuotaError(Exception):
    """The engine account is out of quota or its key is rejected: stop using it for a while."""


_COOLDOWN = {}  # account id -> time until which the account is not used (kept in memory)


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
# Engine 1: ACRCloud   Engine 2: AudD   Engine 3: Shazam
# ======================================================================================
def _hum_score(h: dict) -> float:
    """ACRCloud humming scores come as 0..1 (e.g. 0.43). Everything here uses 0..100."""
    s = h.get("score") or 0
    try:
        s = float(s)
    except (TypeError, ValueError):
        return 0.0
    return s * 100 if s <= 1 else s


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
        timeout=40,
    ).json()
    st = r.get("status", {})
    code = st.get("code")
    if code in (3001, 3003, 3014):  # bad key / limit reached / bad signature
        raise QuotaError(f"acr {code}: {st.get('msg', '')}")
    if code != 0:
        if code not in (1001, 2004):  # 1001 = no result (normal), 2004 = not fingerprintable
            log.info("acr status %s: %s", code, st.get("msg", ""))
        return None
    md = r.get("metadata") or {}
    music = md.get("music") or []
    if not music:
        # melody match (covers, other singers, piano versions): needs the humming bucket in the project
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
            top["more"] = [dict(g, wmul=g["wmul"] * 0.6) for g in good[1:]]  # runner-up melody matches
            return top
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
    artist = ", ".join(a["name"] for a in (m.get("artists") or []) if a.get("name"))  # every singer
    return {"title": m["title"], "artist": artist, "link": link, "yt": yt}


def _audd_check(r: dict):
    """AudD answers 900 (bad token), 901 / 902 (trial or quota finished, limit reached)."""
    err = r.get("error") or {}
    code = err.get("error_code")
    if code in (900, 901, 902):
        raise QuotaError(f"audd {code}: {err.get('error_message', '')}")


def _trim_for_audd(path: str, sec: int):
    """A short copy of the piece: AudD charges by audio length, so a short piece is cheaper.
    Returns the temp file path, or None when ffmpeg failed (the original is used then)."""
    fd, out = tempfile.mkstemp(suffix=".mp3", dir=SAMPLES_DIR)
    os.close(fd)
    try:
        subprocess.run(["ffmpeg", "-y", "-i", path, "-t", str(sec), "-vn", "-ac", "1", "-b:a", "96k", out],
                       check=True, capture_output=True, timeout=60)
        return out
    except Exception:
        try:
            os.remove(out)
        except OSError:
            pass
        return None


def audd_identify(path: str, acc):
    small = _trim_for_audd(path, AUDD_SEC)
    try:
        with open(small or path, "rb") as f:
            r = HTTP.post(
                "https://api.audd.io/",
                data={"api_token": acc["key"], "return": "spotify,apple_music"},
                files={"file": f},
                timeout=40,
            ).json()
    finally:
        if small:
            try:
                os.remove(small)
            except OSError:
                pass
    _audd_check(r)
    res = r.get("result")
    if not res:
        return None
    link = (res.get("spotify") or {}).get("external_urls", {}).get("spotify") or res.get("song_link")
    return {"title": res["title"], "artist": res["artist"], "link": link}


ENGINE_FN = {"acr": acr_identify, "audd": audd_identify}


def _shazam_lyrics_text(track: dict) -> str:
    """Lyrics block inside a Shazam track (they come from Musixmatch, good for Persian too)."""
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

    out = bg_loop().run(go(), timeout=40)
    t = (out or {}).get("track")
    if not t:
        return None
    try:  # Shazam already sends the lyrics with the answer: keep them for the lyrics button
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
    r = HTTP.get("https://api.song.link/v1-alpha.1/links", params=params, timeout=timeout)
    if not r.ok:
        return None, None
    j = r.json()
    plat = j.get("linksByPlatform", {})
    yt = (plat.get("youtube") or plat.get("youtubeMusic") or {}).get("url")
    ent = j.get("entitiesByUniqueId", {}).get(j.get("entityUniqueId"), {})
    name = f"{ent.get('artistName', '')} - {ent.get('title', '')}".strip(" -")
    if len(_ODESLI) > 500:
        _ODESLI.clear()
    _ODESLI[link] = (time.time(), (yt, name))
    return yt, name


def pick_target(song: dict) -> str:
    """Fastest way to the audio: known YouTube link > Odesli > search."""
    if song.get("yt"):
        return song["yt"]
    link = song.get("link") or ""
    if host_in(link, ("youtube.com", "youtu.be")):
        return link
    if link:
        try:
            yt, _ = odesli_to_youtube(link, timeout=3)
            if yt and host_in(yt, ("youtube.com", "youtu.be")):
                return yt
        except Exception:
            pass
    return f"ytsearch1:{song['artist']} - {song['title']}"


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
    """Official music metadata (artist + track) that YouTube / Instagram sometimes provide."""
    if not info:
        return None
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    track = info.get("track")
    if artist and track:
        return {"artist": artist.strip(), "title": track}  # all singers, not only the first
    return None


def _len_filter(max_sec: int):
    # "<=?" lets videos pass whose duration is unknown; live streams are refused
    return yt_dlp.utils.match_filter_func(f"duration <=? {int(max_sec)} & !is_live")


def download_clip(url: str, outdir: str):
    """Video for sending to Telegram. Returns (path, title, meta).
    path is None when the video is too long / too big: the caller then identifies it audio-only."""
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
    """Audio only (low bitrate, small download) of a video of ANY length. Returns (path, meta)."""
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
# Instagram photo / carousel posts (their music is in the post data, yt-dlp refuses them)
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
    """Returns {"path": audio file or None, "meta": {"title", "artist", ...} or None} or None.
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
            except ValueError:  # an HTML login / challenge page instead of data
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
                    "engine": "meta#0", "w": 0.8}  # Instagram's music library name: fairly reliable
        if not want_audio:  # only the music name was needed
            return {"path": None, "meta": meta}
        if not audio_url:  # uploader's own sound
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
# Audio sampling (ffmpeg): works for ANY duration
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
    """raw=True: no filters at all (filters sometimes hurt the fingerprint of quiet / piano music)."""
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
    """Start times of the n loudest, non-overlapping windows (usually the chorus / the singing).
    Scans up to `maxsec` seconds. Uses numpy when available (about 50x faster)."""
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
            window_sum = pref[win:] - pref[:len(pref) - win]  # index i = energy of [i, i+win)
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
    """Where to cut the pieces.
    * unknown / very short (<= win+3 s): one piece from the start (= the whole clip)
    * otherwise: the loudest parts (often the chorus) + pieces spread EVENLY over the whole length,
      more pieces for longer files (3 up to 1 min, 4 up to 5 min, 6 beyond)."""
    if not d or d <= win + 3:
        return [0]
    n = 3 if d <= 60 else 4 if d <= 300 else 6
    loud = loud_offsets(src, win, 2, int(min(d, MAX_SRC_SEC)))
    even = [d * (k + 0.5) / n - win / 2 for k in range(n)]
    mid = even[n // 2]
    rest = [e for i, e in enumerate(even) if i != n // 2]
    cand = loud[:1] + [mid] + loud[1:2] + rest
    offs = []
    for o in cand:
        o = round(max(0, min(o, d - win)))
        if all(abs(o - x) >= 5 for x in offs):  # no near-duplicate pieces
            offs.append(o)
    return offs[:n]


def make_samples(src: str, sid: str) -> list:
    d = probe_duration(src)
    win = 15
    offs = pick_offsets(src, d, win)
    jobs = [(os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3"), o, False) for i, o in enumerate(offs)]
    # last piece: 20 s of the best part with NO filters (only used if the filtered ones fail)
    jobs.append((os.path.join(SAMPLES_DIR, f"{sid}_r.mp3"), offs[0], True))
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:  # all pieces are cut at the same time
        res = list(ex.map(lambda j: j[0] if cut_sample(src, j[0], j[1], 20 if j[2] else win, j[2]) else None, jobs))
    return [r for r in res if r]


def make_variants(src: str) -> list:
    """Slowed / sped-up (pitch + tempo) copies of a piece: reels often re-upload songs like that."""
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
# YouTube search (cached for 5 minutes: inline mode repeats the same queries a lot)
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
    url = first_url(text)
    if url:
        if host_in(url, DIRECT_HOSTS):
            return url, None
        yt, name = odesli_to_youtube(url, timeout=20)
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
    """Cache keys that do not depend on how an engine spells the title / singers."""
    tk = title_key(title)
    return [f"tk:{tk}|{a}" for a in sorted(artist_set(artist, title))[:4]]


def _store_lyrics(artist: str, title: str, text: str):
    with db() as c:
        for k in _lyr_keys(artist, title):
            c.execute("INSERT OR REPLACE INTO lyrics VALUES(?,?)", (k, text))


def shazam_lyrics(artist: str, title: str):
    """Ask Shazam for the lyrics of a song it may not have heard in this audio."""
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
    """lrclib: a result is accepted ONLY if its title/artist match the song."""
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
# Recognition: voting between engines
# ======================================================================================
ENGINE_W = {"acr": 1.0, "shazam": 1.0, "audd": 0.9, "lyrics": 1.2}
# Instagram's "original audio" label is chosen by the uploader and is often wrong: weak vote only.
META_W = 0.6


def _base(tag: str) -> str:
    return (tag or "").split("#")[0]


def _active_engines(skip, names=None) -> list:
    """Engines that may be asked now. `names` limits the list (PRIMARY_ENGINES / BACKUP_ENGINES).
    Accounts that are paused (out of quota / bad key) are left out."""
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


def _audd_accounts() -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM accounts WHERE enabled=1 AND type='audd' ORDER BY id").fetchall()
    return [r for r in rows if not cooling(r["id"])]


def call_engine(name: str, accs, path: str):
    """Ask ONE engine about ONE piece of audio. ACR is retried once on a network error;
    AudD is NOT retried (a timed-out request may already have been counted).
    An account that is out of quota is paused and the next account of the engine is tried."""
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
    attempts = 1 if name == "audd" else 2
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
                return None  # real "no match": don't burn the other accounts on the same audio
            except QuotaError as e:
                cool_down(r["id"], str(e))
                with db() as c:
                    c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
                break  # next account of this engine
            except Exception as e:
                log.warning("account %s failed (try %s): %s", r["id"], attempt, e)
                if attempt == attempts:
                    with db() as c:
                        c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
    return None


def _group(hits: list) -> list:
    """Votes -> ranked answers. The same song found by different engines / pieces of audio
    (even with a different spelling or a different number of singers) counts together.
    Mashup / remix / live / cover titles are pushed down."""
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
        mult = title_penalty(song)
        score *= mult
        solid = len(per) >= 2 or len({h["sample"] for h in hs}) >= 2
        ranked.append({"song": song, "score": score, "solid": solid, "bad": mult < 1})
    ranked.sort(key=lambda r: r["score"] + (1.0 if r["solid"] else 0.0), reverse=True)
    return ranked


def _confident(ranked: list) -> bool:
    """Good enough to stop listening: several votes agree and it is not a mashup/remix-like title."""
    return bool(ranked) and ranked[0]["solid"] and not ranked[0]["bad"]


# ---------- Last resort: what the singer says (speech -> words -> lyrics search) ----------
_WHISPER = {"model": None}
_WHISPER_LOCK = threading.Lock()  # one transcription at a time keeps RAM/CPU under control


def transcribe(path: str) -> str:
    """Speech-to-text of the SINGING with faster-whisper (optional: returns '' if not installed).
    If another transcription is running for longer than WHISPER_WAIT seconds, this one is skipped
    instead of building a queue. Segments whisper itself doesn't believe are speech are dropped."""
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
                continue  # hallucination over music, not singing
            if t != last:  # drop looping hallucinations
                out.append(t)
            last = t
        return " ".join(out).strip()
    finally:
        _WHISPER_LOCK.release()


def lyric_overlap(words, lyrics: str) -> float:
    """How many of the transcribed words really appear in the candidate song's lyrics."""
    ws = {_fold(w) for w in words if len(w) > 2}
    if not ws:
        return 0.0
    lw = set(re.findall(r"\w+", _fold(lyrics)))
    return len(ws & lw) / len(ws)


def audd_find_lyrics(words, acc):
    q = " ".join(words[:40])
    r = HTTP.get("https://api.audd.io/findLyrics/", params={"q": q, "api_token": acc["key"]},
                 timeout=30).json()
    _audd_check(r)
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


def _audd_lyrics_try(words):
    """findLyrics on the first usable AudD account. A paused / out-of-quota account is skipped."""
    for acc in _audd_accounts():
        try:
            res = audd_find_lyrics(words, acc)
            with db() as c:
                c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (acc["id"],))
            return res  # None = no match: don't spend the other accounts on the same words
        except QuotaError as e:
            cool_down(acc["id"], str(e))
        except Exception as e:
            log.warning("audd findLyrics failed: %s", e)
            with db() as c:
                c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (acc["id"],))
            break
    return None


def _parse_yt_title(title: str, channel: str):
    """'Artist - Song (Official Video)' -> (artist, song). Falls back to the channel name."""
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")
    t = re.sub(r"(?i)\b(official|lyrics?|lyric video|audio|music video|video|hd|4k)\b|متن( آهنگ)?|با متن",
               " ", t)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    ch = re.sub(r"(?i)\s*-\s*topic$", "", channel or "").strip()
    return ch, (parts[0] if parts else "")


def yt_lyrics_search(words):
    """Search the heard words on YouTube (two slices), then CHECK each candidate's real lyrics."""
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
            for artist, name in ((a, t), (t, a)):  # "Artist - Song" or "Song - Artist"
                if not artist or not name:
                    continue
                try:
                    ly = get_lyrics(artist, name)
                except Exception:
                    ly = None
                if ly and lyric_overlap(words, ly) >= 0.35:
                    return {"title": name, "artist": artist, "link": None}
                if ly:
                    break  # lyrics found but they don't match: don't try the swapped order
    return None


def lyrics_identify(paths):
    texts, empty = [], 0
    for p in paths[:3]:  # several pieces -> more words -> a much better match
        t = transcribe(p)
        if t:
            texts.append(t)
        else:
            empty += 1
            if empty >= 2 and not texts:
                return None  # no singing in the first two pieces: instrumental, skip the slow part
    if not texts:
        return None
    words = re.findall(r"\w+", " ".join(texts).lower())
    if len(words) < 6:  # instrumental, humming or too little singing
        return None
    best = max(texts, key=len)  # the longest transcript makes the best search query
    # 1) free: YouTube search verified against real lyrics
    res = yt_lyrics_search(re.findall(r"\w+", best.lower()) or words)
    if res:
        return res
    # 2) AudD findLyrics costs quota: only with enough words to make a good match
    if len(words) >= 12:
        return _audd_lyrics_try(words)
    return None


def find_song_by_words(words):
    """Typed lyrics -> song. YouTube search verified against real lyrics (free), then AudD."""
    res = yt_lyrics_search(words)
    if res:
        return res
    return _audd_lyrics_try(words)


def recognize(paths, skip=(), extra=None):
    """Listens like a human would:
    1) ACRCloud + Shazam (generous quota) listen at the same time on the best piece of audio;
       answers are collected AS THEY ARRIVE and the vote stops the moment two of them agree
    2) if not sure yet, more pieces are tried (2 at a time, at most 5 pieces)
    3) if still not sure, AudD (small quota) is asked ONCE on the best piece
    4) if nobody agrees, the singing is transcribed and matched with real lyrics
    5) if still nothing, slowed / sped-up versions of the audio are tried (primary engines only)
    `extra` is a weak hint (e.g. the audio name on an Instagram reel): it counts as one vote.
    The result has an "alts" list with the runner-up answers."""
    if isinstance(paths, str):
        paths = [paths]
    skip = tuple(skip)
    rejected = {t[2:] for t in skip if t.startswith("k:")}  # songs the user said are wrong
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
        for mo in more:  # runner-up melody matches
            if title_key(mo.get("title")) in rejected:
                continue
            mo["engine"] = res["engine"]
            hits.append({"song": mo, "engine": mo["engine"], "sample": idx,
                         "w": base_w * mo.get("wmul", 0.5)})

    def run_batch(batch, jobs):
        futs = {ENGINE_POOL.submit(call_engine, name, accs, p): (name, idx)
                for idx, p in batch for name, accs in jobs}
        if not futs:
            return
        pending = set(futs)
        deadline = time.time() + ENGINE_TIMEOUT
        while pending:
            left = deadline - time.time()
            if left <= 0:
                break
            done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
            for f in done:
                name, idx = futs[f]
                try:
                    absorb(name, idx, f.result())
                except Exception as e:
                    log.warning("engine %s crashed: %s", name, e)
            if _confident(_group(hits)):
                break  # two engines agree: no need to wait for the slow ones
        for f in pending:
            f.cancel()  # not started yet -> dropped; already running -> finishes quietly in the background

    pieces = list(enumerate(paths[:MAX_SAMPLES]))
    pos = 0
    ranked = _group(hits)
    for size in (1, 2, 2):  # best piece first; only listen to more if the engines don't agree yet
        batch = pieces[pos:pos + size]
        pos += size
        if not batch:
            break
        run_batch(batch, primary)
        ranked = _group(hits)
        if _confident(ranked):
            break
    ranked = _group(hits)

    # AudD = the last arrow: ONE short piece, only when the first engines are not sure
    if pieces and not _confident(ranked):
        backup = _active_engines(skip, BACKUP_ENGINES)
        if backup:
            run_batch(pieces[:1] if primary else pieces[:2], backup)
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

    if not ranked and paths and primary:  # nothing at all: try slowed / sped-up versions of the best piece
        try:
            vs = make_variants(paths[0])
            if vs:
                run_batch(list(enumerate(vs, start=50)), primary)
                ranked = _group(hits)
        except Exception as e:
            log.warning("variant stage failed: %s", e)

    if not ranked:
        return None
    log.info("recognize: %s", [(r["song"].get("title"), r["song"].get("artist"), round(r["score"], 2), r["solid"])
                               for r in ranked[:3]])
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


# ======================================================================================
# Sending audio
# ======================================================================================
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
    """Download step. On a cache hit returns the file_id right away.
    The temp folder lives in DL_DIR so a periodic cleanup can always remove leaked ones."""
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
        async with dl_sem():  # at most DL_CONCURRENCY downloads at the same time
            a = await asyncio.to_thread(download_audio, target, tmp, fb)
    except BaseException:
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
    """Identify an audio file and answer with the result + mp3.
    Used by Instagram photo posts and by videos that are too long / too big to send."""
    uid = msg.from_user.id
    trusted = bool(meta) and host_in(src, ("youtube.com", "youtu.be"))  # YouTube's own music metadata
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
            if not await asyncio.to_thread(use_quota, uid):
                return await msg.reply_text(tr(lang, "quota"))
            charged = True
            outs = await asyncio.to_thread(make_samples, audio_path, sid)
            if not outs:
                await asyncio.to_thread(refund_quota, uid)
                return await msg.reply_text(tr(lang, "no_music"))
            await asyncio.to_thread(add_sample, sid, outs[0], src)
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
    """Instagram photo / carousel post: no video to send, so we just identify its music."""
    got = await asyncio.to_thread(ig_post_audio, src, tmp)
    if not got:
        return await msg.reply_text(tr(lang, "no_music"))
    await identify_audio_and_reply(msg, src, got["path"], got.get("meta"), lang)


async def do_audio_only(msg, src: str, tmp: str, lang: str, meta=None):
    """Video too long / too big to send: download only its audio and identify that."""
    try:
        async with dl_sem():
            path, meta2 = await asyncio.to_thread(download_source_audio, src, tmp)
    except Exception as e:
        log.warning("audio-only download failed: %s", str(e)[:150])
        return await msg.reply_text(tr(lang, "clip_missing"))
    await identify_audio_and_reply(msg, src, path, meta or meta2, lang)


async def identify_clip(uid: int, sid: str, meta, src: str, samples_task):
    """Returns (song or None, quota_blocked). The quota is refunded when nothing was found.
    YouTube's own music metadata (official tracks) is trusted. Anything else (Instagram's
    'original audio' label is whatever the uploader picked) is only a weak hint that must be
    confirmed by listening to the real audio."""
    charged = False
    try:
        meta_song = None
        if meta:
            meta_song = {"title": meta["title"], "artist": meta["artist"],
                         "link": src if "youtu" in src else None, "engine": "meta#0",
                         "w": meta.get("w", META_W)}
            if host_in(src, ("youtube.com", "youtu.be")):
                meta["link"] = src
                with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
                return meta_song, False
        if not await asyncio.to_thread(use_quota, uid):
            if meta_song:  # no quota left: the hint is better than nothing
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
        if not path:  # too long / too big for Telegram: identify from the audio alone
            return await do_audio_only(msg, src, tmp, lang, meta)
        if not meta and "instagram.com" in src:  # yt-dlp often misses the music name: ask Instagram itself
            try:
                got = await asyncio.to_thread(ig_post_audio, src, tmp, False)
                meta = (got or {}).get("meta")
            except Exception:
                log.exception("instagram music name failed")
        sid = uuid.uuid4().hex[:10]
        await asyncio.to_thread(add_sample, sid, "", src)

        # background: cut samples -> recognize -> download audio (not awaited)
        samples_task = asyncio.create_task(asyncio.to_thread(make_samples, path, sid))
        prep_task = asyncio.create_task(prep_clip(uid, sid, meta, src, samples_task))
        PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False}
        try:
            # the clip goes out right away with the identify + search buttons
            sent = await upload_video(msg, path, footer(src), clip_keyboard(sid, lang))
            fid = _msg_video_id(sent)
            if fid:
                await asyncio.to_thread(clip_put, src, fid)
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
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))
    PREP.pop(sid, None)


# ======================================================================================
# Voice / audio / typed text from the user
# ======================================================================================
async def handle_media(msg, media, lang: str):
    uid = msg.from_user.id
    fid = media.file_unique_id
    sid = uuid.uuid4().hex[:10]
    song = await asyncio.to_thread(media_cache_get, fid)  # same file sent before -> same answer, no quota
    charged = False
    try:
        if song:
            await asyncio.to_thread(add_sample, sid, "")
            set_last(sid, {**song, "engine": "cache#0"})
        else:
            size = getattr(media, "file_size", 0) or 0
            if size > 20 * 1024 * 1024:  # Telegram bots cannot download bigger files
                return await msg.reply_text(tr(lang, "err"))
            if not await asyncio.to_thread(use_quota, uid):
                return await msg.reply_text(tr(lang, "quota"))
            charged = True
            raw = os.path.join(SAMPLES_DIR, sid + ".raw")
            await (await media.get_file()).download_to_drive(raw)
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
    """Typed lyrics (5+ words) -> find the song, send it with its mp3. False = nothing found."""
    words = re.findall(r"\w+", _fold(text))
    song = await asyncio.to_thread(find_song_by_words, words)
    if not song:
        return False
    audio_task = asyncio.create_task(prepare_song_audio(song))
    kb = Markup(result_keyboard("x", song, lang).inline_keyboard[:-1])  # no "wrong song": no audio sample
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
        # no "working..." message: the status shows under the bot name instead
        async with chat_action(ctx.bot, msg.chat_id, action):
            if media:
                await handle_media(msg, media, lang)
            elif text.startswith(MP3_PREFIX):
                await send_mp3(msg, text[len(MP3_PREFIX):].strip(), lang=lang)
            elif is_clip:
                await do_clip(msg, url)
            elif url:  # other links (Spotify, ...)
                target, _ = await asyncio.to_thread(resolve, text)
                await send_mp3(msg, target, lang=lang)
            else:  # lyrics (5+ words) -> the song; otherwise artist or song name -> list of tracks
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
        # no prepared result (bot restarted / expired): recognize now, below
    else:
        drop_entry(sid)  # "wrong song": the prepared result is wrong, throw it away
        media_cache_drop(sid)  # ...and a cached answer for that voice/audio file too

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
        if not await asyncio.to_thread(use_quota, uid):
            return await q.answer(tr(lang, "quota_short"), show_alert=True)
        await q.answer(tr(lang, "identifying"))
        try:
            song = await asyncio.to_thread(recognize, paths, tuple(tried))
        except Exception:
            await asyncio.to_thread(refund_quota, uid)
            raise
        if song:
            remember_alts(sid, song)
        else:
            await asyncio.to_thread(refund_quota, uid)  # nothing found: give the recognition back
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
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))


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


_VID_RE = re.compile(r"^[\w-]{6,20}$")


async def on_pick(q, ctx, data: str):
    lang = get_lang(q.from_user.id)
    vid = data.split(":", 1)[1]
    if not _VID_RE.match(vid):
        return await q.answer()
    if not rate_ok(q.from_user.id, limit=RATE_LIMIT * 2):
        return await q.answer(tr(lang, "slow"), show_alert=True)
    await q.answer(tr(lang, "downloading"))
    try:
        async with chat_action(ctx.bot, q.message.chat_id, ChatAction.RECORD_VOICE):
            await send_mp3(q.message, f"https://www.youtube.com/watch?v={vid}", lang=lang)
    except Exception:
        log.exception("pick failed")
        await q.message.reply_text(tr(lang, "err"))


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
    touch_user(update.effective_user)
    # only the language list is shown first; after a choice it turns into the welcome text
    await update.message.reply_text(LANG_PROMPT, reply_markup=lang_keyboard())


async def cmd_lang(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user)
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
    # Admin features live in ./admin and are optional: if that folder is missing or broken,
    # the bot still starts anyway.
    try:
        from admin import register as register_admin
        register_admin(app, sys.modules[__name__])
    except Exception:
        log.exception("admin module not loaded - the bot continues without it")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
