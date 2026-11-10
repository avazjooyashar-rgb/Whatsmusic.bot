"""
recognition.py - music recognition core (the ONLY file that knows how a song is recognized).

  1) own fingerprint DB (free, fast, knows remixes / special versions)  -> if it knows the song, done
  2) ACRCloud on several pieces, with automatic account failover:
       - accounts are tried in PRIORITY order (the lowest id first)
       - an exhausted account is paused and the next one is used
       - the pause is short and the account is probed again, so when the quota of the first account
         is open again the bot goes back to it by itself (nothing is ever "cut off")

bot.py only uses this small interface, so this file can be replaced / changed without touching the bot:

    init(load_accounts, local_fn)      once at start. load_accounts() -> [{"id","host","key","secret"}]
                                       local_fn(path) -> {"title","artist"[,"link","yt"]} or None
    ask_local(paths, rejected)         own DB only (free)            -> song dict or None
    ask_acr(paths, rejected)           ACRCloud only (costs quota)   -> song dict or None, raises NoAccounts
    recognize(paths, rejected)         both, in that order
    NoAccounts                         raised when no ACRCloud account could answer
    title_key, artist_set, same_song, is_variant, fold, first_artist     small text helpers

A song dict: {"title","artist","link","yt","dur","engine","alts"}; "rejected" = set of title_key()s.
"""
import base64
import hashlib
import hmac
import logging
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests

log = logging.getLogger("recognition")

# --- config
MAX_PIECES = int(os.environ.get("MAX_SAMPLES", "10"))
WAVES = (3, 3, 4)                                             # pieces sent to ACR per wave
ACR_TIMEOUT = int(os.environ.get("ACR_TIMEOUT", "15"))        # one request
WAVE_TIMEOUT = int(os.environ.get("WAVE_TIMEOUT", "20"))      # one wave
ACR_THREADS = int(os.environ.get("ACR_THREADS", "16"))
LOCAL_TIMEOUT = int(os.environ.get("LOCAL_TIMEOUT", "8"))
LOCAL_MIN_AGREE = int(os.environ.get("LOCAL_MIN_AGREE", "2"))  # own DB: pieces that must agree
SOLO_SCORE = float(os.environ.get("ACR_SOLO_SCORE", "90"))     # one piece is enough from this score (0 = off)
STRICT = os.environ.get("STRICT", "1") == "1"                  # unconfirmed answer -> None

QUOTA_CODES = {3003}                 # limit exceeded
AUTH_CODES = {3001, 3014}            # bad key / signature
NO_MATCH_CODES = {1001, 2004}        # "nothing found": a real answer, other accounts would say the same
QUOTA_RETRY = int(os.environ.get("ACR_QUOTA_RETRY", "600"))    # probe an exhausted account again after 10 min
AUTH_PAUSE = 6 * 3600
ERROR_PAUSE = 60                     # after 3 network errors in a row
ERROR_LIMIT = 3


class NoAccounts(Exception):
    """No ACRCloud account could answer (all paused / unreachable)."""


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


def _local_one(path: str):
    try:
        r = _local_fn(path)
    except Exception as e:
        log.warning("local DB failed: %s", e)
        return None
    if r and r.get("title"):
        return {"title": r["title"], "artist": r.get("artist") or "", "link": r.get("link"),
                "yt": r.get("yt"), "dur": r.get("dur"), "engine": "local"}
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


def _parse(md: dict) -> list:
    out = []
    for rank, m in enumerate((md.get("music") or [])[:3]):
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
        out.append({"title": m["title"], "artist": ", ".join(a["name"] for a in (m.get("artists") or [])
                                                             if a.get("name")),
                    "link": link, "yt": yt, "dur": dur, "score": score, "rank": rank, "engine": "acr"})
    return out


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


