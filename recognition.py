"""
recognition.py - v16 (یک فایل کامل، جای recognition.py قبلی؛ bot.py بدون تغییر کار می‌کنه)

تغییرهای v16 نسبت به v15:
  سرعت:
  - Shazam حالا یک event-loop دائمی و یک نمونه‌ی Shazam مشترک داره (دیگه هر بار asyncio.run نمی‌زنه)
  - Shazam برای هر سه تکه از همون لحظه‌ی اول موازی شروع می‌شه (رایگانه)
  - اگه همه‌ی موتورهای آنلاینِ شروع‌شده جواب دادن و جواب قوی و بدون مخالفه، بدون صبر (grace) فوری قبول می‌شه
  - Whisper: زبان بعد از اولین تکه قفل می‌شه (تکه‌ی دوم سریع‌تر)، و منتظر کامل شدن نمی‌مونه؛ نتیجه‌اش همون لحظه برداشته می‌شه
  - نسخه‌های «آخرین راه» تو /dev/shm (RAM) ساخته می‌شن و شش‌تا همزمان
  - SOFT_LAUNCH=4 (قبلاً ۶): تکه‌ی بعدیِ ACR زودتر شروع می‌شه
  دقت:
  - نسخه‌های جدید آخرین راه که سرعت رو عوض می‌کنن ولی پیچ رو نه (tempo)، و پیچ رو عوض می‌کنن ولی سرعت رو نه (pitch ±2)؛
    برای کاور/اجرای کندتر-تندتر یا کلید متفاوت
  - بونس ترکیبی: متن ترانه + دیتابیس خودت / ملودی / موتور آنلاین هم‌نظر بشن، اطمینان بالاتر می‌ره
  - متن ترانه: کلمه‌های تکراریِ بیش از حد (hallucination) بهتر فیلتر می‌شن

دانلود (تو همین فایل): mp3 با کاور + تگ، موازی و کش‌شده
    fut = recognition.start_download(song["title"], song["artist"], dur=song.get("dur"),
                                     cover_url=song.get("cover"), yt=song.get("yt"))
    path = await asyncio.to_thread(fut.result, 120)
    await bot.send_audio(chat_id, open(path, "rb"), title=song["title"], performer=song["artist"],
                         thumbnail=recognition.thumb_of(path))
  env: COOKIES_FILE (همون کوکی بات؛ یا YT_COOKIES)  DL_BITRATE=192  DL_DIR=/tmp/dl  DL_FRAGMENTS=8

ترتیب تشخیص:
  1) دیتابیس خودت (fp_engine)  2) رای‌گیری ACR + Shazam + ملودی + متن ترانه + رای ضعیف دیتابیس
  3) آخرین راه: نسخه‌های دستکاری‌شده

محیط (.env) - همه اختیاری:
  GUESS_MIN_W=0.75  TOTAL_TIMEOUT=100
  WHISPER_MODEL=small   (دقیق‌تر: medium یا large-v3-turbo؛ خالی = متن ترانه خاموش)
  WHISPER_COMPUTE=int8  WHISPER_LANG=  WHISPER_THREADS=4  WHISPER_MAX_PIECES=2  WHISPER_WAIT=45
  WHISPER_BEAM=1 (برای دقت بیشتر ۳ یا ۵؛ کندتر)  WHISPER_PRELOAD=1
  FAST_W=1.0  FAST_GRACE=2  SOFT_LAUNCH=4  LYRICS_DELAY=2  SHAZAM_TIMEOUT=20  SHAZAM_THREADS=3
  FP_STRONG=60  FP_STRONG2=20  FP_WEAK_MIN=12  LOCAL_PIECES=3
  GROQ_API_KEY= GROQ_MODEL=whisper-large-v3 (اختیاری)  DEBUG_KEEP_DIR=/tmp/dbg (فقط عیب‌یابی)  HUM_CAP=0.6  LAST_RESORT=1  LAST_RESORT_PIECES=2  VAR_WORKERS=2 LAST_RESORT_BUDGET=15 WHISPER_LOOSE=1  DB_PATH
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
import tempfile
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, Future, wait, FIRST_COMPLETED

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
FP_STRONG = _f("FP_STRONG", 60)
FP_STRONG2 = _f("FP_STRONG2", 20)
FP_STRONG_SINGLE = _f("FP_STRONG_SINGLE", 45)
FP_WEAK_MIN = _f("FP_WEAK_MIN", 12)
LOCAL_PIECES = max(1, int(_f("LOCAL_PIECES", 3)))
HUM_CAP = _f("HUM_CAP", 0.6)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip()
WHISPER_COMPUTE = os.environ.get("WHISPER_COMPUTE", "int8").strip() or "int8"
WHISPER_LANG = os.environ.get("WHISPER_LANG", "").strip()
WHISPER_MAX_PIECES = int(_f("WHISPER_MAX_PIECES", 3))
WHISPER_WAIT = _f("WHISPER_WAIT", 45)
WHISPER_PRELOAD = os.environ.get("WHISPER_PRELOAD", "1") == "1"
WHISPER_LOOSE = os.environ.get("WHISPER_LOOSE", "1") == "1"
GROQ_KEY = os.environ.get("GROQ_API_KEY", "").strip()      # اختیاری: Whisper large-v3 میزبانی‌شده (دقیق‌تر + CPU سرور رو مصرف نمی‌کنه)
GROQ_MODEL = os.environ.get("GROQ_MODEL", "whisper-large-v3").strip()
GROQ_TIMEOUT = _f("GROQ_TIMEOUT", 12)
LYRICS_SEARCH_BUDGET = _f("LYRICS_SEARCH_BUDGET", 15)
LYRICS_FINAL_WAIT = _f("LYRICS_FINAL_WAIT", 10)
GROQ_FALLBACK_LOCAL = os.environ.get("GROQ_FALLBACK_LOCAL", "0") == "1"   # اگه Groq جواب داد ولی متن خالی بود، Whisper محلی هم امتحان بشه؟
SHAZAM_THREADS = max(1, int(_f("SHAZAM_THREADS", 3)))
LAST_RESORT = os.environ.get("LAST_RESORT", "1") == "1"
LAST_RESORT_PIECES = max(1, int(_f("LAST_RESORT_PIECES", 2)))
VAR_WORKERS = max(1, int(_f("VAR_WORKERS", 2)))          # روی ۲ هسته بیشتر از ۲ ffmpeg همزمان Shazam رو گرسنه می‌کنه
LAST_RESORT_BUDGET = _f("LAST_RESORT_BUDGET", 15)
ACR_TIMEOUT = 15
SHAZAM_TIMEOUT = _f("SHAZAM_TIMEOUT", 8)          # هر تلاش؛ اتصال گیرکرده رها و دوباره امتحان می‌شه
SHAZAM_TRIES = max(1, int(_f("SHAZAM_TRIES", 2)))
SHAZAM_COOLDOWN = _f("SHAZAM_COOLDOWN", 120)
SHAZAM_VARIANT_KINDS = {k.strip() for k in os.environ.get("SHAZAM_VARIANT_KINDS", "clean,slow,fast").split(",") if k.strip()}
FAST_W = _f("FAST_W", 1.0)
FAST_GRACE = _f("FAST_GRACE", 2)
SOFT_LAUNCH = _f("SOFT_LAUNCH", 4)
LYRICS_DELAY = _f("LYRICS_DELAY", 2)
TIE_GAP = 0.15
EARLY_W = 1.6
EARLY_GAP = 0.5
LAST_GAP = 0.3
MAX_PIECES = 3

_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="rec")
_WPOOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper")
_VPOOL = ThreadPoolExecutor(max_workers=VAR_WORKERS, thread_name_prefix="variant")
_SESS = requests.Session()
_SESS.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=12))
_cfg = {"load": None, "local": None, "words": None, "local_excl": False}

_ONLINE = ("acr", "shazam")


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
    threading.Thread(target=_sh_warm, daemon=True, name="shazam-warm").start()
    log.info("recognition v16.14: local=%s lyrics=%s whisper=%s",
             bool(local_fn), bool(words_fn), ("groq+" if GROQ_KEY else "") + (WHISPER_MODEL or "off"))


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
        log.info("local db: no match (%d pieces)", len(pieces))
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
    votes = []
    for j, pp, rr in lst:
        if rr["score"] >= FP_WEAK_MIN:
            votes.append({"src": "local", "title": rr["title"], "artist": rr["artist"], "dur": rr.get("duration"),
                          "link": None, "yt": None, "w": min(0.7, rr["score"] / 70), "path": pp, "idx": j})
    return None, votes


# ======================================================================================
# ACRCloud (ملودی/زمزمه هم از همین‌جاست: metadata.humming)  ->  ملودی یک رای مستقل (hum)
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


def _num(x, default=0.0) -> float:
    """ACRCloud بعضی فیلدها (duration_ms, score) رو گاهی رشته می‌فرسته."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


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
            ms = _num(m.get("duration_ms"))
            score = _num(m.get("score")) / 100
            if kind == "humming":
                w = min(HUM_CAP, score * 0.7)      # ملودی تنها هرگز قبول نمی‌شه
                src = "hum"
            elif kind == "music":
                w = min(1.1, score * 1.15)
                src = "acr"
            else:
                w = min(1.0, score * 1.05)
                src = "acr"
            out.append({"src": src, "title": m["title"],
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
        with _LOCK:
            _ACC_ERR[acc["id"]] = 0
        code = (j.get("status") or {}).get("code")
        if code == 0:
            parsed = _acr_parse(j)
            log.info("acr #%s ok: %s", acc["id"], [(c["src"], c["title"], round(c["w"], 2)) for c in parsed[:4]])
            return parsed, True
        if code in (1001, 2000, 2001, 2002, 2003, 2004, 2005):
            log.info("acr #%s: no match (status %s)", acc["id"], code)
            return [], True
        if code == 3003:
            _ACC_BLOCK[acc["id"]] = time.time() + 1800
        elif code in (3001, 3002, 3014):
            _ACC_BLOCK[acc["id"]] = time.time() + 3600
        else:
            _acc_fail(acc["id"])
        _bump(acc["id"], "errors")
        log.warning("acr #%s status %s", acc["id"], code)
    return [], False


# ======================================================================================
# Shazam (هر تماس لوپ مخصوص خودش رو داره؛ تماس‌ها موازی‌ان و هم‌دیگه رو بلاک نمی‌کنن)
# ======================================================================================
_SH = {"fails": 0, "until": 0.0}
_SH_SEM = threading.Semaphore(SHAZAM_THREADS)


def _sh_warm():
    try:
        from shazamio import Shazam  # noqa: F401  (import گرم می‌شه)
    except Exception:
        pass


async def _shazam_async(path):
    from shazamio import Shazam
    s = Shazam()
    fn = getattr(s, "recognize", None) or getattr(s, "recognize_song")
    return await asyncio.wait_for(fn(path), SHAZAM_TIMEOUT)


def _shazam(path, variant=False):
    """-> (items, answered)
    variant=True: نسخه‌ی دستکاری‌شده؛ یک تلاش، و شکستش حساب breaker نمی‌شه."""
    if time.time() < _SH["until"]:
        return [], False
    out, ok = None, False
    tries = 1 if variant else SHAZAM_TRIES
    t_start = time.time()
    with _SH_SEM:
        for attempt in range(1, tries + 1):
            try:
                out = asyncio.run(_shazam_async(path))
                ok = True
                break
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("shazam timeout (try %d, %.0fs)", attempt, time.time() - t_start)
                continue
            except Exception as e:
                log.warning("shazam failed (try %d): %s", attempt, str(e)[:120])
                if "No module named" in str(e):
                    break
                time.sleep(0.3)
    if variant:
        if not ok:
            return [], False
    else:
        with _LOCK:
            if ok:
                _SH["fails"] = 0
            else:
                _SH["fails"] += 1
            if _SH["fails"] >= 3:
                _SH["until"] = time.time() + SHAZAM_COOLDOWN
                _SH["fails"] = 0
                log.warning("shazam disabled for %ds after repeated failures (server IP may be blocked/slow)",
                            SHAZAM_COOLDOWN)
    if not ok:
        return [], False
    t = (out or {}).get("track")
    log.info("shazam %s (%.1fs): %s", "variant" if variant else "ok", time.time() - t_start,
             (t or {}).get("title") or "no match")
    if not t or not t.get("title"):
        return [], True
    imgs = t.get("images") or {}
    cover = imgs.get("coverarthq") or imgs.get("coverart") or None
    return [{"src": "shazam", "title": t["title"], "artist": t.get("subtitle", "") or "", "w": 1.0,
             "dur": None, "link": None, "yt": None, "cover": cover}], True


# ======================================================================================
# متن ترانه: صدا -> متن (Whisper) -> پیدا کردن ترانه از روی کلمات
# ======================================================================================
_WH = {"m": None, "fail": False}
_WH_LOCK = threading.Lock()
_WH_RUN = threading.Lock()
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
                _WH["m"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE,
                                        cpu_threads=int(_f("WHISPER_THREADS", 4)))
                log.info("whisper model '%s' loaded", WHISPER_MODEL)
            except ImportError:
                _WH["fail"] = True
                log.warning("faster-whisper is not installed: lyrics recognition is off")
            except Exception as e:
                _WH["fail"] = True
                log.warning("whisper load failed: %s", str(e)[:150])
        return _WH["m"]


def _whisper_pass(model, path, lang, vad, strict):
    kw = dict(beam_size=max(1, int(_f("WHISPER_BEAM", 1))),
              condition_on_previous_text=False, temperature=0.0, vad_filter=bool(vad))
    if vad:
        kw["vad_parameters"] = dict(min_silence_duration_ms=300)
    if lang:
        kw["language"] = lang
    nsp, lp, cr = (0.6, -1.3, 2.4) if strict else (0.9, -1.7, 2.6)
    segs, info = model.transcribe(path, **kw)
    kept = []
    drop = {"no_speech": 0, "logprob": 0, "compress": 0, "halluc": 0}
    for s_ in segs:
        t = (getattr(s_, "text", "") or "").strip()
        if not t:
            continue
        if getattr(s_, "no_speech_prob", 0) >= nsp:
            drop["no_speech"] += 1
            continue
        if getattr(s_, "avg_logprob", 0) < lp:
            drop["logprob"] += 1
            continue
        if getattr(s_, "compression_ratio", 0) > cr:
            drop["compress"] += 1
            continue
        if len(t) < 60 and _HALLU_SEG.search(t):
            drop["halluc"] += 1
            continue
        kept.append(t)
    return " ".join(kept).strip(), info, drop


def _groq_transcribe(path, lang=None):
    """Whisper large-v3 از Groq (API سازگار با OpenAI). -> متن (ممکنه خالی) یا None اگه درخواست شکست خورد."""
    try:
        data = {"model": GROQ_MODEL, "temperature": "0", "response_format": "verbose_json"}
        if lang:
            data["language"] = lang
        with open(path, "rb") as f:
            r = _SESS.post("https://api.groq.com/openai/v1/audio/transcriptions",
                           headers={"Authorization": f"Bearer {GROQ_KEY}"}, data=data,
                           files={"file": (os.path.basename(path), f)}, timeout=GROQ_TIMEOUT)
        if r.status_code != 200:
            log.warning("groq asr HTTP %s: %s", r.status_code, r.text[:150].replace("\n", " "))
            return None
        j = r.json()
        segs = j.get("segments")
        if segs:
            kept = []
            for sg in segs:
                t = (sg.get("text") or "").strip()
                if not t or (sg.get("no_speech_prob") or 0) >= 0.9 or (sg.get("compression_ratio") or 0) > 2.6:
                    continue
                if len(t) < 60 and _HALLU_SEG.search(t):
                    continue
                kept.append(t)
            text = " ".join(kept).strip()
        else:
            text = (j.get("text") or "").strip()
        log.info("groq asr (%s): %d chars", GROQ_MODEL, len(text))
        return text
    except Exception as e:
        log.warning("groq asr failed: %s", str(e)[:150])
        return None


def _transcribe(path, lang=None, skip_vad=False):
    """-> (text, زبان تشخیص‌داده‌شده یا None, loose)
    پاس اول: با VAD و فیلتر سخت‌گیر (مناسب ویس). اگه چیزی نشنید، پاس دوم: بدون VAD و فیلتر نرم‌تر
    (VAD گاهی آواز با موزیک رو «غیرگفتار» می‌دونه و کل صدا رو حذف می‌کنه)."""
    lang = WHISPER_LANG or lang
    if GROQ_KEY:
        gt = _groq_transcribe(path, lang)
        if gt:
            return gt, None, False
        if gt is not None and not GROQ_FALLBACK_LOCAL:
            return "", None, False       # Groq جواب داد و چیزی نشنید (مثلاً فقط موزیکه): Whisper محلیِ کند رو تکرار نمی‌کنیم
    model = _get_whisper()
    if model is None:
        return "", None, False
    try:
        with _WH_RUN:
            text, loose = "", False
            if not skip_vad:
                text, info, drop = _whisper_pass(model, path, lang, vad=True, strict=True)
                log.info("whisper pass1 (vad): %d chars, dropped=%s, lang=%s(%.2f)", len(text), drop,
                         getattr(info, "language", None), getattr(info, "language_probability", 0) or 0)
            if not text and (WHISPER_LOOSE or skip_vad):
                text, info, drop = _whisper_pass(model, path, lang, vad=False, strict=False)
                loose = True
                log.info("whisper pass2 (no vad): %d chars, dropped=%s", len(text), drop)
            det = None
            if text and not WHISPER_LANG and not lang:
                if getattr(info, "language_probability", 0) >= 0.85 and getattr(info, "language", None):
                    det = info.language
            return text, det, loose
    except Exception as e:
        log.warning("whisper failed: %s", str(e)[:150])
    return "", None, False


def _usable_words(text):
    if not text:
        return []
    ws = re.findall(r"\w+", fold(text))
    if len(ws) < 4 or len(set(ws)) / len(ws) < 0.35:
        return []
    # تکرار پشت‌سرهمِ یک کلمه (۴ بار یا بیشتر) = hallucination
    run = 1
    for a, b in zip(ws, ws[1:]):
        run = run + 1 if a == b else 1
        if run >= 4:
            return []
    return ws


def _word_tries(ws):
    """کل کلمات و دو نیمه (پنجره‌بندی رو خود find_song_by_words بات انجام می‌ده)."""
    tries = [ws]
    if len(ws) >= 14:
        h = len(ws) // 2
        tries += [ws[:h], ws[h:]]
    return tries


def _find_by_words(ws):
    wf = _cfg["words"]
    t_begin = time.time()
    for t in _word_tries(ws):
        if time.time() - t_begin > LYRICS_SEARCH_BUDGET:      # جستجوی ترانه تو bot.py می‌تونه تا ۳۰ ثانیه طول بکشه؛ تلاش بعدی شروع نشه
            log.info("lyrics search: budget (%.0fs) used up", LYRICS_SEARCH_BUDGET)
            break
        try:
            song = wf(t)
        except Exception as e:
            log.warning("lyrics search failed: %s", e)
            continue
        if song and song.get("title"):
            log.info("lyrics search found: %s - %s (%d words)", song.get("artist"), song["title"], len(t))
            return song, len(t)
    log.info("lyrics search: no match (%d words)", len(ws))
    return None, 0


def _lyrics_vote(paths, stop=None):
    wf = _cfg["words"]
    if not wf or not (WHISPER_MODEL or GROQ_KEY):
        return [], True
    lang = None            # قفل زبان فقط داخل همین درخواست (بین کاربرها قفل نمی‌مونه)
    all_ws = []
    skip_vad = False       # اگه تکه‌ی قبلی فقط با پاس دوم متن داد، برای بعدی پاس VAD رو تکرار نمی‌کنیم
    for i, p in enumerate(paths[:max(1, WHISPER_MAX_PIECES)]):
        if stop is not None and stop.is_set():
            return [], True
        text, det, loose = _transcribe(p, lang, skip_vad)
        lang = lang or det
        skip_vad = skip_vad or loose
        ws = _usable_words(text)
        if not ws:
            log.info("lyrics: piece %d unusable (heard %d chars)", i, len(text))
            continue
        log.info("lyrics heard (%d words): %s", len(ws), text[:200])
        all_ws.extend(ws)
        song, n = _find_by_words(ws)
        if song:
            w = 0.9 if n >= 10 else (0.8 if n >= 8 else 0.6)
            if loose:
                w = min(w, 0.78)       # متن پاس دوم (بدون VAD) کمی کم‌اطمینان‌تره
            return [{"src": "lyrics", "title": song["title"], "artist": song.get("artist") or "",
                     "w": w, "dur": None, "link": song.get("link"),
                     "yt": None, "path": p, "idx": i}], True
    # هیچ تکه‌ای تنهایی جواب نداد: کلمه‌های همه‌ی تکه‌ها با هم (کلمه‌ی بیشتر = تطبیق قوی‌تر با ترانه)
    if len(all_ws) >= 10 and not (stop is not None and stop.is_set()):
        log.info("lyrics: trying all %d words of the pieces together", len(all_ws))
        song, n = _find_by_words(all_ws)
        if song:
            return [{"src": "lyrics", "title": song["title"], "artist": song.get("artist") or "",
                     "w": 0.85, "dur": None, "link": song.get("link"),
                     "yt": None, "path": paths[0], "idx": 0}], True
    return [], True


# ======================================================================================
# رای‌گیری
# ======================================================================================
_ORDER = ("local", "acr", "hum", "shazam", "lyrics")
_NAME_PRIORITY = {"local": 0, "shazam": 1, "acr": 2, "hum": 3, "lyrics": 4}


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
        bonus = sum(min(0.6, 0.3 * (len(s) - 1)) for s in pieces.values())
        if len(per) >= 3:
            bonus += 0.25
        # هم‌نظری متن ترانه با یک منبع مستقل (صدا + متن = دو نوع شاهد متفاوت)
        if "lyrics" in per:
            if "local" in per:
                bonus += 0.3
            if "hum" in per:
                bonus += 0.2
            if "acr" in per or "shazam" in per:
                bonus += 0.15
        elif "hum" in per and "local" in per:
            bonus += 0.15
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
        for k in ("dur", "link", "yt", "cover"):
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
            "yt": rep.get("yt"), "dur": rep.get("dur"), "cover": rep.get("cover"),
            "engine": engines, "alts": alts, "w": top["w"]}
    if audio_hit:
        song["piece_path"] = audio_hit["path"]
    return song


def _tag(items, path, idx):
    for c in items:
        c["path"], c["idx"] = path, idx
    return items


# ======================================================================================
# آخرین راه: نسخه‌ی تمیز / تغییر سرعت / تغییر پیچ / فقط صدای خواننده / نویزگیری
# ======================================================================================
_VAR_DIR = "/dev/shm" if (os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK)) else tempfile.gettempdir()

