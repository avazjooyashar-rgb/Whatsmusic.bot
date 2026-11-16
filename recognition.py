"""
recognition.py - v14 (یک فایل، جای recognition.py قبلی؛ bot.py بدون تغییر کار می‌کنه)

همون سبک v12، ولی سریع‌تر و دقیق‌تر:
  - خروج سریع: جواب قوی (ACR یا Shazam) بعد از ۳ ثانیه مهلت، بدون منتظر موندن برای موتور کندتر
    (اگه موتور دیگه مخالف باشه جواب نمی‌ده و صبر می‌کنه)
  - دورها پشت‌سرهم منتظر نمی‌مونن: اگه ACR جواب داد و Shazam کنده، تکه‌ی بعدی موازی شروع می‌شه
  - Whisper فقط وقتی شروع می‌شه که جواب سریع نیومده (CPU برای ACR/Shazam آزاد می‌مونه)
  - اتصال HTTP به ACR دوباره استفاده می‌شه (Session)؛ تایم‌اوت Shazam کمتر (SHAZAM_TIMEOUT=20)
  - بعد از جواب، کارهای نیمه‌کاره لغو می‌شن
  - اگه دو جواب خیلی نزدیک بودن، به آخرین راه می‌ره تا تساوی شکسته بشه

قبلاً (v13):
  - Whisper جدا از pool اصلی و موازی با ACR/Shazam اجرا می‌شه (دیگه جواب قطعی رو عقب نمی‌ندازه)
  - مدل Whisper موقع استارت تو پس‌زمینه لود می‌شه (اولین کاربر منتظر نمی‌مونه)
  - deadline کلی برای کل تشخیص (TOTAL_TIMEOUT)
  - Humming (زمزمه) تنها هیچ‌وقت قبول نمی‌شه؛ نتیجه‌ی custom_files خود ACR هم خونده می‌شه
  - ACR: اکانتی که پشت‌سرهم خطا بده موقتاً کنار می‌ره
  - Shazam: یک بار تلاش مجدد + lock برای شمارنده‌ها
  - فیلتر توهم Whisper روی هر segment (نه کل متن) + فیلتر logprob / compression ratio
  - جستجوی ترانه با کل کلمات، بعد نصف اول و نصف دوم
  - دیتابیس خودت: هر تکه‌ی موافق یک رای جدا (شاهد بیشتر)
  - آخرین راه: ۶ نسخه (تمیز، اسلو، اسپید آپ، سرعت میانی، فقط‌صدا) روی چند تکه، با خروج زودهنگام
  - same_song: عنوان‌های عمومی (Intro / Outro ...) بدون خواننده ادغام نمی‌شن
  - امضای fp_engine یک بار با inspect چک می‌شه (نه با try/except روی TypeError)

ترتیب تشخیص:
  1) دیتابیس خودت (fp_engine): رایگان و سریع. روی ۳ تکه
       - قطعی: ۲ تکه روی یک ترک موافق باشن، یا امتیاز خیلی بالا -> همون لحظه جواب
       - ضعیف: به رای‌گیری میره به عنوان رای کم‌وزن (هیچ‌وقت تنها قبول نمی‌شه)
  2) رای‌گیری وزنی بین ACRCloud، Shazam، متن ترانه (Whisper)، رای ضعیف دیتابیس خودت
  3) آخرین راه: نسخه‌های دستکاری‌شده با Shazam + دیتابیس خودت

محیط (.env) - همه اختیاری:
  GUESS_MIN_W=0.75      حداقل مجموع وزن رای برای دادن جواب
  TOTAL_TIMEOUT=100     سقف زمان کل تشخیص (ثانیه)
  WHISPER_MODEL=small   خالی = تشخیص با متن ترانه خاموش (tiny/base/small/medium)
  WHISPER_LANG=         خالی = خودکار (مثلاً fa یا en)
  FAST_W=1.0  FAST_GRACE=3  SOFT_LAUNCH=6  LYRICS_DELAY=3  SHAZAM_TIMEOUT=20
  WHISPER_THREADS=4  WHISPER_MAX_PIECES=2  WHISPER_WAIT=45  WHISPER_BEAM=1  WHISPER_PRELOAD=1
  SHAZAM_THREADS=2
  FP_STRONG=60  FP_STRONG2=20  FP_WEAK_MIN=12  LOCAL_PIECES=3
  HUM_CAP=0.6           سقف وزن نتیجه‌ی زمزمه
  LAST_RESORT=1  LAST_RESORT_PIECES=2
  DB_PATH (برای شمارنده‌ی اکانت‌های ACR؛ همون DB_PATH بات)
"""
import asyncio
import base64
import difflib
import hashlib
import hmac
import inspect
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("recognition")