# --- voting
def _rank(hits: list) -> list:
    """hits: [{"piece": int, "song": {...}}] -> clusters of the same song, best first."""
    clusters = []
    for h in hits:
        for c in clusters:
            if same_song(c["song"], h["song"]):
                c["hits"].append(h)
                break
        else:
            clusters.append({"song": dict(h["song"]), "hits": [h]})
    for c in clusters:
        per_piece = {}
        for h in c["hits"]:
            s = h["song"]
            w = s["score"] / 100 * (1.0 if s["rank"] == 0 else 0.4)   # 2nd / 3rd candidates count less
            per_piece[h["piece"]] = max(per_piece.get(h["piece"], 0.0), w)
        firsts = [h for h in c["hits"] if h["song"]["rank"] == 0]
        c["weight"] = sum(per_piece.values())
        c["pieces"] = len(per_piece)
        c["first"] = len({h["piece"] for h in firsts})
        c["best"] = max((h["song"]["score"] for h in firsts), default=0.0)
        for k in ("link", "yt", "dur"):
            c["song"][k] = next((h["song"][k] for h in c["hits"] if h["song"].get(k)), None)
    clusters.sort(key=lambda c: c["weight"], reverse=True)
    return clusters


def _confident(ranked: list) -> bool:
    """Confirmed = two different pieces agree (one of them as ACR's first answer), or one piece with a very
    high score. And no close competitor."""
    if not ranked:
        return False
    top = ranked[0]
    confirmed = top["first"] >= 1 and top["pieces"] >= 2
    solo = SOLO_SCORE > 0 and top["best"] >= SOLO_SCORE
    if not (confirmed or solo):
        return False
    if is_variant(top["song"]) and not confirmed:   # a remix / live / cover needs two pieces
        return False
    if len(ranked) > 1:
        second = ranked[1]
        if second["pieces"] >= 2 or second["weight"] >= top["weight"] * 0.6:
            return False
    return True


def _ask_acr(paths: list, rejected: set):
    hits, answered, failed, ranked = [], 0, 0, []
    pieces = list(enumerate(paths))
    pos = 0
    for size in WAVES:
        wave = pieces[pos:pos + size]
        pos += size
        if not wave:
            break
        futs = {_ACR_EXEC.submit(identify_piece, p): i for i, p in wave}
        pending, deadline = set(futs), time.time() + WAVE_TIMEOUT
        while pending and time.time() < deadline and not _confident(ranked):
            done, pending = wait(pending, timeout=max(deadline - time.time(), 0.05),
                                 return_when=FIRST_COMPLETED)
            for f in done:
                try:
                    res = f.result()
                except Exception as e:
                    log.warning("ACR piece crashed: %s", e)
                    res = None
                if res is None:
                    failed += 1
                    continue
                answered += 1
                hits += [{"piece": futs[f], "song": s} for s in res if title_key(s["title"]) not in rejected]
            ranked = _rank(hits)
        for f in pending:
            f.cancel()                       # not sent yet -> no quota used
        if _confident(ranked):
            break
    if not answered and failed:
        raise NoAccounts("no ACRCloud account could answer")
    if not ranked or (STRICT and not _confident(ranked)):
        log.info("recognize: %s", [(c["song"]["title"], round(c["weight"], 2)) for c in ranked[:3]] or "nothing")
        return None
    top = {k: v for k, v in ranked[0]["song"].items() if k not in ("rank", "score")}
    top["alts"] = [{k: v for k, v in c["song"].items() if k not in ("rank", "score")} for c in ranked[1:4]]
    return top


# --- public API
ask_local = _ask_local   # own DB only (free)
ask_acr = _ask_acr       # ACRCloud only (costs quota)


def init(load_accounts, local_fn=None):
    """load_accounts() -> [{"id","host","key","secret"}]; local_fn(path) -> {"title","artist",...} or None."""
    global _local_fn
    POOL.set_loader(load_accounts)
    _local_fn = local_fn


def recognize(paths, rejected=frozenset()):
    """paths: audio pieces. rejected: title_key()s the user already marked as wrong.
    Own DB first; ACRCloud only when the DB does not know the song."""
    paths = [paths] if isinstance(paths, str) else list(paths)[:MAX_PIECES]
    if not paths:
        return None
    rejected = set(rejected)
    return ask_local(paths, rejected) or ask_acr(paths, rejected)


def accounts_status() -> list:
    return POOL.status()