_VARIANTS = {
    "clean": "aresample=44100,highpass=f=100,lowpass=f=7500,dynaudnorm",
    "denoise": "aresample=44100,highpass=f=80,afftdn=nf=-25,dynaudnorm",
    "vocal": "aresample=44100,highpass=f=180,lowpass=f=4000,dynaudnorm=f=150",
    # سرعت+پیچ با هم (اسلوود/اسپدآپ)
    "fast": "aresample=44100,asetrate=55125,aresample=44100",
    "slow": "aresample=44100,asetrate=35280,aresample=44100",
    "fast2": "aresample=44100,asetrate=48510,aresample=44100",
    "slow2": "aresample=44100,asetrate=39690,aresample=44100",
    # فقط سرعت (پیچ ثابت): کاور/اجرای کندتر یا تندتر
    "tempo_up": "aresample=44100,atempo=1.12",
    "tempo_down": "aresample=44100,atempo=0.89",
    # فقط پیچ (سرعت ثابت): کاور با کلید متفاوت (±۲ نیم‌پرده)
    "pitch_up": "aresample=44100,asetrate=49500,atempo=0.8909,aresample=44100",
    "pitch_down": "aresample=44100,asetrate=39289,atempo=1.1225,aresample=44100",
}
_PRIMARY_VARIANTS = ("slow", "fast", "clean", "tempo_down", "tempo_up", "pitch_up", "pitch_down",
                     "denoise", "vocal", "slow2", "fast2")
