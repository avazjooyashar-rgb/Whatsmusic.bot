"""
recognition.py - music recognition core (the ONLY file that knows how a song is recognized).  v11

  1) own fingerprint DB  (free, fast, knows remixes / special versions)
  2) ACRCloud + Shazam (+ AudD when AUDD_TOKEN is set) - every piece goes to ALL engines at the same time
       - ONE event-driven round: the next wave of pieces starts as soon as the fast engines (ACR / AudD) of the
         previous wave have answered (or after WAVE_STAGGER s) - a slow Shazam never holds anything back
       - ACR accounts are tried in PRIORITY order; an exhausted account is paused (backoff) and the next one is
         used at once; a paused account comes back through ONE probe request; pauses survive a restart
       - Shazam runs in ONE event loop (concurrent requests inside it) with a half-open circuit breaker
  3) not confirmed after the first seconds -> the best piece is also tried as: denoised, speed-corrected
     and tempo-shifted variants. The own DB is asked with ALL of them too (free). Lyrics run in parallel.
  4) an answer is accepted when it is CONFIRMED (two engines agree / two pieces agree / one engine extremely
     sure / ACR repeats itself / melody matches), or SOFT_ACCEPT, or GUESS_MODE (returned with guess=True).

  v11 changes (why the bot stayed silent while other bots answered):
    * Shazam was only given SHAZAM_GRACE (3 s) as soon as ACR had nothing to say - e.g. when the ACR quota is
      used up or ACR does not know the song. Shazam needs 3-8 s, so its answer arrived AFTER the round gave
      up. Now the short grace applies only when ACR already gave an answer to fall back on; if there is
      nothing yet, Shazam is waited for until the round timeout.
    * ROUND_TIMEOUT 15 -> 20 s, SHAZAM_TIMEOUT 10 -> 12 s.
    * The Shazam circuit breaker was too nervous (4 timeouts = 2 min pause for everybody): now 6 fails = 45 s.
    * "No engine could answer" (-> the generic error text) is only raised when really NO engine answered; when
      Shazam answered "not found" while ACR is down, the user gets "not found".
    * Every engine answer is logged (engine, piece, seconds, titles): journalctl shows exactly who said what.
    * Queued Shazam pieces are dropped when the round is already over (no useless requests).

bot.py uses this interface (unchanged):

    init(load_accounts, local_fn, words_fn)
    ask_local(paths, rejected)          own DB only (free)             -> song dict or None
    ask_local_full(paths, rejected)     own DB only                    -> (song or None, [weak vote])
    ask_acr(paths, rejected)            ACRCloud only (costs quota)
    ask_shazam(paths, rejected)         Shazam only
    ask_engines(paths, rejected, cancel=None, user_id=None, extra=None)  all engines voting
    recognize(paths, rejected, user_id=None)                 DB + engines together
    accounts_status() / shazam_status() / stats()
    NoAccounts / RateLimited
    title_key, artist_set, same_song, is_variant, fold, first_artist, reject_key, is_rejected   text helpers

A song dict: {"title","artist","link","yt","dur","engine","alts","piece_path"[,"guess"]}.
Self test (offline):  python recognition.py --selftest
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests

log = logging.getLogger("recognition")


# --- config (a bad value in .env never crashes the import: it is logged and the default is used)
def _env(cast, default, *names):
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip() != "":
            try:
                return cast(v.strip())
            except (TypeError, ValueError):
                log.warning("bad value for %s=%r -> using %r", n, v, default)
    return default


def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


MAX_PIECES = _env(int, 10, "MAX_SAMPLES")
WAVES = (3, 3, 4)                                               # pieces sent per wave
ACR_TIMEOUT = _env(int, 10, "ACR_TIMEOUT", "ENGINE_TIMEOUT")    # one request
WAVE_TIMEOUT = _env(int, 12, "WAVE_TIMEOUT")                    # one ACR piece, counted from when it STARTS
QUEUE_TIMEOUT = _env(int, 30, "QUEUE_TIMEOUT")                  # a piece that never got a free thread
ROUND_TIMEOUT = _env(int, 20, "ROUND_TIMEOUT")                  # the whole first round (was 15)
PHASE_B_TIMEOUT = _env(int, 25, "PHASE_B_TIMEOUT")              # variants + DB variants + lyrics
WAVE_STAGGER = _env(float, 2.0, "WAVE_STAGGER")                 # next wave starts after this even if ACR is slow
GIVEUP_AFTER = _env(int, 4, "GIVEUP_AFTER")                     # ACR heard this many pieces and knows nothing -> no more plain pieces
SHAZAM_GRACE = _env(float, 3.0, "SHAZAM_GRACE")                 # only Shazam left AND ACR gave an answer: wait this long
SOFT_GRACE = _env(float, 1.0, "SOFT_GRACE")                     # ... but only this long when ACR already gave a decent answer
VARIANT_EARLY = _env(float, 1.2, "VARIANT_EARLY")               # filtered variants are prepared after this many seconds
ACR_THREADS = _env(int, 32, "ACR_THREADS", "ENGINE_THREADS")
ACR_BUDGET = _env(int, 14, "ACR_BUDGET")                        # max ACR pieces (= quota) per request
LOCAL_TIMEOUT = _env(int, 8, "LOCAL_TIMEOUT")
LOCAL_MIN_AGREE = _env(int, 2, "LOCAL_MIN_AGREE")               # own DB: pieces that must agree
LOCAL_SOLO_SCORE = _env(float, 40.0, "LOCAL_SOLO_SCORE")        # own DB: one piece is enough above this score
LOCAL_WEAK_W = _env(float, 0.7, "LOCAL_WEAK_W")                 # vote weight of a weak DB answer
LYRICS_ENGINE = _flag("LYRICS_ENGINE", True)                    # needs faster-whisper (silently skipped without it)
LYRICS_EARLY = _env(float, 4.0, "LYRICS_EARLY")                 # lyrics start in parallel after this many seconds (if no decent answer yet)
LYRICS_TIMEOUT = _env(int, 22, "LYRICS_TIMEOUT")
LYRICS_PIECES = _env(int, 2, "LYRICS_PIECES")                   # the loudest N pieces are transcribed
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")       # "small" is much better for singing / Persian
WHISPER_LANG = os.environ.get("WHISPER_LANG", "").strip() or None   # e.g. fa / en; empty = auto detect
WHISPER_WAIT = _env(int, 25, "WHISPER_WAIT")
WHISPER_THREADS = _env(int, 4, "WHISPER_THREADS")
WHISPER_PRELOAD = _flag("WHISPER_PRELOAD", True)                # load the model at start, not on the first request
HUM_MIN_SCORE = _env(int, 30, "HUM_MIN_SCORE")                  # ACR humming guesses below this are ignored
STRICT = _flag("STRICT", True)                                  # unconfirmed answer -> None (unless SOFT_ACCEPT / GUESS_MODE)
SOFT_ACCEPT = _flag("SOFT_ACCEPT", True)                        # an unconfirmed but decent answer is still returned
SOFT_W = _env(float, 0.75, "SOFT_W")                            # normal ACR / Shazam match: weight needed (= score / 100)
SOFT_HUM_W = _env(float, 0.80, "SOFT_HUM_W")                    # melody-only guess: weight needed
GUESS_MODE = _flag("GUESS_MODE", True)                          # nothing confirmed: still return the best plausible candidate
GUESS_MIN_W = _env(float, 0.5, "GUESS_MIN_W")                   # normal ACR / Shazam / lyrics answer: weight needed
GUESS_HUM_W = _env(float, 0.7, "GUESS_HUM_W")                   # melody-only answer (ACR humming score ~45+)
GUESS_LOCAL_SCORE = _env(float, 20.0, "GUESS_LOCAL_SCORE")      # own DB: aligned-hash score needed
SOLO_ACCEPT = _flag("SOLO_ACCEPT", True)                        # 1-piece clip: one very sure engine is enough
SOLO_W = _env(float, 0.95, "SOLO_W")
SINGLE_STRONG = _flag("SINGLE_STRONG", True)                    # one extremely sure answer is enough (no rival)
SINGLE_W = _env(float, 0.98, "SINGLE_W")
LOCAL_FIRST = _flag("LOCAL_FIRST", False)

# ACR confirmation by repetition (same song named again and again by ACR on the clip and its variants)
REPEAT_MIN_HITS = _env(int, 2, "REPEAT_MIN_HITS")               # normal ACR matches needed ...
REPEAT_MIN_W = _env(float, 0.6, "REPEAT_MIN_W")                 # ... and the best one must reach this weight
HUM_PIECES_MIN = _env(int, 2, "HUM_PIECES_MIN")                 # melody matches: this many DIFFERENT pieces ...
HUM_PIECES_W = _env(float, 0.75, "HUM_PIECES_W")                # ... each with at least this weight
HUM_HITS_MIN = _env(int, 3, "HUM_HITS_MIN")                     # or this many melody matches (any piece) ...
HUM_HITS_W = _env(float, 0.85, "HUM_HITS_W")                    # ... each with at least this weight

SHAZAM_TIMEOUT = _env(int, 12, "SHAZAM_TIMEOUT")                # was 10
SHAZAM_MAX_FAILS = _env(int, 6, "SHAZAM_MAX_FAILS")             # was 4
SHAZAM_PAUSE = _env(int, 45, "SHAZAM_PAUSE")                    # was 120
SHAZAM_THREADS = max(_env(int, 4, "SHAZAM_THREADS"), 1)         # concurrent Shazam requests (all in ONE event loop)

AUDD_TOKEN = os.environ.get("AUDD_TOKEN", "").strip()          # optional third engine (add "audd" to ENGINES)
AUDD_TIMEOUT = _env(int, 12, "AUDD_TIMEOUT")


def _parse_engines(s) -> tuple:
    e = tuple(x.strip() for x in (s or "").split(",") if x.strip() in ("acr", "shazam", "audd"))
    return e or ("acr", "shazam")        # names this file does not know are ignored


ENGINES = _parse_engines(os.environ.get("ENGINES", "acr,shazam"))
FAST_ENGINES = ("acr", "audd")           # a wave is "done" when these answered; Shazam is slower and only helps

SECOND_CHANCE = _flag("SECOND_CHANCE", True)                    # nothing confirmed -> try filtered variants
SECOND_PIECES = _env(int, 1, "SECOND_CHANCE_PIECES")            # how many pieces get filtered variants
VARIANT_ENGINE_N = _env(int, 4, "VARIANT_ENGINE_PIECES")        # variants sent to the engines (the DB gets all)
SILENT_DB = _env(float, -48.0, "SILENT_DB")                     # pieces quieter than this are not sent
CACHE_TTL = _env(int, 3 * 86400, "RECOG_CACHE_TTL")
NEG_TTL = _env(int, 300, "RECOG_NEG_TTL")                       # "not found" is remembered this long
CACHE_MAX = _env(int, 2000, "RECOG_CACHE_MAX")
FFMPEG = shutil.which("ffmpeg")

MAX_ROUNDS = _env(int, 12, "MAX_ROUNDS")                        # engine rounds at the same time
ROUND_WAIT = _env(int, 30, "ROUND_WAIT")
USER_LIMIT = _env(int, 6, "USER_LIMIT")                         # only when user_id is passed; 0 = off
USER_WINDOW = _env(int, 60, "USER_WINDOW")

QUOTA_CODES = {3003} | set(_env(lambda s: {int(x) for x in s.split(",") if x.strip()}, set(), "ACR_QUOTA_CODES"))
AUTH_CODES = {3001, 3014}            # bad key / signature
NO_MATCH_CODES = {1001, 2004}        # "nothing found": a real answer, other accounts would say the same
QUOTA_RETRY = _env(int, 600, "ACR_QUOTA_RETRY")                 # first probe of an exhausted account (10 min)
QUOTA_MAX = _env(int, 1800, "ACR_QUOTA_MAX")                    # the pause doubles up to this
RESET_HOUR = _env(float, None, "ACR_RESET_UTC_HOUR")            # optional: daily quota reset hour (UTC)
BALANCE = _flag("ACR_BALANCE", False)                           # rotate accounts instead of strict priority
STATE_FILE = os.environ.get("ACR_STATE_FILE",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".acr_state.json"))
# per-account counters of the admin panel (accounts.uses / accounts.errors in the bot's SQLite DB)
STATS_DB = os.environ.get("ACCOUNT_STATS_DB") or os.environ.get("DB_PATH") or "bot.db"
AUTH_PAUSE = 6 * 3600
ERROR_PAUSE = 60                     # after 3 network errors in a row (1 when the account is being probed)
ERROR_LIMIT = 3
RATE_PAUSE = 20                      # HTTP 429 / 503
PROBE_WAIT = 6                       # seconds a request waits for the single probe of a paused account

_STAT = Counter()


def _count(name: str, n: int = 1):
    _STAT[name] += n


def _emit(acc: dict, kind: str):
    """kind 'use' = one request sent with this ACR account, 'error' = it failed / was refused.
    Written to accounts.uses / accounts.errors (the columns the old bot filled); never raises."""
    col = "uses" if kind == "use" else "errors"
    try:
        c = sqlite3.connect(STATS_DB, timeout=5)
        try:
            c.execute(f"UPDATE accounts SET {col}=COALESCE({col},0)+1 WHERE id=?", (acc["id"],))
            c.commit()
        finally:
            c.close()
    except Exception as e:
        log.debug("account counter not updated: %s", e)


class NoAccounts(Exception):
    """No engine could answer (ACR accounts all paused / unreachable and Shazam failed, or the server is busy)."""


class RateLimited(Exception):
    """Only raised when the caller passes user_id and the user is over USER_LIMIT per USER_WINDOW."""

    def __init__(self, retry_after: int = 0):
        super().__init__(f"rate limited, retry in {retry_after}s")
        self.retry_after = retry_after


# --- song matching
_SPLIT = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with|x|and|و)\s+|\s*[,&;/+،]\s*", re.I)
_BAD = re.compile(r"remix|mash\s?-?up|medley|nightcore|slowed|sped\s?up|speed\s?up|reverb|karaoke|"
                  r"8d\s?audio|\bcover\b|[\(\[\-]\s*live\b|ریمیکس|ریمیک|مشاپ|کاور", re.I)


def _fold(s: str) -> str:
    s = (s or "").lower().replace("ي", "ی").replace("ك", "ک")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _key(s: str) -> str:
    return re.sub(r"[\W_]+", "", _fold(s))


fold = _fold

# names that contain a separator and must never be split ("AC/DC", "Florence and the Machine"...)
_NOSPLIT = {"acdc", "florenceandthemachine", "earthwindandfire", "earthwindfire", "simonandgarfunkel",
            "hallandoates", "mumfordandsons", "kooldanthegang", "tinaturnerandike"}
_NOSPLIT |= {_key(x) for x in os.environ.get("NOSPLIT_ARTISTS", "").split(",") if x.strip()}


def title_key(title: str) -> str:
    t = title or ""
    stripped = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", t)
    stripped = re.split(r"\s+[-–—]\s+", stripped)[0]
    return _key(stripped) or _key(t)          # a title that is ONLY "(Intro)" keeps its words


def artist_set(artist: str) -> set:
    """Keys of every artist in the string PLUS the key of the whole string, so a name containing 'and' / '/'
    still matches itself and its parts match a feat. credit."""
    out = {k for k in (_key(p) for p in _SPLIT.split(artist or "")) if k}
    full = _key(artist)
    if full:
        out.add(full)
    return out


def first_artist(artist: str) -> str:
    a = (artist or "").strip()
    if _key(a) in _NOSPLIT:
        return a
    parts = [p.strip() for p in _SPLIT.split(a) if p and p.strip()]
    return parts[0] if parts else a


def is_variant(song: dict) -> bool:
    return bool(_BAD.search(song.get("title") or ""))


def same_song(a: dict, b: dict) -> bool:
    ta, tb = title_key(a.get("title")), title_key(b.get("title"))
    if not ta or not tb or is_variant(a) != is_variant(b):
        return False
    if ta != tb:
        short, long_ = sorted((ta, tb), key=len)
        if not (len(short) >= 8 and long_.startswith(short) and len(long_) - len(short) <= len(short)):
            return False
    sa, sb = artist_set(a.get("artist")), artist_set(b.get("artist"))
    if not sa or not sb:                         # an artist is missing: only a long, identical title is trusted
        return ta == tb and len(ta) >= 7
    if sa & sb:
        return True
    return any(x in y or y in x for x in sa for y in sb if len(x) >= 5 and len(y) >= 5)


def reject_key(song: dict) -> str:
    """Strict 'wrong song' key: this title by this artist (other artists' songs of the same name stay allowed)."""
    return f"{title_key(song.get('title'))}::{_key(first_artist(song.get('artist')))}"


def is_rejected(song: dict, rejected) -> bool:
    if not rejected:
        return False
    return title_key(song.get("title")) in rejected or reject_key(song) in rejected


# --- own DB
_local_fn = None
_LOCAL_POOL = ThreadPoolExecutor(max_workers=_env(int, 8, "LOCAL_THREADS"), thread_name_prefix="local")
_AUDIO_POOL = ThreadPoolExecutor(max_workers=_env(int, 6, "AUDIO_THREADS"), thread_name_prefix="audio")  # ffmpeg
_VAR_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="variants")
_MAIN_POOL = ThreadPoolExecutor(max_workers=_env(int, 32, "MAIN_THREADS"), thread_name_prefix="recog")
_LYR_EXEC = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lyrics")