class NoAccounts(Exception):
    """No engine could answer at all (every call failed / no account)."""


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


GUESS_MIN_W = _f("GUESS_MIN_W", 0.75)
TOTAL_TIMEOUT = _f("TOTAL_TIMEOUT", 100)
FP_STRONG = _f("FP_STRONG", 60)        # امتیاز یک تکه که تنهایی قطعیه
FP_STRONG2 = _f("FP_STRONG2", 20)      # حداقل امتیاز وقتی دو تکه موافقن
FP_STRONG_SINGLE = _f("FP_STRONG_SINGLE", 45)   # وقتی فقط یک تکه داریم
FP_WEAK_MIN = _f("FP_WEAK_MIN", 12)
LOCAL_PIECES = max(1, int(_f("LOCAL_PIECES", 3)))
HUM_CAP = _f("HUM_CAP", 0.6)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip()
WHISPER_LANG = os.environ.get("WHISPER_LANG", "").strip()
WHISPER_MAX_PIECES = int(_f("WHISPER_MAX_PIECES", 2))
WHISPER_WAIT = _f("WHISPER_WAIT", 45)
WHISPER_PRELOAD = os.environ.get("WHISPER_PRELOAD", "1") == "1"
SHAZAM_THREADS = max(1, int(_f("SHAZAM_THREADS", 2)))
LAST_RESORT = os.environ.get("LAST_RESORT", "1") == "1"
LAST_RESORT_PIECES = max(1, int(_f("LAST_RESORT_PIECES", 2)))
ACR_TIMEOUT = 15
SHAZAM_TIMEOUT = _f("SHAZAM_TIMEOUT", 20)
FAST_W = _f("FAST_W", 1.0)            # وزن یک موتور که با مهلت کوتاه قبول می‌شه
FAST_GRACE = _f("FAST_GRACE", 3)      # مهلت برای دیدن مخالفت موتور دیگه
SOFT_LAUNCH = _f("SOFT_LAUNCH", 6)    # بعد از این‌قدر، تکه‌ی بعدی موازی شروع می‌شه
LYRICS_DELAY = _f("LYRICS_DELAY", 3)  # Whisper تا این‌قدر صبر می‌کنه که ببینه جواب سریع میاد یا نه
TIE_GAP = 0.15                        # کمتر از این فاصله = تساوی، آخرین راه تلاش کنه
EARLY_W = 1.6       # دو موتور موافق = جواب قطعی
EARLY_GAP = 0.5
LAST_GAP = 0.3      # فاصله‌ی لازم برای خروج زودهنگام در آخرین راه
MAX_PIECES = 3

_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="rec")
_WPOOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")   # Whisper سریاله و pool اصلی رو نمی‌خوره
_VPOOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="variant")
_SESS = requests.Session()
_SESS.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=12))
_cfg = {"load": None, "local": None, "words": None, "local_excl": False}


def _accepts_exclude(fn):
    """True / False / None (نامشخص)"""
    if not fn:
        return False
    try:
        ps = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    return "exclude" in ps or any(p.kind == p.VAR_KEYWORD for p in ps.values())


def init(load_accounts, local_fn=None, words_fn=None):
    _cfg["load"], _cfg["local"], _cfg["words"] = load_accounts, local_fn, words_fn
    _cfg["local_excl"] = _accepts_exclude(local_fn)
    if words_fn and WHISPER_MODEL and WHISPER_PRELOAD:
        threading.Thread(target=_get_whisper, daemon=True, name="whisper-load").start()
    log.info("recognition v14: local=%s lyrics=%s whisper=%s",
             bool(local_fn), bool(words_fn), WHISPER_MODEL or "off")


# ======================================================================================
# متن و مقایسه (bot.py این‌ها رو import می‌کنه)
# ======================================================================================
_MARKS = re.compile(r"[\u064B-\u065F\u0670\u0640\u200c\u200d\u200e\u200f]")
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEAT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with)\s+.*$", re.I)
_SPLIT = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_VARIANT = re.compile(
    r"\b(remix|slowed|sped\s?up|speed\s?up|reverb|nightcore|karaoke|instrumental|live|cover|acoustic|"
    r"8d|mashup|bass\s?boosted|lofi|lo-fi|piano\s?version)\b|ریمیکس|ریمیک|اسلو|ورژن پیانو|کاور|بی کلام|اجرای زنده",
    re.I)