_EXTRA_PIECE_VARIANTS = ("clean", "tempo_down", "pitch_up")


def _variant(path, kind):
    out = os.path.join(_VAR_DIR, f"rv_{uuid.uuid4().hex[:12]}_{kind}.mp3")
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-vn", "-af", _VARIANTS[kind],
                        "-ac", "1", "-ar", "44100", out], check=True, capture_output=True, timeout=60)
    except Exception:
        try:
            os.remove(out)
        except OSError:
            pass
        raise
    return out


def _try_variant(path, kind, rejected):
    items, answered = [], False
    try:
        v = _variant(path, kind)
    except Exception as e:
        log.info("variant %s failed: %s", kind, str(e)[:100])
        return [], False
    try:
        if kind in SHAZAM_VARIANT_KINDS:      # فقط چند نسخه‌ی مهم به Shazam می‌رن؛ ریزش درخواست باعث گیرکردنش می‌شد
            got, ok = _shazam(v, variant=True)
            answered = ok
            for c in got:
                c["w"] = 0.9
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
    t0 = time.time()
    try:
        sizes = []
        for p in list(paths)[:MAX_PIECES]:
            try:
                sizes.append(os.path.getsize(p) // 1024)
            except OSError:
                sizes.append(-1)
        log.info("START pieces(KB)=%s", sizes)
        dbg = os.environ.get("DEBUG_KEEP_DIR", "").strip()
        if dbg:                      # فقط برای عیب‌یابی: تکه‌های صدا رو نگه می‌داره تا دستی تست بشن
            try:
                import shutil
                os.makedirs(dbg, exist_ok=True)
                stamp = time.strftime("%H%M%S")
                for i, p in enumerate(list(paths)[:MAX_PIECES]):
                    shutil.copy(p, os.path.join(dbg, f"{stamp}_p{i}{os.path.splitext(p)[1] or '.mp3'}"))
                log.info("DEBUG pieces saved: %s/%s_p*", dbg, stamp)
            except Exception as e:
                log.info("DEBUG save failed: %s", str(e)[:100])
        res = _ask_engines(paths, set(rejected or ()), list(extra or []), stop)
        if res:
            log.info("RESULT (%.1fs): %s - %s [%s]", time.time() - t0, res.get("artist"), res.get("title"),
                     res.get("engine"))
        else:
            log.info("RESULT (%.1fs): NOT FOUND", time.time() - t0)
        return res
    finally:
        stop.set()


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
    use_lyrics = bool(_cfg["words"] and (WHISPER_MODEL or GROQ_KEY))

    answered = failed = 0
    futs = {}
    acr_f = {}
    sh_f = {}
    handled = set()
    st = {"launched": 0, "last": t0, "lyr": None, "lyr_taken": False, "logged": -1}

    def left():
        return max(0.2, deadline - time.time())

    def get_sh(i):
        """Shazam رایگانه: هر تکه فقط یک بار شروع می‌شه."""
        if i not in sh_f:
            f = _POOL.submit(_shazam, pieces[i])
            sh_f[i] = f
            futs[f] = ("shazam", pieces[i], i)
        return sh_f[i]

    def launch(i):
        """ACR (سهمیه‌دار) تکه‌به‌تکه؛ Shazam همه‌ی تکه‌ها از اول."""
        p = pieces[i]
        fa = _POOL.submit(_acr, p)
        futs[fa] = ("acr", p, i)
        acr_f[i] = fa
        get_sh(i)
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
            items.extend(got)
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

    def online_pending():
        return [f for f, (k, _p, _i) in futs.items() if k in _ONLINE and not f.done()]

    def show(tag):
        if len(items) != st["logged"]:
            st["logged"] = len(items)
            groups, _ = _decide(items, rejected)
            log.info("%s (%.1fs): %s", tag, time.time() - t0,
                     [(round(g["w"], 2), g["items"][0]["title"], sorted(g["srcs"])) for g in groups[:3]])

    try:
        launch(0)
        for i in range(1, len(pieces)):
            get_sh(i)       # Shazam همه‌ی تکه‌ها از همون اول موازی
        grace_until = None
        while time.time() < deadline:
            harvest(futs)
            take_lyrics()
            show("round")
            groups, second = _decide(items, rejected)
            now = time.time()
            if groups and groups[0]["w"] >= EARLY_W and groups[0]["w"] - second >= EARLY_GAP:
                return _to_song(groups)
            if len(groups) >= 2 and not online_pending() and st["launched"] >= min(2, len(pieces)):
                # دو موتور آنلاین (ACR و Shazam) هر دو جواب قوی دادن ولی اسم‌ها فرق داره (مثلاً فارسی/لاتین).
                # صبر برای ترانه یا آخرین راه فایده‌ای نداره: همین حالا قبول می‌شه.
                g1, g2 = groups[0], groups[1]
                strong = lambda g: g["w"] >= FAST_W and any(x in g["srcs"] for x in _ONLINE)
                if strong(g1) and strong(g2):
                    pick = g1
                    if "shazam" in g2["srcs"] and "shazam" not in g1["srcs"] and g1["w"] - g2["w"] <= 0.3:
                        pick = g2          # اسم Shazam معمولاً تمیزتره؛ اسم ACR به‌عنوان گزینه‌ی «آهنگ اشتباه» می‌مونه
                    log.info("two strong online answers with different names: accepting '%s' (other: '%s')",
                             pick["items"][0]["title"], (g2 if pick is g1 else g1)["items"][0]["title"])
                    return _to_song([pick] + [g for g in groups if g is not pick])
            if _single_fast(groups, second):
                # همه‌ی موتورهای آنلاین شروع‌شده جواب دادن و کسی مخالف نیست -> بدون صبر
                if not online_pending() and st["launched"] >= min(2, len(pieces)):
                    log.info("fast accept (all answered): %s", groups[0]["items"][0]["title"])
                    return _to_song(groups)
                if grace_until is None:
                    grace_until = now + FAST_GRACE
                if now >= grace_until:
                    log.info("fast accept: %s", groups[0]["items"][0]["title"])
                    return _to_song(groups)
            else:
                grace_until = None
            if st["lyr"] is None and grace_until is None and now - t0 >= LYRICS_DELAY:
                start_lyrics()
            pending = [f for f in futs if not f.done()]
            if st["launched"] < len(pieces):
                last_acr = acr_f[st["launched"] - 1]
                if not pending or (last_acr.done() and now - st["last"] >= SOFT_LAUNCH and grace_until is None):
                    launch(st["launched"])
                    continue
            if not pending:
                break
            waitset = list(pending)
            if st["lyr"] is not None and not st["lyr_taken"] and not st["lyr"].done():
                waitset.append(st["lyr"])
            wait(waitset, timeout=min(0.3, left()), return_when=FIRST_COMPLETED)
        harvest(futs)

        start_lyrics()
        take_lyrics(block=True)
        groups, second = _decide(items, rejected)
        show("final")
        if groups and groups[0]["w"] >= GUESS_MIN_W and (groups[0]["w"] - second >= TIE_GAP or not LAST_RESORT):
            return _to_song(groups)
        tie = bool(groups and groups[0]["w"] >= GUESS_MIN_W)

        shazam_up = time.time() >= _SH["until"]
        if LAST_RESORT and time.time() < deadline and (shazam_up or _cfg["local"]):
            tasks = [(pieces[0], 0, k) for k in _PRIMARY_VARIANTS]
            for j in range(1, min(LAST_RESORT_PIECES, len(pieces))):
                tasks += [(pieces[j], j, k) for k in _EXTRA_PIECE_VARIANTS]
            vfuts = {_VPOOL.submit(_try_variant, p, k, rejected): ("variant", p, j) for p, j, k in tasks}
            futs.update(vfuts)
            pending = set(vfuts)
            lr_end = min(deadline, time.time() + LAST_RESORT_BUDGET)
            while (pending or (st["lyr"] is not None and not st["lyr_taken"])) and time.time() < lr_end:
                # نتیجه‌ی ترانه هم همین‌جا برداشته می‌شه: اگه حین آخرین راه تموم شد، دور ریخته نمی‌شه
                w_set = set(pending)
                lyr = st["lyr"]
                if lyr is not None and not st["lyr_taken"] and not lyr.done():
                    w_set.add(lyr)
                if not w_set:
                    take_lyrics()
                else:
                    done, _nd = wait(w_set, timeout=max(0.2, lr_end - time.time()), return_when=FIRST_COMPLETED)
                    pending = {f for f in pending if not f.done()}
                    take_lyrics()
                    if not done:
                        break
                harvest(vfuts)
                groups, second = _decide(items, rejected)
                if groups and groups[0]["w"] >= GUESS_MIN_W and groups[0]["w"] - second >= LAST_GAP:
                    log.info("found by the last resort (early): %s", groups[0]["items"][0]["title"])
                    return _to_song(groups)
            harvest(vfuts)
            # اگه کار ترانه هنوز تموم نشده، چند ثانیه‌ی کوتاه آخر هم صبر می‌کنیم
            if st["lyr"] is not None and not st["lyr_taken"] and not st["lyr"].done():
                wait([st["lyr"]], timeout=max(0.0, min(deadline - time.time(), LYRICS_FINAL_WAIT)))
            take_lyrics()
            groups, second = _decide(items, rejected)
            if groups and groups[0]["w"] >= GUESS_MIN_W:
                log.info("found by the last resort: %s", groups[0]["items"][0]["title"])
                return _to_song(groups)

        if tie:
            return _to_song(_decide(items, rejected)[0])
        if answered == 0 and failed > 0:
            raise NoAccounts("no engine answered")
        return None
    finally:
        for f in futs:
            f.cancel()
        if st["lyr"] is not None:
            st["lyr"].cancel()


# ======================================================================================
# دانلود سریع mp3 با کاور (جای fastdl.py قبلی)
# ======================================================================================
DL_DIR = os.environ.get("DL_DIR", os.path.join(tempfile.gettempdir(), "dl"))
BITRATE = os.environ.get("DL_BITRATE", "192")
FRAGMENTS = int(os.environ.get("DL_FRAGMENTS", "8"))
COOKIES = (os.environ.get("YT_COOKIES") or os.environ.get("COOKIES_FILE") or "cookies.txt").strip()   # همون کوکی بات
os.makedirs(DL_DIR, exist_ok=True)

_DPOOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dl")
_CACHE = {}
_DLOCK = threading.Lock()
_DL_VARIANT = re.compile(r"remix|slowed|sped\s?up|reverb|nightcore|karaoke|instrumental|live|cover|acoustic|8d|"
                         r"mashup|bass\s?boost|lofi|lo-fi|piano|ریمیکس|اسلو|کاور|زنده", re.I)


def _norm(s):
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


# ---------------------------------------------------------------- cover
def _upscale(url):
    """آدرس‌های Apple/Shazam (…/400x400cc.jpg) رو به ۱۰۰۰×۱۰۰۰ تبدیل می‌کنه."""
    return re.sub(r"/\d+x\d+(?:[a-z]{2})?\.(jpg|png)$", r"/1000x1000bb.\1", url or "")


def _get_img(url):
    try:
        r = requests.get(url, timeout=8)
        if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) > 2000:
            return r.content
    except Exception:
        pass
    return None