def _local_one(path: str):
    try:
        r = _local_fn(path)
    except Exception as e:
        log.warning("local DB failed: %s", e)
        return None
    if r and r.get("title"):
        try:
            score = float(r["score"]) if r.get("score") is not None else None
        except (TypeError, ValueError):
            score = None
        return {"title": r["title"], "artist": r.get("artist") or "", "link": r.get("link"),
                "yt": r.get("yt"), "dur": r.get("dur") or r.get("duration"), "engine": "local",
                "track_id": r.get("track_id"), "score": score, "piece_path": path}
    return None


def _local_scan(paths: list, rejected: set):
    """Own DB on up to 3 pieces in parallel -> (strong, weak).
    strong: LOCAL_MIN_AGREE pieces name the same song, or the DB score is high (>= LOCAL_SOLO_SCORE), or the DB
            gives no score at all and only one piece exists -> answered at once.
    weak:   anything else the DB found -> goes into the engine vote as one more (lower weight) voter."""
    if not _local_fn or not paths:
        return None, None
    use = paths[:3]
    done, _ = wait([_LOCAL_POOL.submit(_local_one, p) for p in use], timeout=LOCAL_TIMEOUT)
    found = [s for s in (f.result() for f in done) if s and not is_rejected(s, rejected)]
    if not found:
        return None, None
    need = min(LOCAL_MIN_AGREE, len(use))
    best, best_key = None, None
    for s in found:
        group = [g for g in found if same_song(s, g)]
        scores = [g["score"] for g in group if g["score"] is not None]
        top_score = max(scores) if scores else None
        strong = (len(group) >= max(need, 2) if len(use) > 1 else False) \
            or (top_score is not None and top_score >= LOCAL_SOLO_SCORE) \
            or (top_score is None and len(group) >= need)
        rank_key = (strong, len(group), top_score or 0)
        if best_key is None or rank_key > best_key:
            best, best_key = s, rank_key
    if best_key[0]:
        out = {k: v for k, v in best.items() if k != "score"}
        out["alts"] = []
        out["score"] = best.get("score")
        _count("local_strong")
        return out, None
    weak = dict(best)
    weak.update({"w": LOCAL_WEAK_W, "rank": 0})
    _count("local_weak")
    return None, weak