_GENERIC = {"intro", "outro", "interlude", "untitled", "unknown", "track", "song", "instrumental",
            "theme", "prelude", "reprise", "skit", "demo", "music", "audio", "beat"}


def fold(s) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک").replace("ۀ", "ه").replace("ة", "ه")
    s = _MARKS.sub("", s)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def title_key(title) -> str:
    t = fold(title)
    stripped = _FEAT.sub("", _BRACKETS.sub(" ", t))
    key = re.sub(r"[\W_]+", "", stripped)
    return key or re.sub(r"[\W_]+", "", t)


def artist_set(artist) -> set:
    out = {re.sub(r"[\W_]+", "", fold(p)) for p in _SPLIT.split(artist or "") if p and p.strip()}
    out.discard("")
    out.discard("unknown")
    return out


def first_artist(artist) -> str:
    parts = [p.strip() for p in _SPLIT.split(artist or "") if p and p.strip()]
    return parts[0] if parts else (artist or "").strip()


def is_variant(x) -> bool:
    t = x.get("title") if isinstance(x, dict) else str(x or "")
    return bool(_VARIANT.search(fold(t or "")))


def same_song(a: dict, b: dict) -> bool:
    ta, tb = title_key(a.get("title")), title_key(b.get("title"))
    if not ta or not tb:
        return False
    short, long_ = sorted((ta, tb), key=len)
    close = (ta == tb or difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.85
             or (len(short) >= 6 and short in long_ and len(short) / len(long_) >= 0.6))
    if not close:
        return False
    xa, xb = artist_set(a.get("artist")), artist_set(b.get("artist"))
    if not xa or not xb:
        # خواننده‌ی یکی خالیه: عنوان عمومی/خیلی کوتاه رو ادغام نکن (Intro, Outro, ...)
        return not (ta in _GENERIC or tb in _GENERIC or len(short) < 4)
    if xa & xb:
        return True
    return any((len(x) >= 4 and len(y) >= 4 and (x in y or y in x))
               or difflib.SequenceMatcher(None, x, y).ratio() >= 0.75 for x in xa for y in xb)


def is_rejected(song: dict, rejected) -> bool:
    tk = title_key(song.get("title"))
    if not tk:
        return False
    return any(tk == r or (len(r) >= 5 and difflib.SequenceMatcher(None, tk, r).ratio() >= 0.9)
               for r in (rejected or ()))


# ======================================================================================
# دیتابیس خودت
# ======================================================================================
def _call_local(path, exclude):
    fn = _cfg["local"]
    mode = _cfg["local_excl"]
    if mode is False or not exclude:
        return fn(path) if (mode is False or mode is None) else fn(path, exclude=())
    if mode is None:
        try:
            return fn(path, exclude=tuple(exclude))
        except TypeError:
            _cfg["local_excl"] = False
            return fn(path)
    return fn(path, exclude=tuple(exclude))


def _local_piece(path, rejected):
    """fp_engine برای یک تکه. نتیجه‌ی ردشده ('آهنگ اشتباه') کنار گذاشته می‌شه و نفر بعدی گرفته می‌شه."""
    exclude = set()
    for _ in range(4):
        try:
            r = _call_local(path, exclude)
        except Exception as e:
            log.warning("local db failed: %s", e)
            return None
        if not r:
            return None
        if is_rejected(r, rejected):
            if r.get("track_id") is None or _cfg["local_excl"] is False:
                return None
            exclude.add(r["track_id"])
            continue
        return r
    return None


def _alts_of(r) -> list:
    return [{"title": a["title"], "artist": a["artist"], "dur": a.get("duration"), "link": None, "yt": None}
            for a in (r.get("alternatives") or []) if a.get("title")]