def find_cover(title, artist, hint=None):
    """-> bytes (jpg/png) یا None. اول کاور خود Shazam، بعد iTunes، بعد Deezer."""
    if hint:
        for u in (_upscale(hint), hint):
            img = _get_img(u)
            if img:
                return img
    q = f"{artist} {title}".strip()
    try:
        j = requests.get("https://itunes.apple.com/search", timeout=8,
                         params={"term": q, "entity": "song", "limit": 5}).json()
        for it in j.get("results", []):
            if _norm(title) in _norm(it.get("trackName")) or _norm(it.get("trackName")) in _norm(title):
                img = _get_img(_upscale(it.get("artworkUrl100", "")).replace("100x100bb", "1000x1000bb"))
                if img:
                    return img
    except Exception as e:
        log.info("itunes cover failed: %s", str(e)[:80])
    try:
        j = requests.get("https://api.deezer.com/search", timeout=8, params={"q": q, "limit": 3}).json()
        for it in j.get("data", []):
            alb = it.get("album") or {}
            img = _get_img(alb.get("cover_xl") or alb.get("cover_big") or "")
            if img:
                return img
    except Exception as e:
        log.info("deezer cover failed: %s", str(e)[:80])
    return None


# ---------------------------------------------------------------- یوتیوب
def _ydl(**kw):
    import yt_dlp
    o = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 15, "retries": 3}
    if COOKIES and os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    o.update(kw)
    return yt_dlp.YoutubeDL(o)