# --- ACRCloud accounts
class AccountPool:
    """Priority list. available() returns the accounts in priority order, minus the paused ones. An account whose
    pause is over is "half open": ONE request probes it, everybody else keeps using the other accounts until the
    probe says it works. Quota pauses double (backoff) up to QUOTA_MAX. Pauses are saved to STATE_FILE."""

    def __init__(self):
        self._loader = None
        self._cache = (0.0, [])
        self._until = {}        # account id -> paused until
        self._fails = {}        # account id -> network errors in a row
        self._quota_hits = {}   # account id -> quota pauses in a row
        self._quota_paused = set()
        self._half = set()      # pause expired, waiting for a successful probe
        self._probing = {}      # account id -> time the probe started
        self._rr = 0
        self._reset_day = None
        self._lock = threading.RLock()
        self._load_state()

    # persistence
    def _load_state(self):
        if not STATE_FILE:
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            for i, until in d.get("until", []):
                if until > now:
                    self._until[i] = until
                self._half.add(i)
            self._quota_paused = {i for i in d.get("quota", [])}
            self._quota_hits = {int(k) if str(k).lstrip("-").isdigit() else k: v
                                for k, v in (d.get("hits") or {}).items()}
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self):
        if not STATE_FILE:
            return
        try:
            with self._lock:
                d = {"until": [[i, u] for i, u in self._until.items()], "quota": list(self._quota_paused),
                     "hits": dict(self._quota_hits)}
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, STATE_FILE)
        except OSError as e:
            log.debug("could not save ACR state: %s", e)

    def set_loader(self, loader):
        self._loader = loader
        self._cache = (0.0, [])

    def _accounts(self) -> list:
        ts, accs = self._cache
        if self._loader and time.time() - ts > 30:        # accounts added in the DB are picked up within 30 s
            try:
                accs = sorted(self._loader(), key=lambda a: a["id"])
            except Exception as e:
                log.warning("ACR account loader failed (keeping the old list): %s", e)
            self._cache = (time.time(), accs)
        return accs

    def _maybe_daily_reset(self, now: float):
        if RESET_HOUR is None:
            return
        day = int((now - RESET_HOUR * 3600) // 86400)
        if self._reset_day is None:
            self._reset_day = day
        elif day != self._reset_day:
            self._reset_day = day
            for i in list(self._quota_paused):
                self._until.pop(i, None)
                self._quota_hits.pop(i, None)
            self._quota_paused.clear()
            log.info("ACR quota reset time passed: quota pauses cleared")

    def available(self) -> list:
        now = time.time()
        with self._lock:
            self._maybe_daily_reset(now)
            out = []
            for a in self._accounts():
                i = a["id"]
                if self._until.get(i, 0) > now:
                    continue
                if i in self._half:
                    t = self._probing.get(i)
                    if t and now - t < ACR_TIMEOUT + 5:   # somebody is already probing it
                        continue
                    self._probing[i] = now                # this caller becomes the probe
                out.append(a)
            if BALANCE and len(out) > 1:
                self._rr += 1
                k = self._rr % len(out)
                out = out[k:] + out[:k]
            return out

    def any_probing(self) -> bool:
        now = time.time()
        with self._lock:
            return any(now - t < ACR_TIMEOUT + 5 for t in self._probing.values())

    def pause(self, acc: dict, seconds: int, why: str, quota: bool = False):
        with self._lock:
            i = acc["id"]
            self._until[i] = max(self._until.get(i, 0), time.time() + seconds)
            self._half.add(i)
            self._probing.pop(i, None)
            (self._quota_paused.add if quota else self._quota_paused.discard)(i)
        log.warning("ACR account #%s paused %d s: %s", acc["id"], seconds, why)
        self._save_state()

    def quota(self, acc: dict):
        with self._lock:
            if self._until.get(acc["id"], 0) > time.time():
                return                    # another piece of the same burst already paused it: no extra backoff
            n = self._quota_hits[acc["id"]] = self._quota_hits.get(acc["id"], 0) + 1
        _count("acr_quota")
        _emit(acc, "error")
        self.pause(acc, min(QUOTA_RETRY * 2 ** (n - 1), QUOTA_MAX), "quota exhausted", quota=True)

    def ok(self, acc: dict):
        with self._lock:
            i = acc["id"]
            changed = i in self._until or i in self._half or i in self._quota_hits
            self._fails[i] = 0
            self._until.pop(i, None)
            self._half.discard(i)
            self._probing.pop(i, None)
            self._quota_hits.pop(i, None)
            self._quota_paused.discard(i)
        if changed:
            self._save_state()

    def fail(self, acc: dict, why: str):
        _count("acr_errors")
        _emit(acc, "error")
        with self._lock:
            i = acc["id"]
            n = self._fails[i] = self._fails.get(i, 0) + 1
            trip = n >= (1 if i in self._half else ERROR_LIMIT)      # a failed probe pauses at once
            if trip:
                self._fails[i] = 0
        if trip:
            self.pause(acc, ERROR_PAUSE, why)

    def status(self) -> list:
        now = time.time()
        with self._lock:
            return [{"id": a["id"], "host": a["host"],
                     "paused_for": max(0, int(self._until.get(a["id"], 0) - now)),
                     "quota": a["id"] in self._quota_paused,
                     "probing": a["id"] in self._probing and now - self._probing[a["id"]] < ACR_TIMEOUT + 5}
                    for a in self._accounts()]


POOL = AccountPool()
_ACR_EXEC = ThreadPoolExecutor(max_workers=ACR_THREADS, thread_name_prefix="acr")
_SH_EXEC = ThreadPoolExecutor(max_workers=max(SHAZAM_THREADS * 3, 6), thread_name_prefix="shazam")
_AUDD_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="audd")
_HTTP = requests.Session()
_HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=ACR_THREADS * 2))