def ask_local_full(paths, rejected=()):
    """-> (قطعی یا None, [رای ضعیف برای رای‌گیری])  رایگان، بدون سهمیه."""
    if not _cfg["local"] or not paths:
        return None, []
    rejected = set(rejected or ())
    pieces = list(paths[:LOCAL_PIECES])
    results = list(_POOL.map(lambda p: _local_piece(p, rejected), pieces))
    by = {}
    for i, (p, r) in enumerate(zip(pieces, results)):
        if r and r.get("track_id") is not None:
            by.setdefault(r["track_id"], []).append((i, p, r))
    if not by:
        return None, []
    tid, lst = max(by.items(), key=lambda kv: (len(kv[1]), max(x[2]["score"] for x in kv[1])))
    mx = max(x[2]["score"] for x in lst)
    i, p, r = max(lst, key=lambda x: x[2]["score"])
    log.info("local db: %s - %s | pieces=%d score=%s", r["artist"], r["title"], len(lst), mx)
    strong = (mx >= FP_STRONG or (len(lst) >= 2 and mx >= FP_STRONG2)
              or (len(pieces) == 1 and mx >= FP_STRONG_SINGLE))
    if strong:
        return {"title": r["title"], "artist": r["artist"], "link": None, "yt": None,
                "dur": r.get("duration"), "engine": "local", "alts": _alts_of(r),
                "track_id": tid, "piece_path": p}, []
    # رای ضعیف: هر تکه‌ی موافق یک رای جدا (تنها هرگز قبول نمی‌شه: سقف وزن ۰.۷ < GUESS_MIN_W)
    votes = []
    for j, pp, rr in lst:
        if rr["score"] >= FP_WEAK_MIN:
            votes.append({"src": "local", "title": rr["title"], "artist": rr["artist"], "dur": rr.get("duration"),
                          "link": None, "yt": None, "w": min(0.7, rr["score"] / 70), "path": pp, "idx": j})
    return None, votes


# ======================================================================================
# ACRCloud (ملودی/زمزمه هم از همین‌جاست: metadata.humming)
# ======================================================================================
_ACC = {"t": 0.0, "rows": []}
_ACC_BLOCK = {}
_ACC_ERR = {}
_LOCK = threading.Lock()


def _accounts() -> list:
    now = time.time()
    if now - _ACC["t"] > 30 and _cfg["load"]:
        try:
            _ACC["rows"] = list(_cfg["load"]() or [])
        except Exception:
            log.exception("load accounts failed")
        _ACC["t"] = now
    return [a for a in _ACC["rows"] if _ACC_BLOCK.get(a["id"], 0) < now]


def _bump(acc_id, col):
    if col not in ("uses", "errors"):
        return
    try:
        c = sqlite3.connect(os.environ.get("DB_PATH", "bot.db"), timeout=10)
        c.execute(f"UPDATE accounts SET {col}={col}+1 WHERE id=?", (acc_id,))
        c.commit()
        c.close()
    except Exception:
        pass


def _acc_fail(acc_id):
    """سه خطای پشت‌سرهم = ۵ دقیقه کنار."""
    with _LOCK:
        _ACC_ERR[acc_id] = _ACC_ERR.get(acc_id, 0) + 1
        if _ACC_ERR[acc_id] >= 3:
            _ACC_BLOCK[acc_id] = time.time() + 300
            _ACC_ERR[acc_id] = 0


def _acr_parse(j) -> list:
    out = []
    md = j.get("metadata") or {}
    for kind in ("custom_files", "music", "humming"):
        for m in (md.get(kind) or [])[:3]:
            if not m.get("title"):
                continue
            ext = m.get("external_metadata") or {}
            sp = ((ext.get("spotify") or {}).get("track") or {}).get("id")
            yt = (ext.get("youtube") or {}).get("vid")
            ms = m.get("duration_ms") or 0
            score = float(m.get("score") or 0) / 100
            if kind == "humming":
                w = min(HUM_CAP, score * 0.7)      # ملودی تنها هرگز قبول نمی‌شه
            elif kind == "music":
                w = min(1.1, score * 1.15)
            else:
                w = min(1.0, score * 1.05)
            out.append({"src": "acr", "title": m["title"],
                        "artist": ", ".join(a.get("name", "") for a in (m.get("artists") or [])[:2] if a.get("name")),
                        "w": w, "dur": ms / 1000 or None,
                        "link": f"https://open.spotify.com/track/{sp}" if sp else None,
                        "yt": f"https://www.youtube.com/watch?v={yt}" if yt else None})
    return out


