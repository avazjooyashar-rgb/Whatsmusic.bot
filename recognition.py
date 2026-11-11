"""
recognition.py - music recognition core (the ONLY file that knows how a song is recognized).

  1) own fingerprint DB  (free, fast, knows remixes / special versions)
  2) ACRCloud + Shazam   every piece goes to BOTH engines at the same time, the votes are combined
       - ACR accounts are tried in PRIORITY order, an exhausted account is paused and the next one is used
       - Shazam has a circuit breaker: after a few failures in a row it is skipped for a while, so a slow or
         blocked Shazam never holds the answer back
  3) nothing confirmed -> lyrics (what the singer says) as one more vote, then the loudest pieces again, cleaned
     (noise filter) and tempo-shifted (sped-up / slowed remixes) into the SAME votes; near-silent pieces are
     never sent; the same audio is answered from a cache
  4) an answer is accepted only when it is CONFIRMED:
       two engines agree, or two different pieces of the audio agree (never one answer of one engine)

  The own DB runs at the same time as the engines (LOCAL_FIRST=0, default). If the DB knows the song, its answer
  is returned at once and the engines are cancelled. LOCAL_FIRST=1 -> wait for the DB first (saves quota).

bot.py only uses this small interface:

    init(load_accounts, local_fn, words_fn)  once at start. load_accounts() -> [{"id","host","key","secret"}]
                                       local_fn(path) -> {"title","artist"[,"link","yt"]} or None
    ask_local(paths, rejected)         own DB only (free)             -> song dict or None
    ask_acr(paths, rejected)           ACRCloud only (costs quota)    -> song dict or None, raises NoAccounts
    ask_engines(paths, rejected)       ACR + Shazam + second chance   -> song dict or None, raises NoAccounts
    ask_shazam(paths, rejected)        Shazam only                    -> song dict or None
    ask_engines(paths, rejected)       ACRCloud + Shazam voting       -> song dict or None, raises NoAccounts
    recognize(paths, rejected)         DB + ACRCloud + Shazam together
    accounts_status()                  ACR accounts for the admin panel
    shazam_status()                    {"paused_for": seconds, "fails": n}
    NoAccounts                         raised when no engine could answer
    title_key, artist_set, same_song, is_variant, fold, first_artist     small text helpers

A song dict: {"title","artist","link","yt","dur","engine","alts"}; "rejected" = set of title_key()s.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests

log = logging.getLogger("recognition")

# --- config
MAX_PIECES = int(os.environ.get("MAX_SAMPLES", "10"))
WAVES = (3, 3, 4)                                             # pieces sent per wave
ACR_TIMEOUT = int(os.environ.get("ACR_TIMEOUT") or os.environ.get("ENGINE_TIMEOUT") or 15)        # one request
WAVE_TIMEOUT = int(os.environ.get("WAVE_TIMEOUT", "12"))      # one wave
ACR_THREADS = int(os.environ.get("ACR_THREADS") or os.environ.get("ENGINE_THREADS") or 16)
LOCAL_TIMEOUT = int(os.environ.get("LOCAL_TIMEOUT", "8"))
LOCAL_MIN_AGREE = int(os.environ.get("LOCAL_MIN_AGREE", "2"))  # own DB: pieces that must agree
LYRICS_ENGINE = os.environ.get("LYRICS_ENGINE", "1") == "1"    # last resort: what the singer says (needs faster-whisper)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base")
WHISPER_WAIT = int(os.environ.get("WHISPER_WAIT", "25"))
HUM_MIN_SCORE = int(os.environ.get("HUM_MIN_SCORE", "30"))      # ACR humming guesses below this are ignored
STRICT = os.environ.get("STRICT", "1") == "1"                  # unconfirmed answer -> None
LOCAL_FIRST = os.environ.get("LOCAL_FIRST", "0") == "1"

SHAZAM_TIMEOUT = int(os.environ.get("SHAZAM_TIMEOUT", "10"))   # one Shazam request
SHAZAM_MAX_FAILS = int(os.environ.get("SHAZAM_MAX_FAILS", "3"))
SHAZAM_PAUSE = int(os.environ.get("SHAZAM_PAUSE", "300"))
SHAZAM_THREADS = int(os.environ.get("SHAZAM_THREADS", "3"))    # parallel Shazam requests

# which engines vote (ENGINES from .env may also contain names this file does not know, e.g. audd -> ignored)
_ENG = tuple(e.strip() for e in os.environ.get("ENGINES", "acr,shazam").split(",")
             if e.strip() in ("acr", "shazam")) or ("acr", "shazam")
ENGINES = _ENG

SECOND_CHANCE = os.environ.get("SECOND_CHANCE", "1") == "1"   # nothing confirmed -> retry with filtered audio
SILENT_DB = float(os.environ.get("SILENT_DB", "-48"))          # pieces quieter than this are not sent
CACHE_TTL = int(os.environ.get("RECOG_CACHE_TTL", str(3 * 86400)))
CACHE_MAX = int(os.environ.get("RECOG_CACHE_MAX", "2000"))
FFMPEG = shutil.which("ffmpeg")

QUOTA_CODES = {3003}                 # limit exceeded
AUTH_CODES = {3001, 3014}            # bad key / signature
NO_MATCH_CODES = {1001, 2004}        # "nothing found": a real answer, other accounts would say the same
QUOTA_RETRY = int(os.environ.get("ACR_QUOTA_RETRY", "600"))    # probe an exhausted account again after 10 min
AUTH_PAUSE = 6 * 3600
ERROR_PAUSE = 60                     # after 3 network errors in a row
ERROR_LIMIT = 3


class NoAccounts(Exception):
    """No engine could answer (ACR accounts all paused / unreachable and Shazam failed)."""


# --- song matching
_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_BAD = re.compile(r"remix|mash\s?-?up|medley|nightcore|slowed|sped\s?up|speed\s?up|reverb|karaoke|"
                  r"8d\s?audio|\bcover\b|[\(\[\-]\s*live\b|ریمیکس|ریمیک|مشاپ|کاور", re.I)


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def title_key(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title or "")
    t = re.split(r"\s+[-–—]\s+", t)[0]
    return re.sub(r"[\W_]+", "", _fold(t))


def artist_set(artist: str) -> set:
    return {k for k in (re.sub(r"[\W_]+", "", _fold(p)) for p in _SPLIT.split(artist or "")) if k}


fold = _fold


def first_artist(artist: str) -> str:
    parts = [p.strip() for p in _SPLIT.split(artist or "") if p and p.strip()]
    return parts[0] if parts else (artist or "").strip()


def is_variant(song: dict) -> bool:
    return bool(_BAD.search(song.get("title") or ""))


def same_song(a: dict, b: dict) -> bool:
    ta, tb = title_key(a.get("title")), title_key(b.get("title"))
    if not ta or not tb or is_variant(a) != is_variant(b):
        return False
    if ta != tb:
        short, long_ = sorted((ta, tb), key=len)
        if not (len(short) >= 6 and long_.startswith(short)):
            return False
    sa, sb = artist_set(a.get("artist")), artist_set(b.get("artist"))
    if not sa or not sb or sa & sb:
        return True
    return any(x in y or y in x for x in sa for y in sb if len(x) >= 4 and len(y) >= 4)


# --- own DB
_local_fn = None
_LOCAL_POOL = ThreadPoolExecutor(max_workers=int(os.environ.get("LOCAL_THREADS", "4")),
                                 thread_name_prefix="local")
_MAIN_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="recog")   # runs DB and engines side by side


def _local_one(path: str):
    try:
        r = _local_fn(path)
    except Exception as e:
        log.warning("local DB failed: %s", e)
        return None
    if r and r.get("title"):
        return {"title": r["title"], "artist": r.get("artist") or "", "link": r.get("link"),
                "yt": r.get("yt"), "dur": r.get("dur") or r.get("duration"), "engine": "local",
                "track_id": r.get("track_id")}
    return None


def _ask_local(paths: list, rejected: set):
    """Own DB on up to 3 pieces in parallel. Accepted when LOCAL_MIN_AGREE pieces name the same song."""
    if not _local_fn:
        return None
    use = paths[:3]
    done, _ = wait([_LOCAL_POOL.submit(_local_one, p) for p in use], timeout=LOCAL_TIMEOUT)
    found = [s for s in (f.result() for f in done) if s and title_key(s["title"]) not in rejected]
    need = min(LOCAL_MIN_AGREE, len(use))
    for s in found:
        if sum(same_song(s, g) for g in found) >= need:
            s["alts"] = []
            return s
    return None


# --- ACRCloud accounts
class AccountPool:
    """Priority list. available() always returns the accounts in priority order, minus the paused ones, so an
    account whose pause is over is automatically the first choice again."""

    def __init__(self):
        self._loader = None
        self._cache = (0.0, [])
        self._until = {}      # account id -> paused until
        self._fails = {}      # account id -> network errors in a row
        self._lock = threading.Lock()

    def set_loader(self, loader):
        self._loader = loader
        self._cache = (0.0, [])

    def _accounts(self) -> list:
        ts, accs = self._cache
        if time.time() - ts > 30:                      # accounts added in the DB are picked up within 30 s
            accs = sorted(self._loader(), key=lambda a: a["id"])
            self._cache = (time.time(), accs)
        return accs

    def available(self) -> list:
        now = time.time()
        with self._lock:
            return [a for a in self._accounts() if self._until.get(a["id"], 0) <= now]

    def pause(self, acc: dict, seconds: int, why: str):
        with self._lock:
            self._until[acc["id"]] = time.time() + seconds
        log.warning("ACR account #%s paused %d s: %s", acc["id"], seconds, why)

    def ok(self, acc: dict):
        with self._lock:
            self._fails[acc["id"]] = 0
            self._until.pop(acc["id"], None)

    def fail(self, acc: dict, why: str):
        with self._lock:
            n = self._fails[acc["id"]] = self._fails.get(acc["id"], 0) + 1
            if n >= ERROR_LIMIT:
                self._fails[acc["id"]] = 0
        if n >= ERROR_LIMIT:
            self.pause(acc, ERROR_PAUSE, why)

    def status(self) -> list:
        now = time.time()
        with self._lock:
            return [{"id": a["id"], "host": a["host"],
                     "paused_for": max(0, int(self._until.get(a["id"], 0) - now))}
                    for a in self._accounts()]


POOL = AccountPool()
_ACR_EXEC = ThreadPoolExecutor(max_workers=ACR_THREADS, thread_name_prefix="acr")
_SH_EXEC = ThreadPoolExecutor(max_workers=max(SHAZAM_THREADS, 1), thread_name_prefix="shazam")
_HTTP = requests.Session()
_HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=ACR_THREADS * 2))


def _acr_post(path: str, acc: dict):
    ts = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
    sig = base64.b64encode(hmac.new(acc["secret"].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    with open(path, "rb") as f:
        data = f.read()
    r = _HTTP.post(f"https://{acc['host']}/v1/identify",
                   data={"access_key": acc["key"], "data_type": "audio", "signature_version": "1",
                         "signature": sig, "sample_bytes": len(data), "timestamp": ts},
                   files={"sample": data}, timeout=ACR_TIMEOUT)
    j = r.json()
    return (j.get("status") or {}).get("code"), (j.get("metadata") or {})


def _hum_score(h: dict) -> float:
    try:
        v = float(h.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 100 if v <= 1 else v


def _parse(md: dict) -> list:
    """ACR answer -> candidates. "w" = vote weight (as in the old bot): ACR's score, 2nd / 3rd candidates count
    half; a humming (melody) guess weighs less and can never confirm a song by itself."""
    cands = []
    for m in (md.get("music") or [])[:3]:
        if not m.get("title"):
            continue
        ext = m.get("external_metadata") or {}
        sp = ((ext.get("spotify") or {}).get("track") or {}).get("id")
        vid = (ext.get("youtube") or {}).get("vid")
        yt = f"https://www.youtube.com/watch?v={vid}" if vid else None
        link = f"https://open.spotify.com/track/{sp}" if sp else yt
        try:
            score = float(m.get("score"))
        except (TypeError, ValueError):
            score = 100.0
        try:
            dur = float(m.get("duration_ms")) / 1000 or None
        except (TypeError, ValueError):
            dur = None
        cands.append({"title": m["title"], "artist": ", ".join(a["name"] for a in (m.get("artists") or [])
                                                               if a.get("name")),
                      "link": link, "yt": yt, "dur": dur, "w": max(0.5, min(1.0, score / 100.0)),
                      "engine": "acr"})
    if cands:
        cands.sort(key=lambda c: (is_variant(c), -c["w"]))     # the original (not remix / live / cover) goes first
        for rank, c in enumerate(cands):
            c["rank"] = rank
            if rank:
                c["w"] *= 0.5
        return cands
    hum = sorted((h for h in (md.get("humming") or []) if h.get("title")), key=_hum_score, reverse=True)[:5]
    good = []
    for h in hum:
        score = _hum_score(h)
        if score < HUM_MIN_SCORE:
            continue
        good.append({"title": h["title"], "artist": ", ".join(a["name"] for a in (h.get("artists") or [])
                                                              if a.get("name")),
                     "link": None, "yt": None, "dur": None, "w": min(0.9, 0.4 + score / 150),
                     "engine": "acr", "hum": True})
    for rank, g in enumerate(good):
        g["rank"] = rank
        if rank:
            g["w"] *= 0.6
    return good


def identify_piece(path: str):
    """ONE piece against ACRCloud, walking through the accounts in priority order.
    -> list of candidates ([] = ACR heard it but knows no song) or None (no account could answer)."""
    for acc in POOL.available():
        try:
            code, md = _acr_post(path, acc)
        except (requests.RequestException, ValueError) as e:
            POOL.fail(acc, f"{type(e).__name__}")
            continue
        if code in QUOTA_CODES:
            POOL.pause(acc, QUOTA_RETRY, f"quota ({code})")
            continue
        if code in AUTH_CODES:
            POOL.pause(acc, AUTH_PAUSE, f"key rejected ({code})")
            continue
        POOL.ok(acc)
        if code == 0:
            return _parse(md)
        if code not in NO_MATCH_CODES:
            log.info("ACR #%s status %s", acc["id"], code)
        return []
    return None


# --- Shazam (shazamio) with a circuit breaker
_SH = {"client": None, "loop": None, "missing": False}
_SH_STATE = {"fails": 0, "until": 0.0}
_SH_LOCK = threading.Lock()
_SH_SEM = threading.Semaphore(max(SHAZAM_THREADS, 1))


def _sh_loop():
    """One persistent background event loop for all Shazam requests."""
    with _SH_LOCK:
        if _SH["loop"] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="shazam-loop", daemon=True).start()
            _SH["loop"] = loop
        return _SH["loop"]


def shazam_paused() -> bool:
    return _SH["missing"] or time.time() < _SH_STATE["until"]


def shazam_status() -> dict:
    return {"paused_for": max(0, int(_SH_STATE["until"] - time.time())), "fails": _SH_STATE["fails"],
            "installed": not _SH["missing"]}


def _sh_ok():
    with _SH_LOCK:
        _SH_STATE["fails"] = 0


def _sh_fail(e: Exception):
    msg = str(e) or type(e).__name__
    low = msg.lower()
    if any(w in low for w in ("ffmpeg", "decode", "could not read", "no audio", "invalid data")):
        log.warning("shazam could not read this piece: %s", msg[:100])   # a bad piece, not an outage
        return
    with _SH_LOCK:
        _SH_STATE["fails"] += 1
        n = _SH_STATE["fails"]
        log.warning("shazam failed (%d in a row): %s %s", n, type(e).__name__, msg[:100])
        if n >= SHAZAM_MAX_FAILS:
            _SH_STATE["fails"] = 0
            _SH_STATE["until"] = time.time() + SHAZAM_PAUSE
            log.warning("shazam paused for %d s (too many failures in a row)", SHAZAM_PAUSE)


def _sh_call(path: str) -> dict:
    from shazamio import Shazam
    if _SH["client"] is None:
        _SH["client"] = Shazam()
    client = _SH["client"]
    fn = getattr(client, "recognize", None) or getattr(client, "recognize_song")

    async def go():
        return await asyncio.wait_for(fn(path), SHAZAM_TIMEOUT)

    return asyncio.run_coroutine_threadsafe(go(), _sh_loop()).result(timeout=SHAZAM_TIMEOUT + 3)


def shazam_piece(path: str):
    """ONE piece against Shazam -> list of candidates ([] = nothing found) or None (could not answer)."""
    if shazam_paused():
        return None
    with _SH_SEM:
        try:
            res = _sh_call(path)
        except ImportError:
            _SH["missing"] = True
            log.warning("shazamio is not installed -> Shazam is off (venv/bin/pip install shazamio)")
            return None
        except Exception as e:
            _sh_fail(e)
            return None
    _sh_ok()
    track = (res or {}).get("track") or {}
    title = track.get("title")
    if not title:
        return []
    return [{"title": title, "artist": track.get("subtitle") or "", "link": None, "yt": None,
             "dur": None, "w": 0.99, "rank": 0, "engine": "shazam"}]


# --- voting (same rules as the old bot)
def _rank(hits: list) -> list:
    """hits: [{"piece": int, "song": {...}}] -> clusters of the same song, best first.
    Votes of different engines add up; a repeated answer of one engine adds half of ITS OWN weight.
    A song is 'solid' when two different engines name it, or two different pieces name it with a real
    (non-humming) match. A melody (humming) guess alone is never solid."""
    clusters = []
    for h in hits:
        for c in clusters:
            if same_song(c["song"], h["song"]):
                c["hits"].append(h)
                break
        else:
            clusters.append({"song": dict(h["song"]), "hits": [h]})
    for c in clusters:
        hs = c["hits"]
        per = {}
        for h in hs:
            per.setdefault(h["song"].get("engine"), []).append(h["song"]["w"])
        score = sum(max(v) + 0.5 * (sum(v) - max(v)) for v in per.values())
        mult = 0.5 if is_variant(c["song"]) else 1.0      # remix / live / cover: lower score and never "confident"
        first = [h for h in hs if not h["song"].get("hum") and h["song"]["rank"] == 0]
        # a 2nd / 3rd candidate counts as a second confirmation only when the same song was also a first answer
        also = [h for h in hs if not h["song"].get("hum") and h["song"]["rank"] > 0] if first else []
        c["engines"] = set(per)
        c["solid"] = len(per) >= 2 or len({h["piece"] for h in first + also}) >= 2
        c["score"] = score * mult
        c["bad"] = mult < 1
        c["first"] = first
        best = max(hs, key=lambda h: h["song"]["w"])
        c["song"] = dict(best["song"])
        c["song"]["artist"] = max((h["song"].get("artist") or "" for h in hs),
                                  key=lambda a: (len(artist_set(a)), len(a)))
        for k in ("link", "yt", "dur"):                    # details from ACR first, then from the other engines
            c["song"][k] = next((h["song"][k] for h in sorted(hs, key=lambda h: h["song"].get("engine") != "acr")
                                 if h["song"].get(k)), None)
    clusters.sort(key=lambda c: c["score"] + (1.0 if c["solid"] else 0.0), reverse=True)
    return clusters


def _confident(ranked: list) -> bool:
    """Accuracy first. The top answer must be confirmed (two engines agree, or two different pieces of the audio
    agree), must not look like a remix / mashup, and must have no close competitor. A single answer from one
    engine on one piece is never enough."""
    if not ranked or ranked[0]["bad"] or not ranked[0]["solid"]:
        return False
    if len(ranked) > 1:
        second = ranked[1]
        if second["solid"] or second["score"] >= ranked[0]["score"] * 0.6:
            return False
    return True


# --- audio helpers: loudness order, filtered variants (second chance)
_VOL = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")
# tempo variants help with sped-up / slowed remixes, the clean one with street / room noise
_VARIANTS = (("clean", "highpass=f=100,lowpass=f=8000,dynaudnorm=f=150"),
             ("fast", "atempo=1.12"),
             ("slow", "atempo=0.89"))


def _mean_db(path: str):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=6)
        m = _VOL.search(r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _order_pieces(paths: list) -> list:
    """Loudest first, near-silent pieces dropped (never fewer than 3): no quota is wasted on silence."""
    if not FFMPEG or len(paths) < 2:
        return list(paths)
    dbs = list(_LOCAL_POOL.map(_mean_db, paths))
    scored = sorted(zip(paths, dbs), key=lambda x: (x[1] is None, -(x[1] if x[1] is not None else 0.0)))
    keep = [p for p, d in scored if d is None or d > SILENT_DB]
    if len(keep) < min(3, len(paths)):
        keep = [p for p, _ in scored][:min(3, len(paths))]
    return keep


def _make_variants(paths: list, tmp: str):
    jobs = []
    for name, flt in _VARIANTS:                       # interleaved: the first wave already mixes sources
        for gi, p in enumerate(paths[:3]):
            jobs.append((gi, p, os.path.join(tmp, f"v{gi}_{name}.wav"), flt))

    def run(j):
        gi, p, out, flt = j
        try:
            subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", p, "-af", flt,
                            "-ac", "1", "-ar", "22050", "-t", "14", out], timeout=20, check=True)
            return gi, out
        except Exception:
            return None

    done = [r for r in _LOCAL_POOL.map(run, jobs) if r]
    return [o for _, o in done], [g for g, _ in done]


# --- result cache (engine answers only: the own DB can change when a song is deleted / re-learned)
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _cache_key(paths: list):
    try:
        h = hashlib.sha1()
        for p in paths[:2]:
            with open(p, "rb") as f:
                h.update(f.read(200000))
        return h.hexdigest()
    except OSError:
        return None


def _cache_get(key, rejected: set):
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        ts, song = item
        if time.time() - ts > CACHE_TTL or (rejected and title_key(song["title"]) in rejected):
            _CACHE.pop(key, None)                    # expired, or the user said "wrong song"
            return None
        _CACHE.move_to_end(key)
        return dict(song)


def _cache_put(key, song: dict):
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), dict(song))
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)


# --- last resort: what the singer says (same as the old bot; needs faster-whisper and the bot's words_fn)
_words_fn = None
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


def _lyrics_identify(paths: list):
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
    return _words_fn(re.findall(r"\w+", best.lower()) or words)


# --- the engines round
def _ask_engines(paths: list, rejected: set, cancel=None, engines=ENGINES, full=False):
    """ACR + Shazam on every piece AT THE SAME TIME, in waves (3, 3, 4 pieces). Votes are counted as answers
    arrive and the round stops the moment the answer is confirmed. With full=True and nothing confirmed:
      2) lyrics (what the singer says) as one more vote
      3) the best pieces again, cleaned (noise filter) and tempo-shifted (sped-up / slowed remixes), added to the
         SAME votes (variants of one piece count as ONE piece, so a wrong match can not confirm itself)
    STRICT=1 (default): at the end the best answer must be 'solid' (confirmed), otherwise "not found".
    `cancel` (threading.Event) stops everything when the own DB already answered."""
    hits, st = [], {"answered": 0, "failed": 0}

    def cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    def run_wave(batch) -> bool:
        """batch: [(path, piece_id)] -> every piece goes to every engine. True = confirmed."""
        futs = {}
        for p, gid in batch:
            if "acr" in engines:
                futs[_ACR_EXEC.submit(identify_piece, p)] = gid
            if "shazam" in engines and not shazam_paused():
                futs[_SH_EXEC.submit(shazam_piece, p)] = gid
        if not futs:
            return False
        pending, deadline, confirmed = set(futs), time.time() + WAVE_TIMEOUT, False
        while pending and time.time() < deadline and not confirmed and not cancelled():
            done, pending = wait(pending, timeout=max(min(deadline - time.time(), 0.3), 0.05),
                                 return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    res = f.result()
                except Exception as e:
                    log.warning("engine piece crashed: %s", e)
                    res = None
                if res is None:
                    st["failed"] += 1
                    continue
                st["answered"] += 1
                hits.extend({"piece": futs[f], "song": s} for s in res if title_key(s["title"]) not in rejected)
            if done and _confident(_rank(hits)):
                confirmed = True
        for f in pending:
            f.cancel()                       # not sent yet -> no quota used
        return confirmed

    pos = 0
    for size in WAVES:
        if cancelled():
            return None
        batch = [(p, i) for i, p in enumerate(paths) if pos <= i < pos + size]
        pos += size
        if not batch:
            break
        if run_wave(batch):
            break
    ranked = _rank(hits)

    if full and not _confident(ranked) and LYRICS_ENGINE and _words_fn and not cancelled():
        try:
            ly = _lyrics_identify(paths)
            if ly and title_key(ly.get("title")) not in rejected:
                hits.append({"piece": 99, "song": {"title": ly["title"], "artist": ly.get("artist") or "",
                                                   "link": ly.get("link"), "yt": None, "dur": None,
                                                   "w": 0.8, "rank": 0, "engine": "lyrics"}})
                ranked = _rank(hits)
        except Exception as e:
            log.warning("lyrics stage failed: %s", e)

    if full and not _confident(ranked) and SECOND_CHANCE and FFMPEG and not cancelled() \
            and ("acr" in engines or "shazam" in engines):
        tmp = tempfile.mkdtemp(prefix="recog_")
        try:
            vp, vg = _make_variants(paths[:2], tmp)
            if vp:
                log.info("second chance: %d filtered variants", len(vp))
            for i in range(0, len(vp), 3):
                if cancelled() or run_wave(list(zip(vp[i:i + 3], vg[i:i + 3]))):
                    break
            ranked = _rank(hits)
        except Exception as e:
            log.warning("variant stage failed: %s", e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if cancelled():
        return None
    if not ranked:
        if not st["answered"] and st["failed"]:
            raise NoAccounts("no engine could answer")
        log.info("recognize: nothing")
        return None
    log.info("recognize: %s", [(c["song"].get("title"), c["song"].get("artist"), round(c["score"], 2), c["solid"],
                               sorted(c["engines"])) for c in ranked[:3]])
    if STRICT and not ranked[0]["solid"]:
        log.info("recognize: best answer is not confirmed, STRICT is on -> not found")
        return None
    drop = ("rank", "w", "hum", "score")
    top = {k: v for k, v in ranked[0]["song"].items() if k not in drop}
    top["engine"] = "+".join(sorted(e for e in ranked[0]["engines"] if e)) or top.get("engine")
    own = [h["piece"] for h in (ranked[0]["first"] or ranked[0]["hits"]) if h["piece"] < len(paths)]
    top["piece_path"] = paths[min(own) if own else 0]
    top["alts"] = [{k: v for k, v in c["song"].items() if k not in drop} for c in ranked[1:4]]
    return top


def _engines_full(paths: list, rejected: set, cancel=None):
    return _ask_engines(paths, rejected, cancel, full=True)


# --- public API
ask_local = _ask_local   # own DB only (free)


def ask_acr(paths: list, rejected: set = frozenset()):
    """ACRCloud only (costs quota)."""
    return _ask_engines(list(paths), set(rejected), engines=("acr",))


def ask_shazam(paths: list, rejected: set = frozenset()):
    """Shazam only."""
    return _ask_engines(list(paths), set(rejected), engines=("shazam",))


def ask_engines(paths: list, rejected: set = frozenset(), cancel=None):
    """ACRCloud + Shazam voting WITHOUT the own DB (costs quota): loudest pieces first, silence dropped; when
    nothing is confirmed: lyrics, then cleaned / tempo-shifted audio. Raises NoAccounts.
    The answer has "engine" ("acr", "shazam", "acr+shazam"...) and "piece_path" (the piece that gave it).
    The same audio asked again is answered from a cache ("wrong song" evicts the rejected answer)."""
    rejected = set(rejected)
    paths = list(paths)[:MAX_PIECES]
    key = _cache_key(paths)
    if key:
        hit = _cache_get(key, rejected)
        if hit:
            log.info("recognize: cache hit %s", hit["title"])
            return hit
    ordered = _order_pieces(paths)
    res = _engines_full(ordered, rejected, cancel)
    if res and key:
        _cache_put(key, res)
    return res


def init(load_accounts, local_fn=None, words_fn=None):
    """load_accounts() -> [{"id","host","key","secret"}]; local_fn(path) -> {"title","artist",...} or None;
    words_fn(words) -> {"title","artist"} or None finds a song from a few words of its lyrics (the lyrics stage)."""
    global _local_fn, _words_fn
    POOL.set_loader(load_accounts)
    _local_fn = local_fn
    _words_fn = words_fn


def recognize(paths, rejected=frozenset()):
    """paths: audio pieces. rejected: title_key()s the user already marked as wrong.
    Own DB + ACRCloud + Shazam. The DB runs at the same time as the engines: if it knows the song first, that
    answer is returned at once and the engines are cancelled (LOCAL_FIRST=1: DB alone first)."""
    paths = [paths] if isinstance(paths, str) else list(paths)[:MAX_PIECES]
    if not paths:
        return None
    rejected = set(rejected)
    if LOCAL_FIRST or not _local_fn:
        return ask_local(paths, rejected) or ask_engines(paths, rejected)
    cancel = threading.Event()
    local_fut = _MAIN_POOL.submit(ask_local, paths, rejected)
    eng_fut = _MAIN_POOL.submit(ask_engines, paths, rejected, cancel)
    try:
        res = local_fut.result()
    except Exception as e:
        log.warning("local DB crashed: %s", e)
        res = None
    if res:
        cancel.set()
        return res
    return eng_fut.result()          # may raise NoAccounts


def accounts_status() -> list:
    return POOL.status()