def _acr_post(path: str, acc: dict):
    ts = str(int(time.time()))
    to_sign = "\n".join(["POST", "/v1/identify", acc["key"], "audio", "1", ts])
    sig = base64.b64encode(hmac.new(acc["secret"].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    with open(path, "rb") as f:
        data = f.read()
    _count("acr_calls")
    _emit(acc, "use")
    r = _HTTP.post(f"https://{acc['host']}/v1/identify",
                   data={"access_key": acc["key"], "data_type": "audio", "signature_version": "1",
                         "signature": sig, "sample_bytes": len(data), "timestamp": ts},
                   files={"sample": data}, timeout=ACR_TIMEOUT)
    if r.status_code in (429, 503):
        return "RATE", {}
    j = r.json()
    return (j.get("status") or {}).get("code"), (j.get("metadata") or {})


def _hum_score(h: dict) -> float:
    try:
        v = float(h.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return v * 100 if v <= 1 else v


def _parse(md: dict) -> list:
    """ACR answer -> candidates. "w" = vote weight: ACR's score, 2nd / 3rd candidates count half; a humming
    (melody) guess weighs less and is only trusted through the repetition rules of _rank()."""
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


def identify_piece(path: str, stop=None):
    """ONE piece against ACRCloud, walking through the accounts in priority order.
    -> list of candidates ([] = ACR heard it but knows no song) or None (no account could answer).
    `stop` (threading.Event): the round is over, do not spend quota any more."""
    stopped = lambda: stop is not None and stop.is_set()
    accs = POOL.available()
    deadline = time.time() + PROBE_WAIT
    while not accs and POOL.any_probing() and time.time() < deadline and not stopped():
        time.sleep(0.2)                                  # a paused account is being probed: wait for the verdict
        accs = POOL.available()
    for acc in accs:
        if stopped():
            return None
        try:
            code, md = _acr_post(path, acc)
        except (requests.RequestException, ValueError, OSError) as e:
            POOL.fail(acc, type(e).__name__)
            continue
        if code in QUOTA_CODES:
            POOL.quota(acc)
            continue
        if code in AUTH_CODES:
            _emit(acc, "error")
            POOL.pause(acc, AUTH_PAUSE, f"key rejected ({code})")
            continue
        if code == "RATE":
            _emit(acc, "error")
            POOL.pause(acc, RATE_PAUSE, "rate limited (HTTP 429/503)")
            continue
        if code == 0:
            POOL.ok(acc)
            return _parse(md)
        if code in NO_MATCH_CODES:
            POOL.ok(acc)
            return []
        log.info("ACR #%s unknown status %s -> trying the next account", acc["id"], code)
        POOL.fail(acc, f"status {code}")                  # server trouble: NOT "song not found"
    return None


# --- a small circuit breaker (AudD)
class _Breaker:
    def __init__(self, name: str, max_fails: int, pause: int):
        self.name, self.max_fails, self.pause_s = name, max_fails, pause
        self.fails, self.until = 0, 0.0
        self._lock = threading.Lock()

    def paused(self) -> bool:
        return time.time() < self.until

    def ok(self):
        with self._lock:
            self.fails = 0

    def fail(self, why: str, pause: int = 0):
        with self._lock:
            self.fails += 1
            if pause or self.fails >= self.max_fails:
                self.fails = 0
                self.until = time.time() + (pause or self.pause_s)
                log.warning("%s paused %d s: %s", self.name, pause or self.pause_s, why)


_AUDD = _Breaker("audd", 3, 300)


def audd_paused() -> bool:
    return not AUDD_TOKEN or _AUDD.paused()


def audd_piece(path: str, stop=None):
    """ONE piece against AudD (optional third engine) -> list of candidates, [] = nothing, None = could not answer."""
    if audd_paused() or (stop is not None and stop.is_set()):
        return None
    try:
        with open(path, "rb") as f:
            r = _HTTP.post("https://api.audd.io/", data={"api_token": AUDD_TOKEN, "return": "spotify"},
                           files={"file": f}, timeout=AUDD_TIMEOUT)
        j = r.json()
    except (requests.RequestException, ValueError, OSError) as e:
        _AUDD.fail(type(e).__name__)
        return None
    if j.get("status") != "success":
        code = (j.get("error") or {}).get("error_code")
        _AUDD.fail(f"error {code}", pause=3600 if code in (900, 901, 902, 903) else 0)   # token / limit problems
        return None
    _AUDD.ok()
    res = j.get("result")
    if not res or not res.get("title"):
        return []
    sp = (((res.get("spotify") or {}).get("external_urls")) or {}).get("spotify")
    return [{"title": res["title"], "artist": res.get("artist") or "", "link": sp or res.get("song_link"),
             "yt": None, "dur": None, "w": 0.9, "rank": 0, "engine": "audd"}]


# --- Shazam (shazamio): ONE background event loop, concurrent requests inside it, half-open circuit breaker
_SH = {"loop": None, "client": None, "sem": None, "missing": False}
_SH_STATE = {"fails": 0, "until": 0.0, "half": False, "probing": 0.0}
_SH_LOCK = threading.Lock()
_SH_BAD_PIECE = ("ffmpeg", "decode", "could not read", "no audio", "invalid data", "too short", "no such file")


def _sh_loop():
    """The one background event loop of Shazam. Requests run concurrently INSIDE it (the normal way to use
    shazamio); SHAZAM_THREADS limits how many are in flight at once."""
    with _SH_LOCK:
        if _SH["loop"] is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, name="shazam-loop", daemon=True).start()
            _SH["loop"] = loop
        return _SH["loop"]


def shazam_paused() -> bool:
    if _SH["missing"]:
        return True
    now = time.time()
    if now < _SH_STATE["until"]:
        return True
    return _SH_STATE["half"] and now - _SH_STATE["probing"] < SHAZAM_TIMEOUT + 5    # the probe is in flight


def shazam_status() -> dict:
    return {"paused_for": max(0, int(_SH_STATE["until"] - time.time())), "fails": _SH_STATE["fails"],
            "installed": not _SH["missing"], "half_open": _SH_STATE["half"]}


def _sh_ok():
    with _SH_LOCK:
        _SH_STATE.update(fails=0, half=False, probing=0.0)


def _sh_fail(e: Exception, took: float = 0.0):
    msg = str(e) or type(e).__name__
    low = msg.lower()
    with _SH_LOCK:
        _SH_STATE["probing"] = 0.0
        if any(w in low for w in _SH_BAD_PIECE):
            log.warning("shazam could not read this piece: %s", msg[:100])   # a bad piece, not an outage
            return
        _SH_STATE["fails"] += 1
        n = _SH_STATE["fails"]
        _count("shazam_errors")
        log.warning("shazam failed after %.1fs (%d in a row): %s %s", took, n, type(e).__name__, msg[:100])
        if n >= SHAZAM_MAX_FAILS or _SH_STATE["half"]:        # a failed probe pauses again at once
            _SH_STATE["fails"] = 0
            _SH_STATE["half"] = True
            _SH_STATE["until"] = time.time() + SHAZAM_PAUSE
            log.warning("shazam paused for %d s", SHAZAM_PAUSE)


def _sh_call(path: str, stop=None):
    from shazamio import Shazam
    loop = _sh_loop()

    async def go():
        if _SH["sem"] is None:                     # created inside the loop thread: no race
            _SH["sem"] = asyncio.Semaphore(SHAZAM_THREADS)
        if _SH["client"] is None:
            _SH["client"] = Shazam()
        async with _SH["sem"]:
            if stop is not None and stop.is_set():     # the round is over while this piece waited: send nothing
                return None
            cl = _SH["client"]
            fn = getattr(cl, "recognize", None) or getattr(cl, "recognize_song")
            return await asyncio.wait_for(fn(path), SHAZAM_TIMEOUT)

    fut = asyncio.run_coroutine_threadsafe(go(), loop)
    try:
        return fut.result(timeout=SHAZAM_TIMEOUT * 2 + 4)    # a piece may wait for a free slot first
    except BaseException:
        fut.cancel()
        raise


def shazam_piece(path: str, stop=None):
    """ONE piece against Shazam -> list of candidates ([] = nothing found) or None (could not answer)."""
    if _SH["missing"]:
        return None
    with _SH_LOCK:
        now = time.time()
        if now < _SH_STATE["until"]:
            return None
        if _SH_STATE["half"]:
            if now - _SH_STATE["probing"] < SHAZAM_TIMEOUT + 5:
                return None                         # only ONE probe after a pause
            _SH_STATE["probing"] = now
    if stop is not None and stop.is_set():
        return None
    t0 = time.time()
    try:
        res = _sh_call(path, stop)
    except ImportError:
        _SH["missing"] = True
        log.warning("shazamio is not installed -> Shazam is off (venv/bin/pip install shazamio)")
        return None
    except Exception as e:
        _sh_fail(e, time.time() - t0)
        return None
    if res is None:                                 # dropped because the round ended: not an answer, not a failure
        with _SH_LOCK:
            _SH_STATE["probing"] = 0.0
        return None
    _sh_ok()
    track = (res or {}).get("track") or {}
    title = track.get("title")
    if not title:
        return []
    return [{"title": title, "artist": track.get("subtitle") or "", "link": None, "yt": None,
             "dur": None, "w": 0.99, "rank": 0, "engine": "shazam"}]


# --- voting
_PSEUDO = 90        # piece ids from 90 up are not real audio pieces (own DB = 98, lyrics = 99)


def _rank(hits: list, solo: bool = False) -> list:
    """hits: [{"piece": int, "song": {...}}] -> clusters of the same song, best first.
    Votes of different engines add up; a repeated answer of one engine adds half of ITS OWN weight.
    A song is 'solid' when
      - two different engines name it, or
      - two different REAL pieces name it with a real (non-humming) match, or
      - ONE non-humming answer is extremely sure (weight >= SINGLE_W), or the DB is sure (score), or
      - the clip has only one piece and one answer is very sure (solo), or
      - ACR names it at least REPEAT_MIN_HITS times as a normal first answer (the clip and its variants), or
      - ACR melody (humming) matches agree: HUM_PIECES_MIN different pieces (weight >= HUM_PIECES_W) or
        HUM_HITS_MIN matches (weight >= HUM_HITS_W).
    Lyrics count as an engine only when another engine named the song with weight >= 0.8; a single melody
    (humming) guess alone is never solid (see _soft_ok / _guess_pick for the softer rules)."""
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
        engs = set(per)
        other_best = max((max(v) for e, v in per.items() if e != "lyrics"), default=0.0)
        voting = engs - {"lyrics"} if ("lyrics" in engs and other_best < 0.8) else engs
        real_pieces = {h["piece"] for h in first + also if h["piece"] < _PSEUDO}
        sure_one = any(h["song"]["w"] >= (SINGLE_W if SINGLE_STRONG else 2.0) and h["piece"] < _PSEUDO
                       for h in first)
        sure_solo = SOLO_ACCEPT and solo and any(h["song"]["w"] >= SOLO_W and h["piece"] < _PSEUDO for h in first)
        sure_db = any(h["song"].get("sure") for h in hs)
        # ACR named the same song again and again (the clip and its variants count as separate answers here)
        acr_first = [h for h in first if h["song"].get("engine") == "acr"]
        sure_repeat = (len(acr_first) >= REPEAT_MIN_HITS
                       and max(h["song"]["w"] for h in acr_first) >= REPEAT_MIN_W)
        # ACR melody (humming) matches: several different pieces, or several strong matches
        hum_hits = [h for h in hs if h["song"].get("hum") and h["song"]["rank"] == 0 and h["piece"] < _PSEUDO]
        hum_pieces = {h["piece"] for h in hum_hits if h["song"]["w"] >= HUM_PIECES_W}
        hum_strong = [h for h in hum_hits if h["song"]["w"] >= HUM_HITS_W]
        sure_hum = len(hum_pieces) >= HUM_PIECES_MIN or len(hum_strong) >= HUM_HITS_MIN
        c["engines"] = engs
        c["solid"] = (len(voting) >= 2 or len(real_pieces) >= 2 or sure_one or sure_solo or sure_db
                      or sure_repeat or sure_hum)
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


def _rival_free(ranked: list) -> bool:
    """No real competitor for the top answer. A remix / live / cover that came up beside the original is NOT a
    competitor; a rival song blocks only when it is confirmed too (and not far weaker) or nearly as strong."""
    top = ranked[0]
    for other in ranked[1:]:
        if other["bad"] and not other["solid"]:
            continue
        if other["solid"] and top["score"] >= other["score"] * 1.5:
            continue
        if other["solid"] or other["score"] >= top["score"] * 0.75:
            return False
    return True


def _confident(ranked: list) -> bool:
    """The top answer is confirmed, does not look like a remix / mashup, and has no real competitor."""
    if not ranked or ranked[0]["bad"] or not ranked[0]["solid"]:
        return False
    return _rival_free(ranked)


def _soft_ok(ranked: list, allow_hum: bool = True) -> bool:
    """SOFT_ACCEPT: the top answer is not confirmed, but it is good enough to be returned: a decent first
    answer of ACR / Shazam (weight >= SOFT_W), or - when allow_hum - a melody guess (weight >= SOFT_HUM_W),
    from a real piece, no remix / cover, no real rival. Lyrics and a weak DB vote never count."""
    if not SOFT_ACCEPT or not ranked or ranked[0]["bad"] or not _rival_free(ranked):
        return False
    for h in ranked[0]["hits"]:
        s = h["song"]
        if h["piece"] >= _PSEUDO or s.get("rank") != 0:
            continue
        if s.get("hum"):
            if allow_hum and s["w"] >= SOFT_HUM_W:
                return True
        elif s["w"] >= SOFT_W:
            return True
    return False


def _guess_hit_ok(s: dict) -> bool:
    """Is this single answer plausible enough to be offered as a guess?"""
    if s.get("rank", 0) != 0:                       # 2nd / 3rd candidates of an engine are only alternatives
        return False
    if s.get("engine") == "local":                  # own DB: needs a real aligned-hash score (small scores are noise)
        return (s.get("score") or 0) >= GUESS_LOCAL_SCORE
    if s.get("hum"):                                # melody only: ACR humming score ~45 or more
        return s["w"] >= GUESS_HUM_W
    return s["w"] >= GUESS_MIN_W                    # a normal ACR / Shazam match or the lyrics stage


def _guess_pick(ranked: list):
    """GUESS_MODE: nothing is confirmed. -> the best cluster that has at least one plausible answer, or None."""
    if not GUESS_MODE:
        return None
    for c in ranked:
        if any(_guess_hit_ok(h["song"]) for h in c["hits"]):
            return c
    return None


# --- audio helpers: loudness order, filtered variants (second chance)
_VOL = re.compile(r"mean_volume:\s*(-?[\d.]+)\s*dB")


def _resample(f: float) -> str:
    """Plays the audio at speed f by resampling (pitch AND tempo change together, like a sped-up / slowed edit)."""
    return f"aresample=44100,asetrate={int(44100 * f)},aresample=44100"


# in priority order. The first VARIANT_ENGINE_N go to the engines, ALL of them go to the own DB (free):
#   denoise    street / room noise, quiet or muffled audio
#   undo_fast  a sped-up edit (x1.25 / nightcore)  -> played back slower
#   undo_slow  a slowed / slowed+reverb edit (x0.85) -> played back faster
#   undo_fast2 a mildly sped-up edit (x1.12)
#   tempo_*    time-stretched edits (pitch unchanged)
_VARIANTS = (("denoise", "afftdn=nf=-28,highpass=f=90,lowpass=f=9000,dynaudnorm=f=150"),
             ("undo_fast", _resample(0.80)),
             ("undo_slow", _resample(1.18)),
             ("undo_fast2", _resample(0.89)),
             ("tempo_up", "atempo=1.12"),
             ("tempo_down", "atempo=0.89"))


def _mean_db(path: str):
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-i", path, "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True, timeout=6)
        m = _VOL.search(r.stderr)
        return float(m.group(1)) if m else None
    except Exception:
        return None


def _spread(loudest_first: list, index: dict) -> list:
    """The first wave gets the loudest piece plus pieces from OTHER moments of the clip, so one noisy part of
    the audio cannot sink the whole first wave."""
    head, rest = loudest_first[:6], loudest_first[6:]
    if not head:
        return []
    pick, pool = [head[0]], head[1:]
    while pool and len(pick) < 3:
        nxt = max(pool, key=lambda p: min(abs(index[p] - index[q]) for q in pick))
        pick.append(nxt)
        pool.remove(nxt)
    return pick + pool + rest


def _order_pieces(paths: list) -> list:
    """Loudest first, near-silent pieces dropped (never fewer than 3): no quota is wasted on silence."""
    if not FFMPEG or len(paths) < 2:
        return list(paths)
    index = {p: i for i, p in enumerate(paths)}
    dbs = list(_AUDIO_POOL.map(_mean_db, paths))
    scored = sorted(zip(paths, dbs), key=lambda x: (x[1] is None, -(x[1] if x[1] is not None else 0.0)))
    keep = [p for p, d in scored if d is None or d > SILENT_DB]
    if len(keep) < min(3, len(paths)):
        keep = [p for p, _ in scored][:min(3, len(paths))]
    return _spread(keep, index)


def _make_variants(paths: list, tmp: str):
    jobs = []
    for name, flt in _VARIANTS:                       # interleaved: the first wave already mixes the variant kinds
        for gi, p in enumerate(paths):
            jobs.append((gi, p, os.path.join(tmp, f"v{gi}_{name}.wav"), flt))

    def run(j):
        gi, p, out, flt = j
        try:
            subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", p, "-af", flt,
                            "-ac", "1", "-ar", "22050", "-t", "14", out], timeout=20, check=True)
            return gi, out
        except Exception:
            return None

    done = [r for r in _AUDIO_POOL.map(run, jobs) if r]
    return [o for _, o in done], [g for g, _ in done]


