"""
Music recognition Telegram bot - v10 (clean)

Recognition (see recognition.py):
  1) own fingerprint DB (fp_engine): free, fast, knows remixes and special versions
  2) ACRCloud + Shazam vote together (several ACR accounts in priority order, Shazam with a circuit breaker);
     a song confirmed by two engines is learned by the own DB, so the next time it is answered for free
Last resort when nothing is confirmed: lyrics (faster-whisper, optional) and cleaned / tempo-shifted audio.

Sources: Instagram / YouTube links (clip + one tap to identify), voice / audio / video notes, streaming links
(Spotify, Apple Music...), song names (search), pieces of lyrics (find the song by its words).

Environment:
  BOT_TOKEN (required), BOT_USERNAME, ADMIN_IDS, DB_PATH, COOKIES_FILE, DAILY_LIMIT, RATE_LIMIT
  ACR_HOST / ACR_KEY / ACR_SECRET            first ACRCloud account (only used when the accounts table is empty)
  ACR_ACCOUNTS="host:key:secret,host:key:secret"   more accounts to seed (only used when the table is empty)
  more accounts can be added to the `accounts` table (type='acr') at any time: lowest id = highest priority
"""
import array
import asyncio
import datetime
import difflib
import glob
import hashlib
import html
import http.cookiejar
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from urllib.parse import quote, urlparse, parse_qs

import requests
import yt_dlp
from requests.adapters import HTTPAdapter
from telegram import (Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.constants import ChatAction
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          InlineQueryHandler, filters, ContextTypes)

import recognition
from recognition import title_key, artist_set, same_song, is_variant, fold, first_artist

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
    log.warning("fp_engine not loaded: the local database is off")


# ======================================================================================
# Config
# ======================================================================================
def _int(name: str, default) -> int:
    return int(os.environ.get(name, default))


BOT_TOKEN = os.environ["BOT_TOKEN"]
if "BOT_USERNAME" not in os.environ:
    log.warning("BOT_USERNAME is not set, using the built-in default")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "Whatsmuziccbot").lstrip("@")
ACR_HOST = os.environ.get("ACR_HOST")
ACR_KEY = os.environ.get("ACR_KEY")
ACR_SECRET = os.environ.get("ACR_SECRET")
ODESLI_KEY = os.environ.get("ODESLI_KEY")  # optional
DAILY_LIMIT = _int("DAILY_LIMIT", 20)
RATE_LIMIT = _int("RATE_LIMIT", 10)
DL_CONCURRENCY = _int("DL_CONCURRENCY", 4)
CLIP_MAX_SEC = _int("CLIP_MAX_SEC", 900)         # longest clip / song that is downloaded
MAX_SRC_SEC = _int("MAX_SRC_SEC", 3 * 3600)      # longest source that is scanned for recognition
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")
DL_DIR = os.path.join(SAMPLES_DIR, "dl")
COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_BYTES = 49 * 1024 * 1024          # Telegram upload limit for bots
AUDIO_MAX_BYTES = 48 * 1024 * 1024
ARIA2 = bool(shutil.which("aria2c")) and os.environ.get("ARIA2", "1") == "1"
os.makedirs(SAMPLES_DIR, exist_ok=True)
os.makedirs(DL_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
CLIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com")
AUDIO_HOSTS = ("youtube.com", "youtu.be", "soundcloud.com")
YT_HOSTS = ("youtube.com", "youtu.be")
MP3_PREFIX = "🎵 "
SONG_KEYS = ("title", "artist", "link", "yt", "dur")
PAGE = 8

esc = html.escape

PREP = {}    # sid -> {"task", "src", "created", "used", "busy", "song"}   (clips being prepared)
CANDS = {}   # sid -> {"alts": [...], "created": ts}                        (other answers for "wrong song")
SEARCH = {}  # sid -> {"items": [...], "created": ts}                        (search result pages)

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=48))
HTTP.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=8))

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
    "fa": "🇮🇷 زبان فارسی", "en": "🇬🇧 English", "uz": "🇺🇿 O'zbek", "ru": "🇷🇺 Русский",
    "es": "🇪🇸 Español", "ar": "🇸🇦 اللغة العربية", "tr": "🇹🇷 Türkçe", "hi": "🇮🇳 हिंदी",
    "vi": "🇻🇳 Tiếng Việt", "uk": "🇺🇦 Українська", "id": "🇮🇩 Bahasa Indonesia", "ms": "🇲🇾 Melayu",
    "ko": "🇰🇷 한국인", "pt": "🇧🇷 Português", "zh": "🇨🇳 中國人", "fr": "🇫🇷 Français",
    "de": "🇩🇪 Deutsch", "it": "🇮🇹 Italiano", "he": "🇮🇱 עברית", "nl": "🇳🇱 Nederlands",
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
        "err": "❌ دریافت رسانه از منبع ممکن نیست.",
        "clip_missing": "⚠️ کلیپ پیدا نشد یا بزرگ‌تر از ۵۰ مگابایته.",
        "quota": "⛔ سقف تشخیص امروزت پر شده، فردا دوباره امتحان کن.",
        "quota_short": "⛔ سقف تشخیص امروزت پر شده.",
        "not_found": "😕 آهنگ شناسایی نشد.\nبخش دیگری بفرست که موسیقی در آن واضح شنیده شود، بدون صحبت یا سروصدای زیاد.",
        "no_more": "😕 نتیجه‌ی دیگه‌ای پیدا نشد.",
        "expired": "نمونه منقضی شده، لینک یا فایل رو دوباره بفرست.",
        "no_result": "نتیجه‌ی دیگه‌ای نیست.",
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
        "err": "❌ Couldn't get the media from the source.",
        "clip_missing": "⚠️ Clip not found or larger than 50 MB.",
        "quota": "⛔ You've reached today's recognition limit. Try again tomorrow.",
        "quota_short": "⛔ You've reached today's recognition limit.",
        "not_found": "😕 Couldn't identify the song.\nSend another part where the music is clearly audible, without speech or too much noise.",
        "no_more": "😕 No other result found.",
        "expired": "This sample has expired. Please send the link or file again.",
        "no_result": "No other result.",
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
        "err": "❌ Bu dosya şu an kullanılamıyor. Başka bir bağlantı veya şarkı dene ya da birkaç dakika sonra tekrar dene.",
        "clip_missing": "⚠️ Klip bulunamadı veya 50 MB'dan büyük.",
        "quota": "⛔ Bugünkü tanıma hakkın doldu, yarın tekrar dene.",
        "quota_short": "⛔ Bugünkü tanıma hakkın doldu.",
        "not_found": "😕 Şarkıyı tanıyamadım.",
        "no_more": "😕 Başka sonuç bulunamadı.",
        "expired": "Örnek süresi doldu, bağlantıyı veya dosyayı tekrar gönder.",
        "no_result": "Başka sonuç yok.",
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
        "err": "❌ هذا الملف غير متاح حالياً. جرّب رابطاً أو أغنية أخرى، أو حاول مرة أخرى بعد قليل.",
        "clip_missing": "⚠️ لم يتم العثور على المقطع أو أنه أكبر من 50 ميغابايت.",
        "quota": "⛔ وصلت إلى حد التعرّف اليومي، حاول غداً.",
        "quota_short": "⛔ وصلت إلى حد التعرّف اليومي.",
        "not_found": "😕 لم أستطع التعرّف على الأغنية.",
        "no_more": "😕 لم أجد نتيجة أخرى.",
        "expired": "انتهت صلاحية العيّنة، أعد إرسال الرابط أو الملف.",
        "no_result": "لا توجد نتيجة أخرى.",
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
        "err": "❌ Этот файл сейчас недоступен. Попробуй другую ссылку или песню или повтори через несколько минут.",
        "clip_missing": "⚠️ Клип не найден или больше 50 МБ.",
        "quota": "⛔ Дневной лимит распознавания исчерпан, попробуй завтра.",
        "quota_short": "⛔ Дневной лимит распознавания исчерпан.",
        "not_found": "😕 Не удалось распознать песню.",
        "no_more": "😕 Других результатов не найдено.",
        "expired": "Образец устарел, отправь ссылку или файл заново.",
        "no_result": "Других результатов нет.",
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