def pick_video(title, artist, dur=None, want_variant=False):
    """بهترین نتیجه‌ی یوتیوب: کانال «- Topic» (نسخه‌ی رسمی)، نزدیک‌ترین مدت، بدون ریمیکس/لایو."""
    q = f"ytsearch6:{artist} {title}"
    with _ydl(extract_flat="in_playlist") as y:
        info = y.extract_info(q, download=False)
    best, best_s = None, -1e9
    for e in (info or {}).get("entries") or []:
        if not e or not e.get("id"):
            continue
        t, ch, d = e.get("title") or "", e.get("channel") or e.get("uploader") or "", e.get("duration")
        s = 0.0
        if ch.endswith("- Topic"):
            s += 6
        if "official audio" in t.lower() or "audio" in t.lower():
            s += 1.5
        if _norm(title) and _norm(title) in _norm(t):
            s += 3
        if _norm(artist.split(",")[0]) in _norm(t + " " + ch):
            s += 2
        if not want_variant and _DL_VARIANT.search(t) and not _DL_VARIANT.search(title):
            s -= 8
        if d and dur:
            s -= min(abs(d - dur), 120) / 6
        elif d and d > 900:
            s -= 6
        if s > best_s:
            best, best_s = e, s
    return f"https://www.youtube.com/watch?v={best['id']}" if best else None