# --- result cache (engine answers only: the own DB can change when a song is deleted / re-learned)
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_MISS = object()


def _cache_key(paths: list):
    try:
        h = hashlib.sha1()
        for p in paths[:2]:
            h.update(str(os.path.getsize(p)).encode())
            with open(p, "rb") as f:
                h.update(f.read(200000))
        return h.hexdigest()
    except OSError:
        return None


def _cache_get(key, rejected: set):
    """-> _MISS, None (a remembered "not found") or a song dict (alts the user rejected are removed)."""
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return _MISS
        ts, song = item
        if time.time() - ts > (CACHE_TTL if song else NEG_TTL) or (song and is_rejected(song, rejected)):
            _CACHE.pop(key, None)                    # expired, or the user said "wrong song"
            return _MISS
        _CACHE.move_to_end(key)
        if song is None:
            return None
        out = dict(song)
        out["alts"] = [a for a in (out.get("alts") or []) if not is_rejected(a, rejected)]
        return out


def _cache_put(key, song):
    with _CACHE_LOCK:
        if song is not None:
            song = {k: v for k, v in song.items() if k != "piece_path"}     # the temp file is gone later
        _CACHE[key] = (time.time(), song)
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_MAX:
            _CACHE.popitem(last=False)


# --- limits: engine rounds at the same time, per-user rate limit (only when user_id is given)
_ROUNDS = threading.BoundedSemaphore(MAX_ROUNDS)
_USER_HITS = {}
_USER_LOCK = threading.Lock()


def _check_user(user_id):
    if user_id is None or USER_LIMIT <= 0:
        return
    now = time.time()
    with _USER_LOCK:
        dq = _USER_HITS.setdefault(user_id, deque())
        while dq and now - dq[0] > USER_WINDOW:
            dq.popleft()
        if len(dq) >= USER_LIMIT:
            raise RateLimited(int(USER_WINDOW - (now - dq[0])) + 1)
        dq.append(now)
        if len(_USER_HITS) > 5000:
            for k in [k for k, v in _USER_HITS.items() if not v or now - v[-1] > USER_WINDOW]:
                _USER_HITS.pop(k, None)


# --- what the singer says (LYRICS_ENGINE, needs faster-whisper and the bot's words_fn)
_words_fn = None
_WHISPER = {"model": None}
_WHISPER_LOCK = threading.Lock()


def _whisper_model():
    from faster_whisper import WhisperModel
    if _WHISPER["model"] is None:
        _WHISPER["model"] = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                                         cpu_threads=WHISPER_THREADS)
    return _WHISPER["model"]


def _pcm16k(path: str):
    """Decodes a piece with ffmpeg (no PyAV needed) -> float32 mono 16 kHz numpy array, or None."""
    import numpy as np
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path, "-f", "f32le", "-ac", "1",
                        "-ar", "16000", "-"], capture_output=True, timeout=30)
    if r.returncode != 0 or not r.stdout:
        return None
    return np.frombuffer(r.stdout, dtype=np.float32)


def transcribe(path: str, cancel=None) -> str:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return ""
    if not _WHISPER_LOCK.acquire(timeout=WHISPER_WAIT):
        log.info("whisper busy, skipping the lyrics stage for this request")
        return ""
    try:
        if cancel is not None and cancel.is_set():
            return ""
        audio = path
        if FFMPEG:                                       # our own decoding: independent of the installed PyAV version
            try:
                audio = _pcm16k(path)
            except Exception as e:
                log.info("lyrics: ffmpeg decoding failed (%s), letting whisper read the file", e)
                audio = path
            if audio is None:
                return ""
        kw = dict(vad_filter=False, beam_size=3, temperature=0.0, condition_on_previous_text=False,
                  no_speech_threshold=0.4, compression_ratio_threshold=2.2)
        if WHISPER_LANG:
            kw["language"] = WHISPER_LANG
        segs, info = _whisper_model().transcribe(audio, **kw)
        if not WHISPER_LANG and (getattr(info, "language_probability", 1.0) or 0) < 0.45:
            return ""                                   # unsure about the language = it is not really speech
        out, last = [], None
        for sg in segs:
            if cancel is not None and cancel.is_set():
                break
            t = sg.text.strip()
            if not t or sg.no_speech_prob > 0.6 or sg.avg_logprob < -1.0:
                continue
            if t != last:
                out.append(t)
            last = t
        text = " ".join(out).strip()
        words = re.findall(r"\w+", text.lower())
        if len(set(words)) < 4:                          # "la la la la" / looping hallucination
            return ""
        return text
    except Exception as e:
        log.info("lyrics: transcription failed: %s", e)
        return ""
    finally:
        _WHISPER_LOCK.release()