def _acr(path):
    """-> (items, answered). answered=False یعنی هیچ اکانتی جواب نداد."""
    accounts = _accounts()
    if not accounts:
        return [], False
    with open(path, "rb") as f:
        data = f.read()
    for acc in accounts:
        try:
            ts = str(int(time.time()))
            sts = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
            sig = base64.b64encode(hmac.new(acc["secret"].encode(), sts.encode(), hashlib.sha1).digest()).decode()
            r = _SESS.post(f"https://{acc['host']}/v1/identify", timeout=ACR_TIMEOUT,
                           data={"access_key": acc["key"], "sample_bytes": str(len(data)), "timestamp": ts,
                                 "signature": sig, "data_type": "audio", "signature_version": "1"},
                           files={"sample": ("s.mp3", data)})
            j = r.json()
        except Exception as e:
            log.warning("acr #%s failed: %s", acc["id"], str(e)[:120])
            _bump(acc["id"], "errors")
            _acc_fail(acc["id"])
            continue
        _bump(acc["id"], "uses")
        _ACC_ERR[acc["id"]] = 0
        code = (j.get("status") or {}).get("code")
        if code == 0:
            return _acr_parse(j), True
        if code in (1001, 2000, 2001, 2002, 2003, 2004, 2005):   # چیزی پیدا نشد / نمونه قابل استفاده نبود
            return [], True
        if code == 3003:    # سهمیه‌ی این اکانت تموم شده: ۳۰ دقیقه کنار، اکانت بعدی
            _ACC_BLOCK[acc["id"]] = time.time() + 1800
        elif code in (3001, 3002, 3014):   # کلید / امضا اشتباه
            _ACC_BLOCK[acc["id"]] = time.time() + 3600
        else:
            _acc_fail(acc["id"])
        _bump(acc["id"], "errors")
        log.warning("acr #%s status %s", acc["id"], code)
    return [], False


# ======================================================================================
# Shazam
# ======================================================================================
_SH = {"fails": 0, "until": 0.0}
_SH_SEM = threading.Semaphore(SHAZAM_THREADS)


async def _shazam_async(path):
    from shazamio import Shazam
    s = Shazam()
    fn = getattr(s, "recognize", None) or getattr(s, "recognize_song")
    return await asyncio.wait_for(fn(path), SHAZAM_TIMEOUT)


def _shazam(path):
    """-> (items, answered)"""
    if time.time() < _SH["until"]:
        return [], False
    out, ok = None, False
    with _SH_SEM:
        for attempt in (1, 2):
            try:
                out = asyncio.run(_shazam_async(path))
                ok = True
                break
            except asyncio.TimeoutError:
                log.warning("shazam timeout")
                break                       # تایم‌اوت = دوباره تلاش نکن، وقت کل رو نخور
            except Exception as e:
                log.warning("shazam failed (try %d): %s", attempt, str(e)[:120])
                time.sleep(0.4)
    with _LOCK:
        if ok:
            _SH["fails"] = 0
        else:
            _SH["fails"] += 1
            if _SH["fails"] >= 3:
                _SH["until"] = time.time() + 60      # circuit breaker
                _SH["fails"] = 0
    if not ok:
        return [], False
    t = (out or {}).get("track")
    if not t or not t.get("title"):
        return [], True
    return [{"src": "shazam", "title": t["title"], "artist": t.get("subtitle", "") or "", "w": 1.0,
             "dur": None, "link": None, "yt": None}], True


# ======================================================================================
# متن ترانه: صدا -> متن (Whisper) -> پیدا کردن ترانه از روی کلمات
# ======================================================================================
_WH = {"m": None, "fail": False}
_WH_LOCK = threading.Lock()      # لود مدل
_WH_RUN = threading.Lock()       # اجرای مدل
_HALLU_SEG = re.compile(
    r"thanks? for watching|thank you|subtitles?|amara\.org|subscribe|www\.|\.com|"
    r"زیرنویس|تماشا کردی|تماشا کردین|ممنون از تماشا|اشتراک|لایک کنید", re.I)


def _get_whisper():
    if not WHISPER_MODEL or _WH["fail"]:
        return None
    with _WH_LOCK:
        if _WH["m"] is None and not _WH["fail"]:
            try:
                from faster_whisper import WhisperModel
                _WH["m"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                        cpu_threads=int(_f("WHISPER_THREADS", 4)))
                log.info("whisper model '%s' loaded", WHISPER_MODEL)
            except ImportError:
                _WH["fail"] = True
                log.warning("faster-whisper is not installed: lyrics recognition is off")
            except Exception as e:
                _WH["fail"] = True
                log.warning("whisper load failed: %s", str(e)[:150])
        return _WH["m"]


def _transcribe(path) -> str:
    model = _get_whisper()
    if model is None:
        return ""
    kw = dict(vad_filter=True, beam_size=max(1, int(_f("WHISPER_BEAM", 1))),
              condition_on_previous_text=False, temperature=0.0)
    if WHISPER_LANG:
        kw["language"] = WHISPER_LANG
    try:
        with _WH_RUN:
            segs, _info = model.transcribe(path, **kw)
            kept = []
            for s in segs:
                t = (getattr(s, "text", "") or "").strip()
                if not t:
                    continue
                if getattr(s, "no_speech_prob", 0) >= 0.6:
                    continue
                if getattr(s, "avg_logprob", 0) < -1.3:        # Whisper خودش مطمئن نیست
                    continue
                if getattr(s, "compression_ratio", 0) > 2.4:   # تکرار بی‌معنی
                    continue
                if len(t) < 60 and _HALLU_SEG.search(t):        # توهم رایج (فقط segment کوتاه)
                    continue
                kept.append(t)
            return " ".join(kept).strip()
    except Exception as e:
        log.warning("whisper failed: %s", str(e)[:150])
    return ""


