"""
recognition.py - v12 (یک فایل، جای recognition.py قبلی؛ bot.py بدون تغییر کار می‌کنه)

ترتیب تشخیص:
  1) دیتابیس خودت (fp_engine): رایگان و سریع. روی ۳ تکه‌ی بلندترین بخش آهنگ اجرا می‌شه.
       - قطعی: ۲ تکه روی یک ترک موافق باشن، یا امتیاز خیلی بالا -> همون لحظه جواب
       - ضعیف: به رای‌گیری میره به عنوان یک رای کم‌وزن (هیچ‌وقت تنها قبول نمی‌شه)
  2) رای‌گیری وزنی بین:
       - ACRCloud (چند اکانت به ترتیب اولویت؛ شامل نتیجه‌ی Humming = ملودی/زمزمه)
       - Shazam (با circuit breaker)
       - متن ترانه: faster-whisper صدا رو به متن می‌کنه، بعد ترانه از روی کلمات پیدا و تایید می‌شه
       - رای ضعیف دیتابیس خودت
     دور اول روی بلندترین تکه، اگه قطعی نشد دور دوم و سوم روی تکه‌های بعدی.
  3) آخرین راه: نسخه‌ی تمیزشده و نسخه‌ی تغییر سرعت (اسلو / اسپید آپ) با Shazam + دیتابیس خودت

محیط (.env) - همه اختیاری:
  GUESS_MIN_W=0.75      حداقل مجموع وزن رای برای دادن جواب
  WHISPER_MODEL=small   خالی = تشخیص با متن ترانه خاموش (tiny/base/small/medium)
  WHISPER_THREADS=4  WHISPER_MAX_PIECES=2
  SHAZAM_THREADS=2
  FP_STRONG=60  FP_STRONG2=20  FP_WEAK_MIN=12
  LAST_RESORT=1
  DB_PATH (برای شمارنده‌ی اکانت‌های ACR؛ همون DB_PATH بات)
"""
import asyncio
import base64
import difflib
import hashlib
import hmac
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait

import requests

log = logging.getLogger("recognition")


class NoAccounts(Exception):
    """No engine could answer at all (every call failed / no account)."""


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


GUESS_MIN_W = _f("GUESS_MIN_W", 0.75)
FP_STRONG = _f("FP_STRONG", 60)        # امتیاز یک تکه که تنهایی قطعیه
FP_STRONG2 = _f("FP_STRONG2", 20)      # حداقل امتیاز وقتی دو تکه موافقن
FP_STRONG_SINGLE = _f("FP_STRONG_SINGLE", 45)   # وقتی فقط یک تکه داریم
FP_WEAK_MIN = _f("FP_WEAK_MIN", 12)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small").strip()
WHISPER_MAX_PIECES = int(_f("WHISPER_MAX_PIECES", 2))
SHAZAM_THREADS = max(1, int(_f("SHAZAM_THREADS", 2)))
LAST_RESORT = os.environ.get("LAST_RESORT", "1") == "1"
ACR_TIMEOUT = 15
EARLY_W = 1.6       # دو موتور موافق = جواب قطعی
EARLY_GAP = 0.5

_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="rec")
_cfg = {"load": None, "local": None, "words": None}