def _lyrics_identify(paths: list, cancel=None):
    texts, empty = [], 0
    for p in paths[:max(LYRICS_PIECES, 1)]:
        if cancel is not None and cancel.is_set():
            return None
        t = transcribe(p, cancel)
        if t:
            texts.append(t)
        else:
            empty += 1
            if empty >= 2 and not texts:
                return None
    if not texts or (cancel is not None and cancel.is_set()):
        return None
    words = re.findall(r"\w+", " ".join(texts).lower())
    if len(words) < 6:
        return None
    best = max(texts, key=len)
    return _words_fn(re.findall(r"\w+", best.lower()) or words)


# --- the engines round
def _timed(box: dict, fn, *args):
    box["t"] = time.time()                  # the timeout counts from the START of the request, not the queue
    return fn(*args)


def _limit(eng: str) -> float:
    """How long ONE piece may run (from its start) before the round stops waiting for it."""
    return {"shazam": SHAZAM_TIMEOUT + 4, "audd": AUDD_TIMEOUT + 3, "lyrics": LYRICS_TIMEOUT,
            "local": LOCAL_TIMEOUT + 2}.get(eng, WAVE_TIMEOUT)


def _ask_engines(paths: list, rejected: set, cancel=None, engines=None, full=False, extra=None, meta=None):
    if not _ROUNDS.acquire(timeout=ROUND_WAIT):
        _count("busy")
        raise NoAccounts("server busy")
    try:
        return _ask_engines_core(paths, rejected, cancel, engines or ENGINES, full, extra, meta)
    finally:
        _ROUNDS.release()