def _usable_words(text):
    if not text:
        return []
    ws = re.findall(r"\w+", fold(text))
    if len(ws) < 5 or len(set(ws)) / len(ws) < 0.35:   # تکرار بی‌معنی = توهم Whisper
        return []
    return ws


def _find_by_words(ws):
    """کل کلمات؛ اگه پیدا نشد نصف اول و نصف دوم (ترانه‌ی بلند / متن ناقص)."""
    wf = _cfg["words"]
    tries = [ws]
    if len(ws) >= 14:
        h = len(ws) // 2
        tries += [ws[:h], ws[h:]]
    for t in tries:
        try:
            song = wf(t)
        except Exception as e:
            log.warning("lyrics search failed: %s", e)
            continue
        if song and song.get("title"):
            return song, len(t)
    return None, 0


def _lyrics_vote(paths, stop=None):
    wf = _cfg["words"]
    if not wf or not WHISPER_MODEL:
        return [], True
    for i, p in enumerate(paths[:max(1, WHISPER_MAX_PIECES)]):
        if stop is not None and stop.is_set():      # جواب قطعی شد یا وقت تموم شد
            return [], True
        text = _transcribe(p)
        ws = _usable_words(text)
        if not ws:
            continue
        log.info("lyrics heard (%d words): %s", len(ws), text[:90])
        song, n = _find_by_words(ws)
        if song:
            return [{"src": "lyrics", "title": song["title"], "artist": song.get("artist") or "",
                     "w": 0.9 if n >= 8 else 0.6, "dur": None, "link": song.get("link"),
                     "yt": None, "path": p, "idx": i}], True
    return [], True


# ======================================================================================
# رای‌گیری
# ======================================================================================
_ORDER = ("local", "acr", "shazam", "lyrics")
_NAME_PRIORITY = {"local": 0, "shazam": 1, "acr": 2, "lyrics": 3}


def _group(items):
    groups = []
    for c in sorted(items, key=lambda c: -c["w"]):
        for g in groups:
            if same_song(g[0], c):
                g.append(c)
                break
        else:
            groups.append([c])
    out = []
    for g in groups:
        per, pieces = {}, {}
        for c in g:
            per[c["src"]] = max(per.get(c["src"], 0), c["w"])
            pieces.setdefault(c["src"], set()).add(c.get("idx", 0))
        # یک موتور روی چند تکه‌ی مختلف همین جواب رو بده = شاهد بیشتر (سقف +۰.۶ برای هر موتور)
        bonus = sum(min(0.6, 0.3 * (len(s) - 1)) for s in pieces.values())
        if len(per) >= 3:       # سه موتور مستقل موافق
            bonus += 0.25
        out.append({"w": sum(per.values()) + bonus, "srcs": per, "items": g})
    out.sort(key=lambda x: (-x["w"], min(_NAME_PRIORITY.get(s, 9) for s in x["srcs"])))
    return out


def _decide(items, rejected):
    items = [c for c in items if not is_rejected(c, rejected)]
    groups = _group(items)
    second = groups[1]["w"] if len(groups) > 1 else 0.0
    return groups, second


def _to_song(groups):
    top = groups[0]
    its = sorted(top["items"], key=lambda c: _NAME_PRIORITY.get(c["src"], 9))
    rep = dict(its[0])
    for c in its[1:]:
        for k in ("dur", "link", "yt"):
            if not rep.get(k) and c.get(k):
                rep[k] = c[k]
    engines = "+".join(s for s in _ORDER if s in top["srcs"])
    audio_hit = next((c for c in its if c["src"] in ("acr", "shazam") and c.get("path")), None) \
        or next((c for c in its if c.get("path")), None)
    alts = []
    for g in groups[1:7]:
        r = g["items"][0]
        alts.append({k: r.get(k) for k in ("title", "artist", "link", "yt", "dur")})
    song = {"title": rep["title"], "artist": rep.get("artist") or "", "link": rep.get("link"),
            "yt": rep.get("yt"), "dur": rep.get("dur"), "engine": engines, "alts": alts, "w": top["w"]}
    if audio_hit:
        song["piece_path"] = audio_hit["path"]
    return song