def init(load_accounts, local_fn=None, words_fn=None):
    _cfg["load"], _cfg["local"], _cfg["words"] = load_accounts, local_fn, words_fn
    log.info("recognition v12: local=%s lyrics=%s whisper=%s",
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
    if not xa or not xb or xa & xb:
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
def _local_piece(path, rejected):
    """fp_engine برای یک تکه. نتیجه‌ی ردشده ('آهنگ اشتباه') کنار گذاشته می‌شه و نفر بعدی گرفته می‌شه."""
    fn = _cfg["local"]
    exclude = set()
    for _ in range(4):
        try:
            try:
                r = fn(path, exclude=tuple(exclude))
            except TypeError:
                r = fn(path)
        except Exception as e:
            log.warning("local db failed: %s", e)
            return None
        if not r:
            return None
        if is_rejected(r, rejected):
            if r.get("track_id") is None:
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
    pieces = list(paths[:3])
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
    if mx >= FP_WEAK_MIN:  # یک رای کم‌وزن؛ تنها هرگز قبول نمی‌شه (سقف وزن ۰.۷ < GUESS_MIN_W)
        return None, [{"src": "local", "title": r["title"], "artist": r["artist"], "dur": r.get("duration"),
                       "link": None, "yt": None, "w": min(0.7, mx / 70), "path": p, "idx": i}]
    return None, []


# ======================================================================================
# ACRCloud (ملودی/زمزمه هم از همین‌جاست: metadata.humming)
# ======================================================================================
_ACC = {"t": 0.0, "rows": []}
_ACC_BLOCK = {}


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


def _acr_parse(j) -> list:
    out = []
    md = j.get("metadata") or {}
    for kind, mult in (("music", 1.15), ("humming", 0.85)):
        for m in (md.get(kind) or [])[:3]:
            if not m.get("title"):
                continue
            ext = m.get("external_metadata") or {}
            sp = ((ext.get("spotify") or {}).get("track") or {}).get("id")
            yt = (ext.get("youtube") or {}).get("vid")
            ms = m.get("duration_ms") or 0
            out.append({"src": "acr", "title": m["title"],
                        "artist": ", ".join(a.get("name", "") for a in (m.get("artists") or [])[:2] if a.get("name")),
                        "w": min(1.1, float(m.get("score") or 0) / 100 * mult),
                        "dur": ms / 1000 or None,
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
            r = requests.post(f"https://{acc['host']}/v1/identify", timeout=ACR_TIMEOUT,
                              data={"access_key": acc["key"], "sample_bytes": str(len(data)), "timestamp": ts,
                                    "signature": sig, "data_type": "audio", "signature_version": "1"},
                              files={"sample": ("s.mp3", data)})
            j = r.json()
        except Exception as e:
            log.warning("acr #%s failed: %s", acc["id"], str(e)[:120])
            _bump(acc["id"], "errors")
            continue
        _bump(acc["id"], "uses")
        code = (j.get("status") or {}).get("code")
        if code == 0:
            return _acr_parse(j), True
        if code in (1001, 2000, 2001, 2002, 2003, 2004, 2005):   # چیزی پیدا نشد / نمونه قابل استفاده نبود
            return [], True
        if code == 3003:    # سهمیه‌ی این اکانت تموم شده: ۳۰ دقیقه کنار، اکانت بعدی
            _ACC_BLOCK[acc["id"]] = time.time() + 1800
        elif code in (3001, 3002, 3014):   # کلید / امضا اشتباه
            _ACC_BLOCK[acc["id"]] = time.time() + 3600
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
    return await asyncio.wait_for(fn(path), 30)


def _shazam(path):
    """-> (items, answered)"""
    if time.time() < _SH["until"]:
        return [], False
    with _SH_SEM:
        try:
            out = asyncio.run(_shazam_async(path))
        except Exception as e:
            _SH["fails"] += 1
            log.warning("shazam failed (%d): %s", _SH["fails"], str(e)[:120])
            if _SH["fails"] >= 3:
                _SH["until"] = time.time() + 60      # circuit breaker
                _SH["fails"] = 0
            return [], False
    _SH["fails"] = 0
    t = (out or {}).get("track")
    if not t or not t.get("title"):
        return [], True
    return [{"src": "shazam", "title": t["title"], "artist": t.get("subtitle", "") or "", "w": 1.0,
             "dur": None, "link": None, "yt": None}], True


# ======================================================================================
# متن ترانه: صدا -> متن (Whisper) -> پیدا کردن ترانه از روی کلمات
# ======================================================================================
_WH = {"m": None, "fail": False}
_WH_LOCK = threading.Lock()
_HALLUCINATION = re.compile(
    r"thanks? for watching|thank you|subtitles?|amara\.org|subscribe|www\.|\.com|زیرنویس|تماشا|اشتراک", re.I)


def _transcribe(path) -> str:
    if not WHISPER_MODEL or _WH["fail"]:
        return ""
    with _WH_LOCK:
        try:
            if _WH["m"] is None:
                from faster_whisper import WhisperModel
                _WH["m"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                        cpu_threads=int(_f("WHISPER_THREADS", 4)))
                log.info("whisper model '%s' loaded", WHISPER_MODEL)
            segs, _info = _WH["m"].transcribe(path, vad_filter=True, beam_size=1,
                                              condition_on_previous_text=False, temperature=0.0)
            return " ".join(s.text.strip() for s in segs if getattr(s, "no_speech_prob", 0) < 0.6).strip()
        except ImportError:
            _WH["fail"] = True
            log.warning("faster-whisper is not installed: lyrics recognition is off")
        except Exception as e:
            log.warning("whisper failed: %s", str(e)[:150])
    return ""


def _usable_words(text):
    if not text or _HALLUCINATION.search(text):
        return []
    ws = re.findall(r"\w+", fold(text))
    if len(ws) < 5 or len(set(ws)) / len(ws) < 0.35:   # تکرار بی‌معنی = توهم Whisper
        return []
    return ws


def _lyrics_vote(paths):
    wf = _cfg["words"]
    if not wf or not WHISPER_MODEL:
        return [], True
    for i, p in enumerate(paths[:max(1, WHISPER_MAX_PIECES)]):
        text = _transcribe(p)
        ws = _usable_words(text)
        if not ws:
            continue
        log.info("lyrics heard (%d words): %s", len(ws), text[:90])
        try:
            song = wf(ws)
        except Exception as e:
            log.warning("lyrics search failed: %s", e)
            song = None
        if song and song.get("title"):
            return [{"src": "lyrics", "title": song["title"], "artist": song.get("artist") or "",
                     "w": 0.9 if len(ws) >= 8 else 0.6, "dur": None, "link": song.get("link"),
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
# آخرین راه: نسخه‌ی تمیز / تغییر سرعت
# ======================================================================================
_VARIANTS = {
    "clean": "aresample=44100,highpass=f=100,lowpass=f=7500,dynaudnorm",
    "fast": "aresample=44100,asetrate=55125,aresample=44100",   # اسلو -> سرعت عادی
    "slow": "aresample=44100,asetrate=35280,aresample=44100",   # اسپید آپ -> سرعت عادی
}


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
    rejected = set(rejected or ())
    items = list(extra or [])
    pieces = list(paths[:3])
    if not pieces:
        return None
    answered = failed = 0

    def collect(futs):
        nonlocal answered, failed
        done, _ = wait(list(futs), timeout=60)
        for f in done:
            kind, path, idx = futs[f]
            try:
                got, ok = f.result()
            except Exception as e:
                log.warning("%s crashed: %s", kind, e)
                failed += 1
                continue
            if kind != "lyrics":   # متن ترانه جزو «موتور جواب‌دهنده» حساب نمی‌شه
                answered, failed = answered + (1 if ok else 0), failed + (0 if ok else 1)
            items.extend(_tag(got, path, idx) if kind != "lyrics" else got)

    for i, p in enumerate(pieces):
        futs = {_POOL.submit(_acr, p): ("acr", p, i), _POOL.submit(_shazam, p): ("shazam", p, i)}
        if i == 0 and _cfg["words"] and WHISPER_MODEL:
            futs[_POOL.submit(_lyrics_vote, pieces)] = ("lyrics", p, i)
        collect(futs)
        groups, second = _decide(items, rejected)
        log.info("round %d: %s", i + 1, [(g["w"], g["items"][0]["title"], sorted(g["srcs"])) for g in groups[:3]])
        if groups and groups[0]["w"] >= EARLY_W and groups[0]["w"] - second >= EARLY_GAP:
            return _to_song(groups)

    groups, second = _decide(items, rejected)
    if groups and groups[0]["w"] >= GUESS_MIN_W:
        return _to_song(groups)

    if LAST_RESORT:
        futs = {_POOL.submit(_try_variant, pieces[0], k, rejected): k for k in _VARIANTS}
        done, _ = wait(list(futs), timeout=60)
        for f in done:
            try:
                got, ok = f.result()
            except Exception:
                failed += 1
                continue
            answered, failed = answered + (1 if ok else 0), failed + (0 if ok else 1)
            items.extend(_tag(got, pieces[0], 0))
        groups, second = _decide(items, rejected)
        if groups and groups[0]["w"] >= GUESS_MIN_W:
            log.info("found by the last resort: %s", groups[0]["items"][0]["title"])
            return _to_song(groups)

    if answered == 0 and failed > 0:
        raise NoAccounts("no engine answered")
    return None