def _fetch_audio(url, out_base):
    with _ydl(format="bestaudio[ext=m4a]/bestaudio/best", outtmpl=out_base + ".%(ext)s",
              concurrent_fragment_downloads=FRAGMENTS, noplaylist=True) as y:
        info = y.extract_info(url, download=True)
        return y.prepare_filename(info)


# ---------------------------------------------------------------- mp3 + کاور
def _tag_mp3(src, cover_bytes, title, artist, out):
    cover = None
    if cover_bytes:
        cover = out + ".cover.jpg"
        with open(cover, "wb") as f:
            f.write(cover_bytes)
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src]
    if cover:
        cmd += ["-i", cover, "-map", "0:a:0", "-map", "1:v:0", "-c:v", "mjpeg", "-q:v", "2",
                "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    else:
        cmd += ["-map", "0:a:0", "-vn"]
    if src.lower().endswith(".mp3"):
        cmd += ["-c:a", "copy", "-id3v2_version", "3",
                "-metadata", f"title={title}", "-metadata", f"artist={artist}", out]
        return _run_tag(cmd, cover)
    cmd += ["-c:a", "libmp3lame", "-b:a", f"{BITRATE}k", "-id3v2_version", "3",
            "-metadata", f"title={title}", "-metadata", f"artist={artist}", out]
    _run_tag(cmd, cover)


def _run_tag(cmd, cover):
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    finally:
        if cover and os.path.exists(cover):
            os.remove(cover)


def tag_mp3_with_cover(src, cover_bytes, title, artist, out, thumb=None):
    """فایل صوتی (m4a/mp3/...) -> mp3 با کاور + تگ. thumb: مسیر بندانگشتی JPEG برای تلگرام (اختیاری).
    -> True اگه کاور داخل فایل رفت."""
    _tag_mp3(src, cover_bytes, title, artist, out)
    if cover_bytes and thumb:
        tmp = thumb + ".src"
        try:
            with open(tmp, "wb") as f:
                f.write(cover_bytes)
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", tmp, "-frames:v", "1",
                            "-vf", "scale='min(320,iw)':-2", "-q:v", "5", thumb],
                           check=True, capture_output=True, timeout=30)
        except Exception as e:
            log.info("thumb failed: %s", str(e)[:80])
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return bool(cover_bytes)