def tr(lang: str, key: str, **kw) -> str:
    s = STR.get(lang, STR["en"]).get(key) or STR["en"][key]
    return s.format(**kw) if kw else s


def lang_keyboard():
    btns = [Btn(label, callback_data=f"lang:{code}") for code, label in LANGS.items()]
    return Markup([btns[i:i + 2] for i in range(0, len(btns), 2)])


# ======================================================================================
# Small helpers: chat status, rate limit, URLs
# ======================================================================================
@asynccontextmanager
async def chat_action(bot, chat_id, action=ChatAction.RECORD_VOICE):
    """Keeps the status at the top of the chat ("recording voice..." / "typing...") on while the block runs.
    This is the ONLY progress indicator: nothing is posted under the clip."""
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
    """One canonical form per clip / video, so the cache hits no matter how the link was written."""
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
    """Adds columns that older databases do not have yet."""
    with db() as c:
        for table, col in (("songs", "info"), ("samples", "src"), ("users", "lang")):
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        c.execute("DELETE FROM accounts WHERE type='audd'")  # AudD is not used anymore


def seed_accounts():
    """First start only: ACRCloud accounts from the environment. After that the table is the source of truth."""
    specs = []
    if all([ACR_HOST, ACR_KEY, ACR_SECRET]):
        specs.append((ACR_HOST, ACR_KEY, ACR_SECRET))
    for item in os.environ.get("ACR_ACCOUNTS", "").split(","):
        parts = item.strip().split(":")
        if len(parts) == 3 and all(parts):
            specs.append(tuple(parts))
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            return
        for host, key, secret in specs:
            c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", (host, key, secret))


def load_accounts() -> list:
    """Called by recognition.py (every 30 s at most): the enabled ACRCloud accounts."""
    with db() as c:
        rows = c.execute("SELECT id,host,key,secret FROM accounts WHERE type='acr' AND enabled=1 ORDER BY id")
        return [dict(r) for r in rows.fetchall()]


def get_setting(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_setting(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))


def daily_limit() -> int:
    return int(get_setting("daily_limit", DAILY_LIMIT))


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


def touch_user(u):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid, first_seen, lang) VALUES(?,?,?)",
                  (u.id, datetime.date.today().isoformat(), detect_lang(u.language_code)))


def _song_json(song: dict) -> str:
    return json.dumps({k: song.get(k) for k in SONG_KEYS}, ensure_ascii=False)


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
    """file_id and song are filled separately (the song is known later): NULLs never overwrite a value."""
    data = _song_json(song) if song else None
    with db() as c:
        c.execute("""INSERT INTO clips(url,file_id,song) VALUES(?,?,?)
                     ON CONFLICT(url) DO UPDATE SET
                       file_id=COALESCE(excluded.file_id, clips.file_id),
                       song=COALESCE(excluded.song, clips.song)""", (url, file_id, data))


def clip_delete(url):
    with db() as c:
        c.execute("DELETE FROM clips WHERE url=?", (url,))


def media_cache_get(uid: str):
    with db() as c:
        r = c.execute("SELECT song FROM media_songs WHERE uid=?", (uid,)).fetchone()
    return json.loads(r[0]) if r else None


def media_cache_put(uid: str, song: dict, sid: str):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO media_songs VALUES(?,?,?)", (uid, _song_json(song), sid))


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
    """The answer currently shown for this sample: "wrong song" rejects it."""
    val = f"{song.get('engine') or ''}||{title_key(song.get('title'))}"
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (val, sid))


def remember_alts(sid: str, song: dict):
    CANDS[sid] = {"alts": song.pop("alts", None) or [], "created": time.time()}


def save_meta(sid: str, song: dict):
    """The name found in the post (title / caption / music library): kept as a candidate for "wrong song"."""
    with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
        json.dump({k: song.get(k) for k in ("title", "artist", "link")}, f, ensure_ascii=False)


# ======================================================================================
# Background housekeeping
# ======================================================================================
def cleanup_samples():
    now = time.time()
    cutoff = int(now) - 86400
    with db() as c:
        for r in c.execute("SELECT id FROM samples WHERE created<?", (cutoff,)).fetchall():
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
    """Forgets a prepared clip and deletes the mp3 that was downloaded for it (if it was never sent)."""
    e = PREP.pop(sid, None)
    if not e:
        return
    t = e["task"]
    if t.done():
        _rm_audio_of(t)
    else:
        t.add_done_callback(_rm_audio_of)


def cleanup_memory():
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
            cleanup_memory()
        except Exception:
            log.exception("maintenance failed")
        await asyncio.sleep(600)


# ======================================================================================
# Recognition glue: own DB first (free), then ACRCloud + Shazam together (1 quota)
# ======================================================================================
class OutOfQuota(Exception):
    """The user's daily recognition limit is used up."""