def _tag(items, path, idx):
    for c in items:
        c["path"], c["idx"] = path, idx
    return items


# ======================================================================================
# آخرین راه: نسخه‌ی تمیز / تغییر سرعت / فقط صدای خواننده
# ======================================================================================
_VARIANTS = {
    "clean": "aresample=44100,highpass=f=100,lowpass=f=7500,dynaudnorm",
    "fast": "aresample=44100,asetrate=55125,aresample=44100",    # اسلو -> سرعت عادی (×1.25)
    "slow": "aresample=44100,asetrate=35280,aresample=44100",    # اسپید آپ -> سرعت عادی (×0.8)
    "fast2": "aresample=44100,asetrate=48510,aresample=44100",   # اسلو کم (×1.1)
    "slow2": "aresample=44100,asetrate=39690,aresample=44100",   # اسپید آپ کم (×0.9)
    "vocal": "aresample=44100,highpass=f=180,lowpass=f=4000,dynaudnorm=f=150",   # بیشتر صدا، کمتر ساز
}
_EXTRA_PIECE_VARIANTS = ("clean", "fast", "slow")


def _variant(path, kind):
    out = f"{path}_{kind}.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-vn", "-af", _VARIANTS[kind],
                    "-ac", "1", "-ar", "44100", out], check=True, capture_output=True, timeout=60)
    return out


def _try_variant(path, kind, rejected):
    items, answered = [], False
    try:
        v = _variant(path, kind)
    except Exception as e:
        log.info("variant %s failed: %s", kind, str(e)[:100])
        return [], False
    try:
        got, ok = _shazam(v)
        answered = ok
        for c in got:
            c["w"] = 0.9      # کمی کمتر: صدا دستکاری شده
        items += got
        if _cfg["local"]:
            r = _local_piece(v, rejected)
            if r and r.get("score", 0) >= FP_WEAK_MIN:
                items.append({"src": "local", "title": r["title"], "artist": r["artist"], "dur": r.get("duration"),
                              "link": None, "yt": None, "w": 0.9 if r["score"] >= 40 else min(0.7, r["score"] / 70)})
                answered = True
    finally:
        try:
            os.remove(v)
        except OSError:
            pass
    return items, answered


# ======================================================================================
# موتورهای آنلاین + رای‌گیری (۱ سهمیه‌ی کاربر)
# ======================================================================================
def ask_engines(paths, rejected=(), _unused1=None, _unused2=None, extra=None):
    stop = threading.Event()
    try:
        return _ask_engines(paths, set(rejected or ()), list(extra or []), stop)
    finally:
        stop.set()      # Whisper اگه هنوز تو صفه، دیگه شروع نکنه


def _single_fast(groups, second):
    """یک موتور آنلاین (ACR / Shazam) جواب قوی داده و هیچ جواب مخالفی نزدیکش نیست."""
    if not groups:
        return False
    g = groups[0]
    return (g["w"] >= FAST_W and ("acr" in g["srcs"] or "shazam" in g["srcs"])
            and g["w"] - second >= EARLY_GAP)