def clean_dl_cache(max_age=6 * 3600):
    """فایل‌های قدیمی کش دانلود (DL_DIR) رو پاک می‌کنه؛ bot.py هر ۱۰ دقیقه صداش می‌زنه.
    فایل‌های ناقص آخرین راه تو /dev/shm هم اگه جا مونده باشن پاک می‌شن."""
    now = time.time()
    try:
        for n in os.listdir(DL_DIR):
            p = os.path.join(DL_DIR, n)
            if os.path.isfile(p) and now - os.path.getmtime(p) > max_age:
                os.remove(p)
    except OSError:
        pass
    try:
        for n in os.listdir(_VAR_DIR):
            if n.startswith("rv_"):
                p = os.path.join(_VAR_DIR, n)
                if os.path.isfile(p) and now - os.path.getmtime(p) > 900:
                    os.remove(p)
    except OSError:
        pass


def thumb_of(path):
    """بندانگشتی کوچک (۳۲۰px) برای send_audio تلگرام؛ یا None."""
    out = path + ".thumb.jpg"
    try:
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-an", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                        "-frames:v", "1", "-q:v", "5", out], check=True, capture_output=True, timeout=20)
        return open(out, "rb")
    except Exception:
        return None


def _job(title, artist, dur, cover_url, yt):
    key = hashlib.md5(f"{_norm(artist)}|{_norm(title)}".encode()).hexdigest()[:16]
    final = os.path.join(DL_DIR, key + ".mp3")
    if os.path.exists(final) and os.path.getsize(final) > 50_000:
        return final                                   # کش: دانلود دوباره لازم نیست
    cover_f = _DPOOL.submit(find_cover, title, artist, cover_url)     # کاور موازی با جستجو/دانلود
    url = yt or pick_video(title, artist, dur, want_variant=bool(_DL_VARIANT.search(title)))
    if not url:
        raise RuntimeError("no video found")
    base = os.path.join(DL_DIR, key + "_raw")
    raw = _fetch_audio(url, base)
    try:
        try:
            cover = cover_f.result(timeout=15)
        except Exception:
            cover = None
        tmp = final + ".part.mp3"
        _tag_mp3(raw, cover, title, artist, tmp)
        os.replace(tmp, final)
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    return final


def start_download(title, artist, dur=None, cover_url=None, yt=None) -> Future:
    """دانلود رو تو پس‌زمینه شروع می‌کنه (اگه قبلاً شروع شده همون Future رو می‌ده)."""
    k = (_norm(artist), _norm(title))
    with _DLOCK:
        f = _CACHE.get(k)
        stale = False
        if f is not None and f.done() and not f.exception():
            stale = not os.path.exists(f.result())
        if f is None or stale or (f.done() and f.exception()):
            f = _DPOOL.submit(_job, title, artist or "", dur, cover_url, yt)
            _CACHE[k] = f
        return f