def _learn_async(paths, song):
    """A song confirmed by TWO engines (ACR and Shazam agree) is added to the own DB in the background, so the
    next time it is answered at once and for free. One engine alone is never learned (a wrong name would poison
    the DB)."""
    if not fp_engine or not {"acr", "shazam"} <= set((song.get("engine") or "").split("+")) or not paths:
        return
    title, artist = song.get("title"), song.get("artist")
    path = song.get("piece_path") or paths[0]
    if not title or not artist:
        return

    def run():
        try:
            if fp_engine._thread_engine().learn(path, title, artist, source="api"):
                log.info("learned: %s - %s", artist, title)
        except Exception as e:  # learning must never break an answer
            log.warning("learn failed: %s", e)

    threading.Thread(target=run, daemon=True, name="learn").start()


async def identify(uid: int, paths, rejected=()):
    """Own DB first: no quota. ACRCloud + Shazam only when the DB does not know the song: costs 1 quota, refunded
    when nothing is found or something breaks. -> song dict or None. Raises OutOfQuota / NoAccounts."""
    rejected = set(rejected)
    song = await asyncio.to_thread(recognition.ask_local, paths, rejected)
    if song:
        return song
    if not await asyncio.to_thread(use_quota, uid):
        raise OutOfQuota()
    song = None
    try:
        song = await asyncio.to_thread(recognition.ask_engines, paths, rejected)
    finally:
        if not song:  # not found, no account answered, or cancelled: the user did not get anything
            await asyncio.to_thread(refund_quota, uid)
    if song:
        _learn_async(paths, song)
    return song


async def find_song(uid: int, paths, fallback=None, rejected=()):
    """identify() that never raises. `fallback` is a name already known from the post (used when recognition
    gives nothing). -> (song | None, why) with why in ok / quota / busy / none."""
    why = "none"
    try:
        song = await identify(uid, paths, rejected)
        if song:
            return song, "ok"
    except OutOfQuota:
        why = "quota"
    except recognition.NoAccounts:
        log.error("no recognition engine could answer")
        why = "busy"
    if fallback:
        return dict(fallback), "ok"
    return None, why


WHY_KEY = {"quota": "quota", "busy": "err", "none": "not_found"}


def meta_to_song(meta, src: str):
    if not meta:
        return None
    return {"title": meta["title"], "artist": meta["artist"], "link": src if host_in(src, YT_HOSTS) else None,
            "engine": "meta#0"}


def meta_is_trusted(meta, src: str) -> bool:
    """A YouTube title or a verified name (Instagram music library, a caption found in iTunes / Deezer)."""
    return bool(meta) and (host_in(src, YT_HOSTS) or bool(meta.get("verified")))


# ======================================================================================
# Streaming links -> a name (Odesli, or the page itself)
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
                url = requests.compat.urljoin(url, r.headers.get("Location") or "")
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
    parts = [x for x in u.path.split("/") if x]
    tid = (parse_qs(u.query).get("i") or [None])[0] or next((x for x in reversed(parts) if x.isdigit()), None)
    if not tid:
        return ""
    cc = parts[0] if parts and re.fullmatch(r"[a-z]{2}", parts[0]) else "us"
    r = HTTP.get("https://itunes.apple.com/lookup", params={"id": tid, "country": cc}, timeout=timeout)
    if not r.ok:
        return ""
    res = (r.json().get("results") or [None])[0] or {}
    return f"{res.get('artistName', '')} - {res.get('trackName') or res.get('collectionName') or ''}".strip(" -")


def link_meta_name(link: str, timeout: int = 8) -> str:
    """'Artist - title' of a streaming link without Odesli (iTunes lookup / page tags). '' if unknown."""
    hit = _META.get(link)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    if not host_in(link, META_HOSTS):
        return ""
    path = urlparse(link).path
    if host_in(link, ("spotify.com",)) and re.match(r"/(?:intl-[a-z]+/)?(?:artist|playlist|user|show|episode)/", path):
        return ""
    name = _apple_name(link, timeout) if host_in(link, ("music.apple.com", "itunes.apple.com")) else ""
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
    """Odesli first; if it finds nothing, read the page itself. -> (yt_url | None, name | None)"""
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


def resolve(text: str):
    """Typed text / link -> (download target, name). Raises ValueError for an unsupported link."""
    url = first_url(text)
    if not url:
        return text, text
    if host_in(url, CLIP_HOSTS) or host_in(url, ("soundcloud.com",)):
        return url, None
    yt, name = link_to_youtube(url, timeout=20)
    if not yt and not name:
        raise ValueError("unsupported link")
    return (yt or f"ytsearch1:{name}"), name


# ======================================================================================
# YouTube: search, picking the ORIGINAL recording, downloading
# ======================================================================================
_YT_CACHE = {}
_JUNK_YT_RE = re.compile(r"full\s+album|playlist|compilation|non-?stop|jukebox|\b\d+\s*hours?\b|"
                         r"1\s*hour|reaction|tutorial|how to play|instrumental|acoustic", re.I)


def ydl_base():
    o = {"quiet": True, "noplaylist": True, "concurrent_fragment_downloads": 8,
         "http_chunk_size": 10 * 1024 * 1024, "socket_timeout": 20, "retries": 3, "fragment_retries": 3}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def yt_search(query: str, n: int = 10):
    key = (query.strip().lower(), n)
    hit = _YT_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    with yt_dlp.YoutubeDL({**ydl_base(), "extract_flat": True, "skip_download": True}) as ydl:
        items = ydl.extract_info(f"ytsearch{n}:{query}", download=False).get("entries") or []
    if len(_YT_CACHE) > 300:
        _YT_CACHE.clear()
    _YT_CACHE[key] = (time.time(), items)
    return items


def clean_title(t: str) -> str:
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t or "").strip()


def _ck(s: str) -> str:
    return re.sub(r"[\W_]+", "", fold(s or ""))


def yt_match_score(item: dict, song: dict) -> float:
    """Higher = more likely the original studio track of this song."""
    t = item.get("title") or ""
    ch = item.get("channel") or item.get("uploader") or ""
    tl, chl = t.lower(), ch.lower()
    tc, chc = _ck(t), _ck(ch)
    s = 0.0
    want = _ck(clean_title(song.get("title")) or song.get("title"))
    s += 4 if want and want in tc else -3
    arts = artist_set(song.get("artist"))
    if arts:
        hit = sum(1 for a in arts if a in tc or a in chc)
        s += (2 + 0.5 * min(hit - 1, 2)) if hit else -1.5
    if is_variant(item) and not is_variant(song):
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
    elif v and (v > 600 or v < 60):
        s -= 3
    return s


def rank_youtube(song: dict, n: int = 8) -> list:
    """[(score, video_id)], best first, for the original recording of this song."""
    q = f"{(song.get('artist') or '')[:80]} - {song.get('title') or ''}".strip(" -")
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
    if not out or song.get("dur"):  # without a known duration a direct id cannot be verified: skip the search
        for score, vid in rank_youtube(song)[:3]:
            if score >= 2:
                add(f"https://www.youtube.com/watch?v={vid}")
    add(f"ytsearch1:{song.get('artist', '')} - {song.get('title', '')}")
    return out[:4]