def _ask_engines_core(paths, rejected, cancel, engines, full, extra, meta):
    """ONE event-driven round over all engines (ACR / Shazam / AudD):
      - wave 1 (3 pieces) goes out at once; the next wave starts when the FAST engines (ACR / AudD) of the
        running pieces have answered, or after WAVE_STAGGER s - never after a slow Shazam
      - votes are counted as answers arrive; the round stops the moment the answer is confirmed
      - when only a slow Shazam is left AND ACR already gave an answer to fall back on, it is waited for
        SHAZAM_GRACE s at most (SOFT_GRACE s when that answer is decent). With NOTHING to fall back on
        (ACR knows nothing / is paused / out of quota) Shazam is waited for until the round timeout.
      - ACR heard GIVEUP_AFTER pieces and knows nothing -> no more plain pieces (saves quota)
    With full=True and nothing confirmed / decent after the first round (phase B):
      - the best piece again as denoised / speed-corrected / tempo-shifted variants; the own DB gets ALL
        variants too (free). The variants are prepared in the background while the first round still runs.
      - lyrics (what the singer says) run in PARALLEL when LYRICS_ENGINE is on
    `extra` is a live list of votes from elsewhere (the weak answer of the own DB). `cancel` stops everything."""
    engines = tuple(e for e in engines if e != "audd" or AUDD_TOKEN)
    hits = []
    st = {e: {"answered": 0, "failed": 0} for e in ("acr", "shazam", "audd")}
    budget = {"acr": ACR_BUDGET}
    solo = len(paths) == 1
    quit_ev = threading.Event()          # set when the round is over: stops whisper and queued work
    lyr = {"f": None}
    var = {"f": None, "tmp": None}       # the filtered variants, made in the background
    side = {}                            # future -> (piece, engine, box): lyrics / own-DB variants (no quota)
    t_start = time.time()

    log.info("round start: %d pieces, engines=%s, acr_accounts_ready=%d, shazam_paused=%s",
             len(paths), "+".join(engines), len(POOL.available()) if "acr" in engines else 0,
             shazam_paused() if "shazam" in engines else "n/a")

    def cancelled() -> bool:
        if cancel is not None and cancel.is_set():
            quit_ev.set()
            return True
        return False

    def ranked_now() -> list:
        return _rank(hits + (list(extra) if extra else []), solo)

    def answered() -> int:
        return sum(v["answered"] for v in st.values())

    def failed() -> int:
        return sum(v["failed"] for v in st.values())

    def submit(p, gid, stop, pending):
        for eng in engines:
            if eng == "acr":
                if budget["acr"] <= 0:
                    continue
                budget["acr"] -= 1
                ex, fn = _ACR_EXEC, identify_piece
            elif eng == "audd":
                if audd_paused():
                    continue
                ex, fn = _AUDD_EXEC, audd_piece
            elif eng == "shazam":
                if shazam_paused():
                    continue
                ex, fn = _SH_EXEC, shazam_piece
            else:
                continue
            box = {"t": None, "sub": time.time()}
            pending[ex.submit(_timed, box, fn, p, stop)] = (gid, eng, box)

    def start_lyrics():
        if lyr["f"] or not (full and LYRICS_ENGINE and _words_fn) or quit_ev.is_set():
            return
        box = {"t": None, "sub": time.time()}
        lyr["f"] = _LYR_EXEC.submit(_timed, box, _lyrics_identify, paths, quit_ev)
        side[lyr["f"]] = (99, "lyrics", box)

    def start_variants():
        if var["f"] is not None or not (full and SECOND_CHANCE and FFMPEG) or quit_ev.is_set():
            return
        var["tmp"] = tempfile.mkdtemp(prefix="recog_")
        var["f"] = _VAR_POOL.submit(_make_variants, paths[:max(SECOND_PIECES, 1)], var["tmp"])

    def absorb(key, res):
        gid, eng, box = key
        took = time.time() - (box.get("t") or box.get("sub") or time.time())
        if eng == "lyrics":
            log.info("answer lyrics %.1fs -> %s", took, (res or {}).get("title") if res else None)
            if res and not is_rejected(res, rejected):
                hits.append({"piece": 99, "song": {"title": res["title"], "artist": res.get("artist") or "",
                                                   "link": res.get("link"), "yt": None, "dur": None,
                                                   "w": 0.6 if res.get("loose") else 0.7, "rank": 0,
                                                   "engine": "lyrics"}})
            return
        if eng == "local":                                  # the own DB on a variant: a vote, or a sure answer
            if res and not is_rejected(res, rejected):
                sc = res.get("score")
                sure = sc is not None and sc >= LOCAL_SOLO_SCORE
                hits.append({"piece": 98, "song": {**res, "w": 0.99 if sure else LOCAL_WEAK_W, "rank": 0,
                                                   "engine": "local", "sure": sure}})
            return
        if res is None:
            st[eng]["failed"] += 1
            log.info("answer %s piece=%s %.1fs -> no answer (failed / paused / dropped)", eng, gid, took)
            return
        st[eng]["answered"] += 1
        log.info("answer %s piece=%s %.1fs -> %s", eng, gid, took,
                 [(s.get("artist"), s.get("title"), round(s.get("w", 0), 2)) for s in res[:2]] or "nothing found")
        hits.extend({"piece": gid, "song": s} for s in res if not is_rejected(s, rejected))

    def drive(waves, timeout, wait_side) -> bool:
        """Runs `waves` (lists of (path, piece_id)) until confirmed / done / timeout. True = confirmed."""
        stop = threading.Event()
        pending = {}
        s = {"next": 0, "last": 0.0, "shaz": None}
        began = time.time()

        def launch():
            batch = waves[s["next"]]
            s["next"] += 1
            s["last"] = time.time()
            for p, gid in batch:
                submit(p, gid, stop, pending)

        try:
            if waves:
                launch()
            while not cancelled() and time.time() - began <= timeout:
                if _confident(ranked_now()):
                    return True
                now = time.time()
                if full and now - t_start >= LYRICS_EARLY and not _soft_ok(ranked_now(), False):
                    start_lyrics()                           # nothing decent yet: the lyrics run in parallel
                if full and var["f"] is None and now - t_start >= VARIANT_EARLY and not _soft_ok(ranked_now(), False):
                    start_variants()                         # prepare the filtered variants while ACR / Shazam work
                for f, k in side.items():
                    pending.setdefault(f, k)
                for f, k in list(pending.items()):          # too slow: stop waiting for it
                    t = k[2]["t"]
                    if (t is not None and now - t > _limit(k[1])) or (t is None and now - k[2]["sub"] > QUEUE_TIMEOUT):
                        pending.pop(f)
                        side.pop(f, None)
                        f.cancel()
                        if k[1] in st:
                            st[k[1]]["failed"] += 1
                        _count("piece_timeout")
                        log.info("piece timeout: %s piece=%s", k[1], k[0])
                core = [k[1] for k in pending.values() if k[1] in st]
                if s["next"] < len(waves):
                    if s["next"] >= 2 and not ranked_now() and st["acr"]["answered"] >= GIVEUP_AFTER:
                        s["next"] = len(waves)               # ACR heard enough and knows nothing: save the quota
                    elif not any(e in FAST_ENGINES for e in core) or now - s["last"] >= WAVE_STAGGER:
                        launch()
                        continue
                if not (core or (wait_side and pending)):
                    break
                # only a slow Shazam is left. The short grace is ONLY for the case where a fast engine already
                # answered AND something exists to fall back on; with nothing at all, Shazam is the only hope.
                only_slow = (core and all(e == "shazam" for e in core) and s["next"] >= len(waves)
                             and any(st[e]["answered"] for e in FAST_ENGINES) and bool(ranked_now()))
                if only_slow and not (wait_side and any(k[1] not in st for k in pending.values())):
                    s["shaz"] = s["shaz"] or now
                    grace = SOFT_GRACE if _soft_ok(ranked_now(), False) else SHAZAM_GRACE
                    if now - s["shaz"] >= grace:
                        break
                else:
                    s["shaz"] = None
                done, _ = wait(set(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                for f in done:
                    key = pending.pop(f)
                    side.pop(f, None)
                    try:
                        res = f.result()
                    except Exception as e:
                        log.warning("engine piece crashed: %s", e)
                        res = None
                    absorb(key, res)
            return _confident(ranked_now())
        finally:
            stop.set()                       # queued work is dropped, running requests send nothing more
            for f, k in pending.items():
                if k[1] in st:
                    f.cancel()

    try:
        waves, pos = [], 0
        for size in WAVES:
            batch = [(p, i) for i, p in enumerate(paths) if pos <= i < pos + size]
            pos += size
            if batch:
                waves.append(batch)
        drive(waves, ROUND_TIMEOUT, False)
        ranked = ranked_now()

        # nothing confirmed and no decent normal answer yet -> the second chance (filtered variants + lyrics)
        if full and not _confident(ranked) and not cancelled() and not _soft_ok(ranked, False):
            start_lyrics()
            waves_b = []
            try:
                start_variants()
                vp, vg = [], []
                if var["f"] is not None:
                    try:
                        vp, vg = var["f"].result(timeout=15)
                    except Exception as e:
                        log.warning("variants failed: %s", e)
                if vp:
                    log.info("second chance: %d variants", len(vp))
                if vp and _local_fn:
                    for v in vp:                             # the own DB is free: it gets every variant
                        box = {"t": None, "sub": time.time()}
                        side[_LOCAL_POOL.submit(_timed, box, _local_one, v)] = (98, "local", box)
                if vp and answered() > 0:                    # engines are up: send the first variants to them
                    pairs = list(zip(vp, vg))[:VARIANT_ENGINE_N]
                    waves_b = [pairs[i:i + 3] for i in range(0, len(pairs), 3)]
                drive(waves_b, PHASE_B_TIMEOUT, True)
            except Exception as e:
                log.warning("second chance failed: %s", e)
            ranked = ranked_now()

        if meta is not None:
            meta["clean"] = answered() > 0 and failed() == 0         # a real "not found", not an outage
        if cancelled():
            return None
        if not ranked:
            # raise only when NOBODY could answer. If an engine (e.g. Shazam) answered "not found" while ACR is
            # down, the honest result is "not found", not a generic error.
            if answered() == 0 and failed() > 0:
                _count("no_accounts")
                log.warning("recognize: no engine could answer (acr=%s shazam=%s audd=%s)",
                            st["acr"], st["shazam"], st["audd"])
                raise NoAccounts("no engine could answer")
            log.info("recognize: nothing (acr=%s shazam=%s)", st["acr"], st["shazam"])
            _count("not_found")
            return None
        # per song: title, artist, score, solid?, engines, [(piece, melody?, rank, weight) of every answer]
        log.info("recognize: %s", [(c["song"].get("title"), c["song"].get("artist"), round(c["score"], 2), c["solid"],
                                   sorted(e for e in c["engines"] if e),
                                   [(h["piece"], bool(h["song"].get("hum")), h["song"]["rank"],
                                     round(h["song"]["w"], 2)) for h in c["hits"]])
                                  for c in ranked[:3]])
        top_c, guess = ranked[0], False
        if STRICT and not top_c["solid"]:
            if _soft_ok(ranked, True):
                log.info("recognize: not confirmed but decent -> answered (SOFT_ACCEPT)")
                _count("soft_accept")
            else:
                g = _guess_pick(ranked)
                if g is None:
                    log.info("recognize: best answer is not confirmed and not plausible enough -> not found")
                    _count("unconfirmed")
                    return None
                top_c, guess = g, True
                log.info("recognize: not confirmed -> best guess: %s - %s (GUESS_MODE)",
                         top_c["song"].get("artist"), top_c["song"].get("title"))
                _count("guess")
        _count("found")
        drop = ("rank", "w", "hum", "score", "sure")
        top = {k: v for k, v in top_c["song"].items() if k not in drop}
        top["engine"] = "+".join(sorted(e for e in top_c["engines"] if e)) or top.get("engine")
        own = [h["piece"] for h in (top_c["first"] or top_c["hits"]) if h["piece"] < len(paths)]
        top["piece_path"] = paths[min(own) if own else 0]
        top["alts"] = [{**{k: v for k, v in c["song"].items() if k not in drop}, "guess": True}
                       for c in ranked if c is not top_c][:3]     # what "wrong song" shows next: always guesses
        if guess:
            top["guess"] = True
        log.info("recognize: answered in %.1fs", time.time() - t_start)
        return top
    finally:
        quit_ev.set()
        if var["f"] is not None:                 # remove the variant files as soon as their making is over
            tmp_dir = var["tmp"]
            var["f"].add_done_callback(lambda _f: shutil.rmtree(tmp_dir, ignore_errors=True))


# --- public API
def ask_local(paths: list, rejected: set = frozenset()):
    """Own DB only (free). Returns the answer only when it is strong (see _local_scan)."""
    strong, _ = _local_scan(list(paths), set(rejected))
    return strong


def ask_local_full(paths: list, rejected: set = frozenset()):
    """Own DB only (free) -> (strong answer or None, [weak vote]). Give the second value to ask_engines(extra=...):
    a weak DB match then counts as one more voter instead of being thrown away."""
    strong, weak = _local_scan(list(paths), set(rejected))
    if strong:
        return strong, []
    return None, ([{"piece": 98, "song": weak}] if weak else [])


def ask_acr(paths: list, rejected: set = frozenset()):
    """ACRCloud only (costs quota)."""
    return _ask_engines(list(paths), set(rejected), engines=("acr",))


def ask_shazam(paths: list, rejected: set = frozenset()):
    """Shazam only."""
    return _ask_engines(list(paths), set(rejected), engines=("shazam",))


def _engines_entry(paths, rejected, cancel, user_id, extra, pre_ordered):
    paths = list(paths)[:MAX_PIECES]
    if not paths:
        return None
    key = _cache_key(paths)
    if key:
        hit = _cache_get(key, rejected)
        if hit is not _MISS:
            if hit is None:
                log.info("recognize: cached 'not found'")
                _count("cache_neg")
                return None
            hit["piece_path"] = paths[0]                  # a path that exists NOW (the cached one is long gone)
            log.info("recognize: cache hit %s", hit["title"])
            _count("cache_hit")
            return hit
    _check_user(user_id)
    ordered = paths if pre_ordered else _order_pieces(paths)
    meta = {}
    res = _ask_engines(ordered, rejected, cancel, full=True, extra=extra, meta=meta)
    if key:
        if res and "local" not in (res.get("engine") or "") and not res.get("guess"):
            _cache_put(key, res)                          # a guess is never cached: it may be better next time
        elif res is None and meta.get("clean") and not extra and not (cancel is not None and cancel.is_set()):
            _cache_put(key, None)
    return res


def ask_engines(paths: list, rejected: set = frozenset(), cancel=None, user_id=None, extra=None):
    """ACRCloud + Shazam (+ AudD) voting WITHOUT the own DB (costs quota): loudest pieces first, silence dropped;
    when nothing is confirmed: denoised / speed-corrected variants and the lyrics. Raises NoAccounts (and
    RateLimited when user_id is given). The answer has "engine" ("acr", "shazam", "acr+shazam"...) and
    "piece_path"; song["guess"] is True when it is only the best plausible candidate (GUESS_MODE).
    `extra` = the weak DB vote from ask_local_full(). The same audio asked again is answered from a cache
    ("wrong song" evicts the rejected answer)."""
    return _engines_entry(paths, set(rejected), cancel, user_id, list(extra) if extra else None, False)


def _warmup():
    """Takes the slow first-request costs out of the first user's way."""
    if WHISPER_PRELOAD and LYRICS_ENGINE and _words_fn:
        try:
            with _WHISPER_LOCK:
                _whisper_model()
            log.info("whisper model '%s' preloaded", WHISPER_MODEL)
        except Exception as e:
            log.info("whisper preload skipped: %s", e)
    try:
        for h in {a["host"] for a in POOL.status()}:
            _HTTP.head(f"https://{h}", timeout=5)             # TLS handshake now, not during the first clip
    except Exception:
        pass


def init(load_accounts, local_fn=None, words_fn=None):
    """load_accounts() -> [{"id","host","key","secret"}]; local_fn(path) -> {"title","artist",...} or None;
    words_fn(words) -> {"title","artist"} or None finds a song from a few words of its lyrics (the lyrics stage)."""
    global _local_fn, _words_fn
    POOL.set_loader(load_accounts)
    _local_fn = local_fn
    _words_fn = words_fn
    log.info("recognition v11: engines=%s round_timeout=%ss shazam_timeout=%ss shazam_threads=%d",
             "+".join(ENGINES), ROUND_TIMEOUT, SHAZAM_TIMEOUT, SHAZAM_THREADS)
    threading.Thread(target=_warmup, daemon=True, name="recog-warmup").start()


def _local_job(paths, rejected, extra):
    strong, weak = _local_scan(paths, rejected)
    if strong:
        return strong
    if weak:
        extra.append({"piece": 98, "song": weak})      # one more voter for the engines round
    return None


def recognize(paths, rejected=frozenset(), user_id=None):
    """paths: audio pieces. rejected: title_key()s (or reject_key()s) the user already marked as wrong.
    Own DB + engines. The DB runs at the same time as the engines: a strong DB answer is returned at once and
    the engines are cancelled; a weak one becomes a vote. (LOCAL_FIRST=1: DB alone first.)"""
    paths = [paths] if isinstance(paths, str) else list(paths)[:MAX_PIECES]
    if not paths:
        return None
    rejected = set(rejected)
    ordered = _order_pieces(paths)
    extra = []
    if LOCAL_FIRST or not _local_fn:
        res = _local_job(ordered, rejected, extra) if _local_fn else None
        return res or _engines_entry(ordered, rejected, None, user_id, extra, True)
    cancel = threading.Event()
    local_fut = _MAIN_POOL.submit(_local_job, ordered, rejected, extra)
    eng_fut = _MAIN_POOL.submit(_engines_entry, ordered, rejected, cancel, user_id, extra, True)
    try:
        res = local_fut.result()
    except Exception as e:
        log.warning("local DB crashed: %s", e)
        res = None
    if res:
        cancel.set()
        return res
    return eng_fut.result()          # may raise NoAccounts / RateLimited


def accounts_status() -> list:
    return POOL.status()


def stats() -> dict:
    """Counters since start (found / guess / not_found / unconfirmed / soft_accept / cache_hit / acr_calls ...)."""
    with _CACHE_LOCK:
        size = len(_CACHE)
    return {"counters": dict(_STAT), "cache_size": size, "shazam": shazam_status(), "acr": accounts_status(),
            "audd": {"enabled": bool(AUDD_TOKEN), "paused": audd_paused()}}


# --- offline self test:  python recognition.py --selftest
def _selftest():
    S = lambda t, a, e, w, rank=0, **k: {"title": t, "artist": a, "w": w, "rank": rank, "engine": e, **k}
    assert title_key("(Intro)") == "intro"
    assert title_key("Song (feat. X) - Remastered 2011") == "song"
    assert same_song({"title": "Back in Black", "artist": "AC/DC"}, {"title": "Back In Black", "artist": "ACDC"})
    assert same_song({"title": "Some Song", "artist": "Florence and the Machine"},
                     {"title": "Some Song", "artist": "Florence + The Machine"})
    assert first_artist("AC/DC") == "AC/DC" and first_artist("Drake feat. Rihanna") == "Drake"
    assert not same_song({"title": "Home", "artist": ""}, {"title": "Homeless", "artist": "X"})
    assert not same_song({"title": "Love", "artist": ""}, {"title": "Love", "artist": "X"})      # short + no artist
    assert not same_song({"title": "Song", "artist": "A"}, {"title": "Song (Remix)", "artist": "A"})
    assert is_rejected({"title": "Crazy", "artist": "Gnarls Barkley"}, {"crazy"})
    gb = reject_key({"title": "Crazy", "artist": "Gnarls Barkley"})
    assert is_rejected({"title": "Crazy", "artist": "Gnarls Barkley"}, {gb})
    assert not is_rejected({"title": "Crazy", "artist": "Patsy Cline"}, {gb})
    weak = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.9)}]
    assert not _confident(_rank(weak))                                  # one engine, one piece, score 90: not confirmed
    assert _soft_ok(_rank(weak), False)                                 # ... but decent: answered right after round 1
    assert _confident(_rank([{"piece": 0, "song": S("A Long Song", "X", "acr", 0.99)}]))   # ONE extremely sure answer
    assert _confident(_rank([{"piece": 0, "song": S("A Long Song", "X", "shazam", 0.99)}]))  # a single Shazam answer
    assert _confident(_rank([{"piece": 0, "song": S("A Long Song", "X", "acr", 0.96)}], solo=True))   # 1-piece clip
    two = weak + [{"piece": 0, "song": S("A Long Song", "X", "shazam", 0.99)}]
    assert _confident(_rank(two))                                       # two engines agree
    hum = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.99, hum=True)}]
    assert not _confident(_rank(hum))                                   # a single humming guess is never solid
    humrep = [{"piece": 0, "song": S("A Long Song", "X", "acr", w, hum=True)} for w in (0.86, 0.9, 0.89)]
    assert _confident(_rank(humrep))                                    # three strong melody matches
    assert not _confident(_rank(humrep[:2]))                            # two matches of ONE piece are not enough
    humtwo = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.8, hum=True)},
              {"piece": 1, "song": S("A Long Song", "X", "acr", 0.78, hum=True)}]
    assert _confident(_rank(humtwo))                                    # two different pieces agree on the melody
    humweak = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.7, hum=True)} for _ in range(3)]
    assert not _confident(_rank(humweak))                               # weak melody matches never confirm
    acr2 = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.7)},
            {"piece": 0, "song": S("A Long Song", "X", "acr", 0.65)}]
    assert _confident(_rank(acr2))                                      # ACR repeats the same normal answer
    lyr = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.6)}, {"piece": 99, "song": S("A Long Song", "X", "lyrics", 0.7)}]
    assert not _confident(_rank(lyr))                                   # lyrics + a weak hit never confirm
    lyr2 = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.9)}, {"piece": 99, "song": S("A Long Song", "X", "lyrics", 0.7)}]
    assert _confident(_rank(lyr2))
    db = [{"piece": 98, "song": S("A Long Song", "X", "local", 0.99, sure=True)}]
    assert _confident(_rank(db))                                        # the own DB is sure
    dbw = [{"piece": 98, "song": S("A Long Song", "X", "local", 0.7)}, {"piece": 0, "song": S("A Long Song", "X", "acr", 0.9)}]
    assert _confident(_rank(dbw))                                       # weak DB vote + one ACR hit = two engines
    rival = two + [{"piece": 1, "song": S("Other Thing", "Y", "acr", 0.9)}, {"piece": 2, "song": S("Other Thing", "Y", "acr", 0.9)}]
    assert not _confident(_rank(rival))                                 # a confirmed rival blocks
    remix = two + [{"piece": 1, "song": S("A Long Song (Remix)", "X", "acr", 0.9)}]
    assert _confident(_rank(remix))                                     # a remix beside the original does not
    remix_hum = humrep + [{"piece": 0, "song": S("Other (Slowed & Reverb)", "Y", "acr", w, hum=True)} for w in (0.76, 0.81)]
    assert _confident(_rank(remix_hum))                                 # the real case: melody match + slowed remix beside it
    # soft acceptance
    assert not _soft_ok(_rank([{"piece": 0, "song": S("A Long Song", "X", "acr", 0.7)}]), True)       # too weak
    h1 = [{"piece": 0, "song": S("A Long Song", "X", "acr", 0.85, hum=True)}]
    assert not _soft_ok(_rank(h1), False) and _soft_ok(_rank(h1), True)    # melody guess: only after the second chance
    assert not _soft_ok(_rank([{"piece": 0, "song": S("A Long Song", "X", "acr", 0.7, hum=True)}]), True)
    two_songs = weak + [{"piece": 0, "song": S("Other Thing", "Y", "acr", 0.85)}]
    assert not _soft_ok(_rank(two_songs), False)                        # a close rival blocks
    assert not _soft_ok(_rank([{"piece": 0, "song": S("A Long Song (Remix)", "X", "acr", 0.9)}]), True)   # remix: never
    assert not _soft_ok(_rank([{"piece": 99, "song": S("A Long Song", "X", "lyrics", 0.9)}]), True)       # lyrics: never
    assert _soft_ok(_rank(h1 + [{"piece": 0, "song": S("Other (Slowed & Reverb)", "Y", "acr", 0.8, hum=True)}]), True)
    # best guess (GUESS_MODE)
    g = lambda *hs: _guess_pick(_rank(list(hs)))
    assert g({"piece": 0, "song": S("A Long Song", "X", "acr", 0.6, hum=True)}) is None                   # weak melody
    assert g({"piece": 0, "song": S("A Long Song", "X", "acr", 0.72, hum=True)}) is not None             # decent melody
    assert g({"piece": 0, "song": S("A Long Song", "X", "acr", 0.55)}) is not None                       # weak normal match
    assert g({"piece": 0, "song": S("A Long Song", "X", "acr", 0.5, rank=1)}) is None                    # only an alternative
    assert g({"piece": 99, "song": S("A Long Song", "X", "lyrics", 0.6)}) is not None                    # loose lyrics
    assert g({"piece": 98, "song": S("A Long Song", "X", "local", 0.7, score=10)}) is None               # DB noise
    assert g({"piece": 98, "song": S("A Long Song", "X", "local", 0.7, score=25)}) is not None           # DB partial match
    best = g({"piece": 98, "song": S("Noise Track", "N", "local", 0.7, score=5)},
             {"piece": 0, "song": S("A Long Song", "X", "acr", 0.8, hum=True)})
    assert best is not None and best["song"]["title"] == "A Long Song"                                    # skips the noisy cluster
    assert _check_user(None) is None
    assert len(_VARIANTS) == 6 and "asetrate=35280" in _VARIANTS[1][1]
    print("selftest ok")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