def _ask_engines(paths, rejected, items, stop):
    pieces = list(paths[:MAX_PIECES])
    if not pieces:
        return None
    t0 = time.time()
    deadline = t0 + TOTAL_TIMEOUT
    use_lyrics = bool(_cfg["words"] and WHISPER_MODEL)

    answered = failed = 0
    futs = {}           # future -> (kind, path, idx)
    acr_f = {}          # idx -> future ACR همون تکه
    handled = set()
    st = {"launched": 0, "last": t0, "lyr": None, "lyr_taken": False, "logged": -1}

    def left():
        return max(0.2, deadline - time.time())

    def launch(i):
        p = pieces[i]
        fa, fs = _POOL.submit(_acr, p), _POOL.submit(_shazam, p)
        futs[fa], futs[fs] = ("acr", p, i), ("shazam", p, i)
        acr_f[i] = fa
        st["launched"], st["last"] = i + 1, time.time()

    def start_lyrics():
        if use_lyrics and st["lyr"] is None:
            st["lyr"] = _WPOOL.submit(_lyrics_vote, pieces, stop)

    def take_lyrics(block=False):
        lyr = st["lyr"]
        if lyr is None or st["lyr_taken"]:
            return
        if not lyr.done():
            if not block:
                return
            wait([lyr], timeout=max(0.0, min(deadline, t0 + WHISPER_WAIT) - time.time()))
            if not lyr.done():
                return
        st["lyr_taken"] = True
        try:
            got, _ok = lyr.result()
            items.extend(got)       # متن ترانه جزو «موتور جواب‌دهنده» حساب نمی‌شه
        except Exception as e:
            log.warning("lyrics crashed: %s", e)

    def harvest(pool):
        nonlocal answered, failed
        for f, (kind, path, idx) in list(pool.items()):
            if f in handled or not f.done():
                continue
            handled.add(f)
            try:
                got, ok = f.result()
            except Exception as e:
                log.warning("%s crashed: %s", kind, e)
                failed += 1
                continue
            answered, failed = answered + (1 if ok else 0), failed + (0 if ok else 1)
            items.extend(_tag(got, path, idx))

    def show(tag):
        if len(items) != st["logged"]:
            st["logged"] = len(items)
            groups, _ = _decide(items, rejected)
            log.info("%s (%.1fs): %s", tag, time.time() - t0,
                     [(round(g["w"], 2), g["items"][0]["title"], sorted(g["srcs"])) for g in groups[:3]])

    try:
        launch(0)
        grace_until = None
        while time.time() < deadline:
            harvest(futs)
            take_lyrics()
            show("round")
            groups, second = _decide(items, rejected)
            now = time.time()
            # ۱) قطعی: دو موتور موافق
            if groups and groups[0]["w"] >= EARLY_W and groups[0]["w"] - second >= EARLY_GAP:
                return _to_song(groups)
            # ۲) سریع: یک موتور قوی؛ مهلت کوتاه تا موتور دیگه مخالفت نکرده
            if _single_fast(groups, second):
                if grace_until is None:
                    grace_until = now + FAST_GRACE
                if now >= grace_until:
                    log.info("fast accept: %s", groups[0]["items"][0]["title"])
                    return _to_song(groups)
            else:
                grace_until = None
            # ۳) Whisper فقط وقتی جواب سریعی در راه نیست
            if st["lyr"] is None and grace_until is None and now - t0 >= LYRICS_DELAY:
                start_lyrics()
            pending = [f for f in futs if not f.done()]
            # ۴) تکه‌ی بعدی: همه تموم شدن، یا ACR تموم شده و موتور کند معطل کرده
            if st["launched"] < len(pieces):
                last_acr = acr_f[st["launched"] - 1]
                if not pending or (last_acr.done() and now - st["last"] >= SOFT_LAUNCH and grace_until is None):
                    launch(st["launched"])
                    continue
            if not pending:
                break
            wait(pending, timeout=min(0.4, left()), return_when=FIRST_COMPLETED)
        harvest(futs)

        start_lyrics()                  # جواب سریع نیومد: حالا نوبت متن ترانه‌ست
        take_lyrics(block=True)
        groups, second = _decide(items, rejected)
        show("final")
        if groups and groups[0]["w"] >= GUESS_MIN_W and (groups[0]["w"] - second >= TIE_GAP or not LAST_RESORT):
            return _to_song(groups)
        tie = bool(groups and groups[0]["w"] >= GUESS_MIN_W)

        if LAST_RESORT and time.time() < deadline:
            tasks = [(pieces[0], 0, k) for k in _VARIANTS]
            for j in range(1, min(LAST_RESORT_PIECES, len(pieces))):
                tasks += [(pieces[j], j, k) for k in _EXTRA_PIECE_VARIANTS]
            vfuts = {_VPOOL.submit(_try_variant, p, k, rejected): (k, p, j) for p, j, k in tasks}
            futs.update(vfuts)
            pending = set(vfuts)
            while pending and time.time() < deadline:
                done, pending = wait(pending, timeout=left(), return_when=FIRST_COMPLETED)
                if not done:
                    break
                harvest(vfuts)
                groups, second = _decide(items, rejected)
                if groups and groups[0]["w"] >= GUESS_MIN_W and groups[0]["w"] - second >= LAST_GAP:
                    log.info("found by the last resort (early): %s", groups[0]["items"][0]["title"])
                    return _to_song(groups)
            harvest(vfuts)
            groups, second = _decide(items, rejected)
            if groups and groups[0]["w"] >= GUESS_MIN_W:
                log.info("found by the last resort: %s", groups[0]["items"][0]["title"])
                return _to_song(groups)

        if tie:      # آخرین راه هم تساوی رو نشکست: بهترین حدس
            return _to_song(_decide(items, rejected)[0])
        if answered == 0 and failed > 0:
            raise NoAccounts("no engine answered")
        return None
    finally:
        for f in futs:          # کارهای هنوز شروع‌نشده لغو بشن
            f.cancel()
        if st["lyr"] is not None:
            st["lyr"].cancel()