def _len_filter(max_sec: int):
    return yt_dlp.utils.match_filter_func(f"duration <=? {int(max_sec)} & !is_live")


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
    """Max CLIP_MAX_SEC and 48 MB: anything longer can never be sent to Telegram anyway."""
    target = normalize_target(target)
    plain = {**ydl_base(), "format": "bestaudio[ext=m4a][abr<=130]/bestaudio[ext=m4a]/bestaudio/best",
             "outtmpl": f"{outdir}/%(id)s.%(ext)s", "max_filesize": AUDIO_MAX_BYTES,
             "match_filter": _len_filter(CLIP_MAX_SEC)}
    fast = dict(plain)
    if ARIA2:
        fast["external_downloader"] = {"default": "aria2c"}
        fast["external_downloader_args"] = {"aria2c": ["-x16", "-s16", "-k1M", "--file-allocation=none"]}

    def run(t, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(t, download=True)
            if not info:
                raise RuntimeError("rejected (too long / too big)")
            if "entries" in info:
                if not info["entries"]:
                    raise RuntimeError("track not found")
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
    dur = info.get("duration")
    return {"path": path, "title": info.get("title", "music"),
            "vid": info.get("id") if info.get("extractor_key") == "Youtube" else None,
            "url": info.get("webpage_url"), "duration": int(dur) if dur else None}


# ======================================================================================
# Clips and the names that come with them
# ======================================================================================
_CAP_SEP = re.compile(r"\s+[-–—]\s+")


def _sim(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _caption_pair(info: dict):
    """First line of the caption written as 'A - B' -> (a, b) or None."""
    text = info.get("description") or info.get("title") or ""
    for raw in text.splitlines():
        line = re.sub(r"https?://\S+", " ", raw)
        line = re.sub(r"[#@]\S+", " ", line)
        line = re.sub(r"\s+", " ", line).strip()
        line = re.sub(r"^[\W_]+", "", line)
        line = re.sub(r"[\s•·|.!~*_]+$", "", line)
        if not line:
            continue
        if len(line) > 120:
            return None
        parts = _CAP_SEP.split(line)
        if len(parts) != 2:
            return None
        a, b = parts[0].strip(), parts[1].strip()
        return (a, b) if 2 <= len(a) <= 70 and 2 <= len(b) <= 70 else None
    return None


def _pair_score(claim_title: str, claim_artist: str, real_title: str, real_artist: str) -> float:
    ts = _sim(title_key(claim_title), title_key(real_title))
    ca, ra = _ck(claim_artist), _ck(real_artist)
    if not ca or not ra:
        return 0.0
    art = 1.0 if len(ca) >= 4 and (ca in ra or ra in ca) else _sim(ca, ra)
    return min(ts, art)


def _best_catalog_hit(results, a: str, b: str):
    """results: [(title, artist)]. Both orders of 'a - b' are tried: the catalog tells which one is right."""
    best = None
    for t, ar in results:
        if not t or not ar:
            continue
        for ct, ca in ((a, b), (b, a)):
            s = _pair_score(ct, ca, t, ar)
            if s >= 0.82 and (not best or s > best[0]):
                best = (s, t, ar)
    return {"title": best[1], "artist": best[2]} if best else None


def _catalog_find(url: str, params: dict, parse, a: str, b: str):
    try:
        r = HTTP.get(url, params=params, timeout=6)
        data = parse(r.json()) if r.ok else []
    except Exception as e:
        log.warning("catalog search failed: %s", e)
        return None
    return _best_catalog_hit(data, a, b)


def caption_meta(info: dict):
    """The song named in the caption AND found in a real catalog -> verified name, else None (blocking)."""
    pair = _caption_pair(info)
    if not pair:
        return None
    a, b = pair
    hit = _catalog_find("https://itunes.apple.com/search",
                        {"term": f"{a} {b}", "media": "music", "entity": "song", "limit": 8},
                        lambda j: [(x.get("trackName"), x.get("artistName")) for x in j.get("results") or []], a, b) \
        or _catalog_find("https://api.deezer.com/search", {"q": f"{a} {b}", "limit": 8},
                         lambda j: [(x.get("title"), (x.get("artist") or {}).get("name"))
                                    for x in j.get("data") or []], a, b)
    if not hit:
        log.info("caption '%s' / '%s' is not a known track, ignored", a, b)
        return None
    log.info("caption song: %s - %s", hit["artist"], hit["title"])
    return {"title": hit["title"], "artist": hit["artist"], "verified": True}


def _meta_from_info(info):
    if not info:
        return None
    artist = info.get("artist") or ", ".join(info.get("artists") or [])
    track = info.get("track")
    if artist and track:
        return {"artist": artist.strip(), "title": track}
    if str(info.get("extractor_key") or "").lower().startswith("instagram"):
        try:
            return caption_meta(info)
        except Exception as e:
            log.warning("caption meta failed: %s", e)
    return None


def download_clip(url: str, outdir: str):
    """-> (path, title, meta). path is None when the clip is too long / too big for Telegram."""
    if not host_in(url, CLIP_HOSTS):
        raise ValueError("unsupported link")
    opts = {**ydl_base(),
            "format": "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/b[height<=480]/b",
            "outtmpl": f"{outdir}/clip.%(ext)s", "merge_output_format": "mp4",
            "max_filesize": MAX_BYTES, "match_filter": _len_filter(CLIP_MAX_SEC)}
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
    """Low-quality audio of a long source, for recognition only. -> (path, meta)"""
    if not host_in(url, CLIP_HOSTS):
        raise ValueError("unsupported link")
    opts = {**ydl_base(), "format": "bestaudio[abr<=70]/worstaudio/bestaudio/worst",
            "outtmpl": f"{outdir}/src.%(ext)s", "max_filesize": 250 * 1024 * 1024,
            "match_filter": _len_filter(MAX_SRC_SEC)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info and "entries" in info:
            info = (info["entries"] or [None])[0]
    files = [f for f in glob.glob(f"{outdir}/src.*") if not f.endswith((".part", ".ytdl", ".aria2"))]
    if not info or not files:
        raise RuntimeError("source audio was not downloaded (too long / too big / unavailable)")
    return files[0], _meta_from_info(info)


# --- Instagram photo / carousel posts (needs a logged-in instagram.com cookie in cookies.txt) ---------
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
    """-> {"path": audio file or None, "meta": {...} or None} or None."""
    m = _IG_CODE_RE.search(url)
    if not m or not os.path.exists(COOKIES):
        return None
    try:
        jar = http.cookiejar.MozillaCookieJar(COOKIES)
        jar.load(ignore_discard=True, ignore_expires=True)
        if not any(c.name == "sessionid" and "instagram" in c.domain for c in jar):
            log.warning("instagram: cookies.txt has no instagram.com sessionid cookie (login cookie needed)")
            return None
        s = requests.Session()
        s.cookies = jar
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "X-IG-App-ID": IG_APP_ID, "X-Requested-With": "XMLHttpRequest", "Referer": url})
        csrf = next((c.value for c in jar if c.name == "csrftoken" and "instagram" in c.domain), "")
        mid = ig_media_id(m.group(1))
        data = None
        for u, hdr in ((f"https://www.instagram.com/api/v1/media/{mid}/info/", {"X-CSRFToken": csrf}),
                       (f"https://i.instagram.com/api/v1/media/{mid}/info/", {"User-Agent": IG_MOBILE_UA})):
            r = s.get(u, headers=hdr, timeout=20)
            try:
                data = r.json()
                break
            except ValueError:
                log.warning("instagram info is not JSON: HTTP %s | %s", r.status_code, r.text[:80].replace("\n", " "))
        items = (data or {}).get("items") or []
        if not items:
            log.warning("instagram info has no items: %s", str(data)[:120])
            return None
        it = items[0]
        cm = it.get("clips_metadata") or {}
        asset = (((it.get("music_metadata") or {}).get("music_info") or {}).get("music_asset_info")
                 or (cm.get("music_info") or {}).get("music_asset_info") or {})
        meta = None
        if asset.get("title") and asset.get("display_artist"):  # Instagram's own music library: trusted
            meta = {"title": asset["title"], "artist": asset["display_artist"], "verified": True}
        elif not asset:
            log.info("instagram: no music-library track (original sound or no music)")
        if not want_audio:
            return {"path": None, "meta": meta}
        audio_url = (asset.get("progressive_download_url") or asset.get("fast_start_progressive_download_url")
                     or (cm.get("original_sound_info") or {}).get("progressive_download_url"))
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
# Audio pieces (ffmpeg). The loudest piece comes FIRST: that is what the own DB is asked with
# ======================================================================================
def probe_duration(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def cut_sample(src: str, out: str, start: float, dur: int) -> bool:
    """RAW piece (no filters: they change the audio and hurt fingerprint matching)."""
    cmd = ["ffmpeg", "-y", "-ss", str(max(0, start)), "-i", src, "-t", str(dur),
           "-vn", "-ac", "1", "-ar", "44100", "-b:a", "128k", out]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        return True
    except Exception:
        return False


def _scan_energy(src: str, maxsec: int):
    """Loudness (mean square) of every second, from a 2 kHz mono decode."""
    sr = 2000
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(int(maxsec)), "-i", src, "-vn", "-ac", "1", "-ar", str(sr),
         "-f", "s16le", "-"], capture_output=True, timeout=60 + int(maxsec) // 30).stdout
    if np is not None:
        pcm = np.frombuffer(out[:len(out) // 2 * 2], dtype=np.int16).astype(np.float32)
        secs = len(pcm) // sr
        return (pcm[:secs * sr].reshape(secs, sr) ** 2).mean(axis=1) if secs else []
    pcm = array.array("h")
    pcm.frombytes(out[:len(out) // 2 * 2])
    secs = len(pcm) // sr
    return [sum(x * x for x in pcm[i * sr:(i + 1) * sr]) / sr for i in range(secs)]


def loud_offsets(src: str, win: int, n: int = 2, maxsec: int = 600) -> list:
    """Start times of the n loudest non-overlapping windows (that is the music, not silence or talking)."""
    try:
        energy = _scan_energy(src, maxsec)
        secs = len(energy)
        if secs <= win + 2:
            return []
        if np is not None:
            pref = np.concatenate(([0.0], np.cumsum(energy, dtype=np.float64)))
            sums = pref[win:] - pref[:len(pref) - win]
            ranked = [int(i) for i in np.argsort(-sums) if i <= secs - win]
        else:
            pref = [0.0]
            for v in energy:
                pref.append(pref[-1] + v)
            ranked = [i for _, i in sorted(((pref[i + win] - pref[i], i) for i in range(secs - win + 1)),
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
    """Order matters: loudest, middle, 2nd loudest, then evenly spread. The own DB uses the first three."""
    if not d or d <= win + 3:
        return [0]
    n = 4 if d <= 60 else 6 if d <= 300 else 8
    loud = loud_offsets(src, win, 2, int(min(d, MAX_SRC_SEC)))
    even = [d * (k + 0.5) / n - win / 2 for k in range(n)]
    cand = loud[:1] + [even[n // 2]] + loud[1:2] + [e for i, e in enumerate(even) if i != n // 2]
    offs = []
    for o in cand:
        o = round(max(0, min(o, d - win)))
        if all(abs(o - x) >= 5 for x in offs):
            offs.append(o)
    return offs[:n]


def make_samples(src: str, sid: str) -> list:
    """Cuts the pieces (in parallel) -> list of file paths, in pick_offsets order."""
    win = 15
    offs = pick_offsets(src, probe_duration(src), win)
    outs = [os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3") for i in range(len(offs))]
    threads, ok = [], [False] * len(offs)

    def job(i):
        ok[i] = cut_sample(src, outs[i], offs[i], win)

    for i in range(len(offs)):
        t = threading.Thread(target=job, args=(i,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return [o for o, good in zip(outs, ok) if good]


# ======================================================================================
# Search results
# ======================================================================================
def search_tracks(query: str, n: int = 30):
    out, seen = [], set()
    for it in yt_search(query, n):
        vid, title, dur = it.get("id"), it.get("title"), it.get("duration")
        if not vid or not title or (dur and not 60 <= dur <= 720):
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


# ======================================================================================
# Lyrics
# ======================================================================================
def _lrclib_lyrics(artist: str, title: str):
    t = clean_title(title) or title
    a = first_artist(artist)
    want = {"title": title, "artist": artist}
    tries = ([{"track_name": t, "artist_name": a}] if a else []) + [{"q": f"{a} {t}".strip()}]
    for params in tries:
        try:
            r = HTTP.get("https://lrclib.net/api/search", params=params, timeout=10)
            items = r.json() if r.ok else []
        except Exception as e:
            log.warning("lrclib failed: %s", e)
            continue
        for it in items:
            got = {"title": it.get("trackName") or "", "artist": it.get("artistName") or ""}
            if it.get("plainLyrics") and same_song(got, want):
                return it["plainLyrics"]
    return None


def _ovh_lyrics(artist: str, title: str):
    a, t = first_artist(artist), clean_title(title) or title
    if not a:
        return None
    try:
        r = HTTP.get(f"https://api.lyrics.ovh/v1/{quote(a, safe='')}/{quote(t, safe='')}", timeout=10)
        return r.json().get("lyrics") if r.ok else None
    except Exception as e:
        log.warning("lyrics.ovh failed: %s", e)
        return None


def get_lyrics(artist: str, title: str):
    key = f"{artist}|{title}".lower()
    with db() as c:
        r = c.execute("SELECT text FROM lyrics WHERE key=?", (key,)).fetchone()
    if r:
        return r[0]
    text = _lrclib_lyrics(artist, title) or _ovh_lyrics(artist, title)
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


def lyric_overlap(words, lyrics: str) -> float:
    ws = {fold(w) for w in words if len(w) > 2}
    if not ws:
        return 0.0
    return len(ws & set(re.findall(r"\w+", fold(lyrics)))) / len(ws)


def _parse_yt_title(title: str, channel: str):
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")
    t = re.sub(r"(?i)\b(official|lyrics?|lyric video|audio|music video|video|hd|4k)\b|متن( آهنگ)?|با متن", " ", t)
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", t) if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return re.sub(r"(?i)\s*-\s*topic$", "", channel or "").strip(), (parts[0] if parts else "")


def find_song_by_words(words):
    """A few words of the lyrics -> the song: YouTube search, then the lyrics of each result must contain the words."""
    slices = [words[:12]] + ([words[10:22]] if len(words) >= 20 else [])
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


# ======================================================================================
# Captions and keyboards
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


def result_keyboard(sid: str, song: dict, lang: str = "fa", wrong: bool = True):
    q = quote(f"{song['artist']} - {song['title']}")
    link = song.get("link") or ""
    sp = link if link.startswith("https://open.spotify.com") else f"https://open.spotify.com/search/{q}"
    rows = [
        [Btn("Google", url=f"https://www.google.com/search?q={q}"),
         Btn("YouTube Music", url=f"https://music.youtube.com/search?q={q}"),
         Btn("Spotify", url=sp)],
        [Btn(f"🔍 {tr(lang, 'btn_artist')}", switch_inline_query_current_chat=first_artist(song["artist"]))],
    ]
    if wrong:
        rows.append([Btn(f"❌ {tr(lang, 'btn_wrong')} ❌", callback_data=f"bad:{sid}")])
    return Markup(rows)


def audio_keyboard(tid: str, song: dict, lang: str = "fa"):
    q = f"{song.get('artist', '')} {song.get('title', '')}".strip()[:200]
    return Markup([[Btn(f"{tr(lang, 'btn_lyrics')} 🔤", callback_data=f"lyr:{tid}"),
                    Btn("🔍", switch_inline_query_current_chat=q)]])


# ======================================================================================
# Sending audio
# ======================================================================================
def song_keys(song: dict) -> list:
    tk = title_key(song.get("title"))
    arts = sorted(artist_set(song.get("artist")))[:4] or [""]
    return [f"s2:{tk}|{a}" for a in arts] + ["s:" + f"{song.get('artist', '')}|{song.get('title', '')}".lower()]


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
    """A cached Telegram file_id, or a fresh download (in its own temp folder)."""
    keys = [target] + (song_keys(song) if song else [])
    for k in keys:
        hit = await asyncio.to_thread(cache_get, k)
        if hit:
            return {"file_id": hit["file_id"], "title": hit["title"], "info": hit["info"]}
    tmp = tempfile.mkdtemp(dir=DL_DIR)
    fb = f"{song['artist']} - {song['title']}" if song else (target[10:] if target.startswith("ytsearch1:") else None)
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
    """Downloads the ORIGINAL recording. If the downloaded length is clearly different from the real length of
    the song, the next candidate is tried."""
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
                                     caption=audio_caption(info), parse_mode="HTML", reply_markup=kb)
    try:
        if os.path.getsize(a["path"]) > MAX_BYTES:
            raise RuntimeError("audio file is larger than Telegram's upload limit")
        with open(a["path"], "rb") as f:
            sent = await msg.reply_audio(f, title=title, performer=performer, duration=a.get("duration"),
                                         caption=audio_caption(info), parse_mode="HTML", reply_markup=kb,
                                         read_timeout=300, write_timeout=300)
    finally:
        shutil.rmtree(a["dir"], ignore_errors=True)
    if sent.audio:
        for k in a["keys"]:
            await asyncio.to_thread(cache_put, k, sent.audio.file_id, title, info)


async def send_audio_task(msg, task, song, lang: str):
    """Waits for a prepare_song_audio() task and sends the mp3. Failures are reported to the user."""
    try:
        await deliver_audio(msg, await task, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await msg.reply_text(tr(lang, "err"))


async def send_result(msg, song: dict, lang: str, sid: str, src=None, wrong: bool = True):
    """Result message first (the song name appears at once), the mp3 follows when it is downloaded.
    The download starts BEFORE the message is sent, so both happen at the same time."""
    task = asyncio.create_task(prepare_song_audio(song))
    try:
        await msg.reply_text(result_caption(song, src), parse_mode="HTML",
                             reply_markup=result_keyboard(sid, song, lang, wrong), **NO_PREVIEW)
    except BaseException:
        task.cancel()
        raise
    await send_audio_task(msg, task, song, lang)


async def send_mp3(msg, target: str, title=None, performer=None, lang: str = "fa"):
    song = {"title": title, "artist": performer or ""} if title else None
    await deliver_audio(msg, await fetch_audio(normalize_target(target), song), song, lang)


# ======================================================================================
# Clips and links
# ======================================================================================
async def upload_video(msg, path: str, caption: str, kb=None):
    with open(path, "rb") as f:
        return await msg.reply_video(f, caption=caption, parse_mode="HTML", reply_markup=kb,
                                     supports_streaming=True, read_timeout=300, write_timeout=300)


def _video_file_id(sent):
    for attr in ("video", "animation", "document"):
        obj = getattr(sent, attr, None)
        if obj:
            return obj.file_id
    return None


async def identify_audio_and_reply(msg, src: str, audio_path: str, meta, lang: str):
    """Links that are not sent as a clip (too big / blocked): the audio is recognized, the result is a message."""
    uid = msg.from_user.id
    sid = uuid.uuid4().hex[:10]
    await asyncio.to_thread(add_sample, sid, "", src)
    meta_song = meta_to_song(meta, src)
    if meta_is_trusted(meta, src):
        song = meta_song
    else:
        outs = await asyncio.to_thread(make_samples, audio_path, sid)  # audio_path dies with the caller's folder
        if not outs:
            return await msg.reply_text(tr(lang, "no_music"))
        song, why = await find_song(uid, outs, fallback=meta_song)
        if not song:
            return await msg.reply_text(tr(lang, WHY_KEY[why]))
    if meta_song:
        save_meta(sid, meta_song)
    remember_alts(sid, song)
    set_last(sid, song)
    await asyncio.to_thread(clip_put, src, None, song)
    await send_result(msg, song, lang, sid, src)


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


async def prep_clip(uid: int, sid: str, meta, src: str, samples_task) -> dict:
    """Background job for a clip: find the song, then download its mp3. The song name is published in
    PREP[sid]["song"] as soon as it is known, so the button does not wait for the mp3."""
    res = {"song": None, "audio": None, "blocked": False}
    try:
        meta_song = meta_to_song(meta, src)
        if meta_song:
            save_meta(sid, meta_song)
        if meta_is_trusted(meta, src):
            song, why = meta_song, "ok"  # no recognition, no quota; the pieces are still cut for "wrong song"
        else:
            outs = await samples_task
            song, why = await find_song(uid, outs, fallback=meta_song) if outs else (meta_song, "none")
        res["song"], res["blocked"] = song, why == "quota"
        if song and sid in PREP:
            PREP[sid]["song"] = song
        if song:
            remember_alts(sid, song)
            set_last(sid, song)
            await asyncio.to_thread(clip_put, src, None, song)
            try:
                res["audio"] = await prepare_song_audio(song)
            except Exception:
                log.exception("prepare audio failed")
    except Exception:
        log.exception("prep_clip failed")
    return res


async def clip_from_cache(msg, src: str, hit, lang: str):
    song = json.loads(hit["song"])
    sid = uuid.uuid4().hex[:10]
    await asyncio.to_thread(add_sample, sid, "", src)
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
                path, _title, meta = await asyncio.to_thread(download_clip, src, tmp)
        except Exception as e:
            if "instagram.com" in src:
                log.warning("instagram clip download failed (%s): trying it as a photo post", str(e)[:100])
                return await do_ig_post(msg, src, tmp, lang)
            log.warning("clip download failed (%s): trying audio only", str(e)[:100])
            return await do_audio_only(msg, src, tmp, lang)
        if not path:
            return await do_audio_only(msg, src, tmp, lang, meta)
        if not meta and "instagram.com" in src:  # the music name of a reel from Instagram's own data
            try:
                got = await asyncio.to_thread(ig_post_audio, src, tmp, False)
                meta = (got or {}).get("meta")
            except Exception:
                log.exception("instagram music name failed")
        sid = uuid.uuid4().hex[:10]
        await asyncio.to_thread(add_sample, sid, "", src)

        # background: cut the pieces, find the song, download the mp3. The clip is sent meanwhile
        samples_task = asyncio.create_task(asyncio.to_thread(make_samples, path, sid))
        prep_task = asyncio.create_task(prep_clip(uid, sid, meta, src, samples_task))
        PREP[sid] = {"task": prep_task, "src": src, "created": time.time(), "used": False,
                     "busy": False, "song": None}
        try:
            sent = await upload_video(msg, path, footer(src), clip_keyboard(sid, lang))
            fid = _video_file_id(sent)
            if fid:
                await asyncio.to_thread(clip_put, src, fid)
        except Exception:
            drop_entry(sid)
            raise
        finally:
            await asyncio.gather(samples_task, return_exceptions=True)  # the clip file lives in tmp


async def show_prepared(q, sid: str, entry: dict, lang: str):
    """The user tapped 'identify'. The only progress indicator is the status at the top of the chat; the result
    appears as soon as the name is known."""
    if entry.get("used") or entry.get("busy"):
        return await q.answer()
    entry["busy"] = True
    await q.answer()
    task = entry["task"]
    t0 = time.time()
    async with chat_action(q.get_bot(), q.message.chat_id):
        while not (entry.get("song") or task.done()) and time.time() - t0 < 240:
            await asyncio.sleep(0.3)
    res = task.result() if task.done() and not task.cancelled() else {}
    song = entry.get("song") or res.get("song")
    if not song:
        drop_entry(sid)  # the next tap recognizes again
        return await q.message.reply_text(tr(lang, "quota_short" if res.get("blocked") else "not_found"))
    if entry.get("used"):
        return
    entry["used"] = True
    try:
        await q.edit_message_caption(caption=result_caption(song, entry["src"]), parse_mode="HTML",
                                     reply_markup=result_keyboard(sid, song, lang))
    except Exception:
        log.warning("edit caption failed", exc_info=True)
    try:
        async with chat_action(q.get_bot(), q.message.chat_id):  # the status stays on while the mp3 is prepared
            r2 = await asyncio.wait_for(asyncio.shield(task), timeout=240)
            a = (r2 or {}).get("audio") or await prepare_song_audio(song)
        await deliver_audio(q.message, a, song, lang)
    except Exception:
        log.exception("mp3 failed")
        await q.message.reply_text(tr(lang, "err"))
    PREP.pop(sid, None)


# ======================================================================================
# Voice / audio / typed text
# ======================================================================================
async def handle_media(msg, media, lang: str):
    """Voice / audio / video note. No status message: handle() keeps the top-of-chat status on."""
    uid = msg.from_user.id
    fid = media.file_unique_id
    sid = uuid.uuid4().hex[:10]
    song = await asyncio.to_thread(media_cache_get, fid)
    if song:
        await asyncio.to_thread(add_sample, sid, "")
        set_last(sid, {**song, "engine": "cache#0"})
    else:
        if (getattr(media, "file_size", 0) or 0) > 20 * 1024 * 1024:
            return await msg.reply_text(tr(lang, "err"))
        raw = os.path.join(SAMPLES_DIR, sid + ".raw")
        await (await media.get_file()).download_to_drive(raw)
        try:
            outs = await asyncio.to_thread(make_samples, raw, sid)
            if not outs:  # ffmpeg could not cut it: use the file as it is
                fallback = os.path.join(SAMPLES_DIR, f"{sid}_0.ogg")
                shutil.copyfile(raw, fallback)
                outs = [fallback]
        finally:
            try:
                os.remove(raw)
            except OSError:
                pass
        await asyncio.to_thread(add_sample, sid, outs[0])
        song, why = await find_song(uid, outs)
        if not song:
            return await msg.reply_text(tr(lang, WHY_KEY[why]))
        remember_alts(sid, song)
        set_last(sid, song)
    await asyncio.to_thread(media_cache_put, fid, song, sid)
    await send_result(msg, song, lang, sid)


async def try_lyrics_search(msg, text: str, lang: str) -> bool:
    words = re.findall(r"\w+", fold(text))
    song = await asyncio.to_thread(find_song_by_words, words)
    if not song:
        return False
    await send_result(msg, song, lang, "x", wrong=False)
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
    if not media and not text:
        return
    url = first_url(text)
    is_clip = bool(url and host_in(url, CLIP_HOSTS) and host_of(url) != "music.youtube.com")
    action = ChatAction.UPLOAD_VIDEO if is_clip else ChatAction.RECORD_VOICE
    try:
        # the status at the top of the chat stays on for the WHOLE request until the answer is sent
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
            elif len(re.findall(r"\w+", text)) >= 5 and await try_lyrics_search(msg, text, lang):
                return
            else:
                await send_results(msg, text, lang)
    except Exception:
        log.exception("handle failed")
        await msg.reply_text(tr(lang, "err"))


# ======================================================================================
# Buttons
# ======================================================================================
async def reidentify(q, sid: str, wrong: bool):
    """'wrong song' (wrong=True) or 'identify' for a sample that is no longer in memory (wrong=False).
    The wrong answer is rejected, then: other answers already known -> the name from the post -> ask again."""
    uid = q.from_user.id
    lang = await asyncio.to_thread(get_lang, uid)
    if wrong:
        drop_entry(sid)
        await asyncio.to_thread(media_cache_drop, sid)
    with db() as c:
        row = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    if row and wrong and row["src"]:
        await asyncio.to_thread(clip_delete, row["src"])
    paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, f"{sid}_*")))
    mpath = os.path.join(SAMPLES_DIR, f"{sid}.json")
    if not row or not (paths or os.path.exists(mpath) or CANDS.get(sid)):
        return await q.answer(tr(lang, "expired"), show_alert=True)

    rejected = {t[2:] for t in (row["tried"] or "").split(",") if t.startswith("k:")}
    if wrong and row["last"]:
        key = row["last"].partition("||")[2]
        if key and key not in rejected:
            rejected.add(key)
            with db() as c:
                c.execute("UPDATE samples SET tried=? WHERE id=?", (",".join("k:" + k for k in rejected), sid))

    song = None
    if wrong:
        alts = (CANDS.get(sid) or {}).get("alts") or []
        while alts and not song:
            cand = alts.pop(0)
            if title_key(cand.get("title")) not in rejected:
                song = cand
    if not song and os.path.exists(mpath):
        try:
            with open(mpath, encoding="utf-8") as f:
                m = json.load(f)
            if title_key(m["title"]) not in rejected:
                song = {"title": m["title"], "artist": m["artist"], "link": m.get("link"), "engine": "meta#0"}
        except Exception:
            song = None

    if song:
        await q.answer()
    else:
        if not paths:
            return await q.answer(tr(lang, "no_result"), show_alert=True)
        await q.answer()  # no text: the status at the top of the chat shows the progress
        async with chat_action(q.get_bot(), q.message.chat_id):
            song, why = await find_song(uid, paths, rejected=rejected)
        if not song:
            key = {"quota": "quota_short", "busy": "err", "none": "no_more" if rejected else "not_found"}[why]
            return await q.message.reply_text(tr(lang, key))
        remember_alts(sid, song)

    set_last(sid, song)
    src = row["src"]
    cap, kb = result_caption(song, src), result_keyboard(sid, song, lang)
    task = asyncio.create_task(prepare_song_audio(song))
    try:
        if q.message.text:
            await q.edit_message_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
        else:
            await q.edit_message_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await q.message.reply_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
    if src:
        video = getattr(q.message, "video", None)
        await asyncio.to_thread(clip_put, src, video.file_id if video else None, song)
    async with chat_action(q.get_bot(), q.message.chat_id):
        await send_audio_task(q.message, task, song, lang)


async def on_rec(q, data: str):
    action, sid = data.split(":", 1)
    if action == "rec":
        entry = PREP.get(sid)
        if entry:
            return await show_prepared(q, sid, entry, await asyncio.to_thread(get_lang, q.from_user.id))
    await reidentify(q, sid, wrong=(action == "bad"))


async def on_lyrics(q, data: str):
    lang = await asyncio.to_thread(get_lang, q.from_user.id)
    tid = data.split(":", 1)[1]
    with db() as c:
        row = c.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        return await q.answer(tr(lang, "track_missing"), show_alert=True)
    await q.answer()
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
    await q.answer(tr(code, "lang_set") if code in STR else "✅ " + LANGS[code])
    text = tr(code, "welcome").rsplit("\n\n", 1)[0] + "\n\n🌐 /lang"
    try:
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=None, **NO_PREVIEW)
    except Exception:
        pass


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    await asyncio.to_thread(touch_user, q.from_user)
    try:
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
    except Exception:
        log.exception("button %s failed", data[:20])
        try:
            await q.message.reply_text(tr(await asyncio.to_thread(get_lang, q.from_user.id), "err"))
        except Exception:
            pass


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
        desc = it.get("channel") or it.get("uploader") or ""
        if it.get("duration"):
            d = int(it["duration"])
            desc += f" • {d // 60}:{d % 60:02d}"
        results.append(InlineQueryResultArticle(
            id=vid, title=(it.get("title") or vid)[:100], description=desc,
            thumbnail_url=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
            input_message_content=InputTextMessageContent(f"{MP3_PREFIX}https://www.youtube.com/watch?v={vid}")))
    await q.answer(results, cache_time=60)


# ======================================================================================
# Commands / lifecycle
# ======================================================================================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
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


def _fp_function():
    """The identify function of fp_engine, found by name."""
    if fp_engine:
        for name in ("identify", "recognize", "match", "lookup", "query"):
            f = getattr(fp_engine, name, None)
            if callable(f):
                log.info("fp_engine: using fp_engine.%s()", name)
                return f
        log.warning("fp_engine has no identify/recognize/match/lookup/query function: local database is off")
    return None


def main():
    migrate()
    seed_accounts()
    recognition.init(load_accounts, local_fn=_fp_function(), words_fn=find_song_by_words)
    if not load_accounts():
        log.warning("no ACRCloud account in the accounts table: only the local database and Shazam can recognize songs")
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    try:  # teach the bot songs by forwarding them to its private chat (learn_forward.py, hashes only)
        import learn_forward
        learn_forward.register(app)
    except Exception:
        log.exception("learn_forward not loaded - forwarding songs to teach the bot is off")
    app.add_handler(CommandHandler(["start", "lang"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle))
    app.add_error_handler(on_error)
    try:
        from admin import register as register_admin
        register_admin(app, sys.modules[__name__])
    except Exception:
        log.exception("admin module not loaded - the bot continues without it")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
