import json, asyncio, sqlite3, datetime, os, re, tempfile, glob, time, hmac, hashlib, base64, uuid, subprocess, logging, html, shutil
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse, parse_qs
import requests
import yt_dlp
from telegram import (Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
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
ENGINE_ORDER = os.environ.get("ENGINES", "acr,audd").split(",")
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
SAMPLES_DIR = os.environ.get("SAMPLES_DIR", "samples")
COOKIES = os.environ.get("COOKIES_FILE", "cookies.txt")
MAX_BYTES = 49 * 1024 * 1024
os.makedirs(SAMPLES_DIR, exist_ok=True)

URL_RE = re.compile(r"https?://\S+")
DIRECT_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "music.youtube.com")
CLIP_HOSTS = ("youtube.com", "youtu.be", "instagram.com")
MP3_PREFIX = "🎵 "

esc = html.escape


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
    m = r["metadata"]["music"][0]
    ext = m.get("external_metadata", {})
    link = None
    yt = None
    if ext.get("spotify", {}).get("track", {}).get("id"):
        link = "https://open.spotify.com/track/" + ext["spotify"]["track"]["id"]
    if ext.get("youtube", {}).get("vid"):
        yt = "https://www.youtube.com/watch?v=" + ext["youtube"]["vid"]
        link = link or yt
    return {"title": m["title"], "artist": m["artists"][0]["name"], "link": link, "yt": yt}


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
            yt, _ = odesli_to_youtube(link, timeout=4)
            if yt:
                return yt
        except Exception:
            pass
    return f"ytsearch1:{song['artist']} - {song['title']}"


# ---------- yt-dlp ----------
def ydl_base():
    o = {"quiet": True, "noplaylist": True,
         "concurrent_fragment_downloads": 4,
         "http_chunk_size": 10 * 1024 * 1024}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def to_mp3(src: str) -> str:
    out = os.path.splitext(src)[0] + ".mp3"
    subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "libmp3lame", "-b:a", "192k", out],
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
    opts = {**ydl_base(), "format": "bestaudio[ext=m4a]/bestaudio/best",
            "outtmpl": f"{outdir}/%(id)s.%(ext)s"}

    def run(t):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(t, download=True)
            if "entries" in info:
                if not info["entries"]:
                    raise RuntimeError("آهنگ توی SoundCloud پیدا نشد (یوتیوب هم بلاکه، کوکی لازمه)")
                info = info["entries"][0]
        return info

    try:
        info = run(target)
    except Exception as e:
        if not fallback:
            raise
        log.warning("youtube failed (%s), trying soundcloud", str(e)[:80])
        info = run(f"scsearch1:{fallback}")
    files = [f for f in glob.glob(f"{outdir}/*") if not f.endswith((".part", ".ytdl", ".json"))]
    if not files:
        raise RuntimeError("فایل صوتی ساخته نشد")
    path = max(files, key=os.path.getmtime)
    if not path.lower().endswith((".m4a", ".mp3")):
        path = to_mp3(path)
    vid = info.get("id") if info.get("extractor_key") == "Youtube" else None
    dur = info.get("duration")
    return {"path": path, "title": info.get("title", "music"), "vid": vid,
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
        meta = {"artist": artist.split(",")[0].strip(), "title": track}
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


def make_samples(src: str, sid: str) -> list:
    d = probe_duration(src)
    win = 15
    if d and d > win + 3:
        offs = [d * 0.4 - win / 2, d * 0.05, d * 0.75 - win / 2]
        offs = [max(0, min(o, d - win)) for o in offs]
    else:
        offs = [0]
    jobs, seen = [], set()
    for i, o in enumerate(offs):
        o = round(o)
        if o in seen:
            continue
        seen.add(o)
        jobs.append((os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3"), o))
    # cut all samples at the same time instead of one by one
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(lambda j: j[0] if cut_sample(src, j[0], j[1], win) else None, jobs))
    return [r for r in res if r]


def yt_search(query: str, n: int = 10):
    opts = {**ydl_base(), "extract_flat": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{n}:{query}", download=False)
    return info.get("entries") or []


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
    a = (artist or "").split("/")[0].split(",")[0].strip()
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
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, host TEXT, key TEXT, secret TEXT,
        enabled INTEGER DEFAULT 1, uses INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS songs(key TEXT PRIMARY KEY, file_id TEXT, title TEXT, info TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS usage(uid INTEGER, day TEXT, n INTEGER, PRIMARY KEY(uid, day))")
    c.execute("CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, first_seen TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS samples(
        id TEXT PRIMARY KEY, path TEXT, tried TEXT DEFAULT '', last TEXT, created INTEGER, src TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS clips(url TEXT PRIMARY KEY, file_id TEXT, song TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS tracks(id TEXT PRIMARY KEY, title TEXT, artist TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS lyrics(key TEXT PRIMARY KEY, text TEXT)")
    return c


def migrate():
    """Add new columns to an old bot.db (safe to run every start)."""
    with db() as c:
        for table, col in (("songs", "info"), ("samples", "src")):
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


def touch_user(uid: int):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users VALUES(?,?)", (uid, datetime.date.today().isoformat()))


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
        return c.execute("SELECT file_id, song FROM clips WHERE url=?", (url,)).fetchone()


def clip_put(url, file_id, song: dict):
    data = {k: song.get(k) for k in ("title", "artist", "link", "yt")}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO clips VALUES(?,?,?)",
                  (url, file_id, json.dumps(data, ensure_ascii=False)))


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


def _chain(path: str, skip=()):
    with db() as c:
        rows = c.execute("SELECT * FROM accounts WHERE enabled=1").fetchall()
    for name in [n.strip() for n in ENGINE_ORDER]:
        if name == "shazam":
            if "shazam#0" in skip:
                continue
            try:
                res = shazam_identify(path)
                if res:
                    res["engine"] = "shazam#0"
                    return res
            except Exception as e:
                log.warning("shazam failed: %s", e)
            continue
        if name not in ENGINE_FN:
            continue
        for r in sorted((x for x in rows if x["type"] == name), key=lambda x: x["id"]):
            tag = f'{name}#{r["id"]}'
            if tag in skip:
                continue
            try:
                res = ENGINE_FN[name](path, r)
                with db() as c:
                    c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (r["id"],))
                if res:
                    res["engine"] = tag
                    return res
                break
            except Exception as e:
                log.warning("account %s failed: %s", r["id"], e)
                with db() as c:
                    c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
    return None


def recognize(paths, skip=()):
    if isinstance(paths, str):
        paths = [paths]
    for p in paths:
        res = _chain(p, skip)
        if res:
            return res
    return None


# ---------- Captions & keyboards ----------
def footer(src=None) -> str:
    f = f'<a href="https://t.me/{BOT_USERNAME}">{esc(BOT_USERNAME)}</a>'
    if src:
        f += f' | <a href="{esc(src, quote=True)}">source</a>'
    return f


def result_caption(song: dict, src=None) -> str:
    return f"<code>{esc(song['title'])} — {esc(song['artist'])}</code>\n\n{footer(src)}"


def plain_caption(title: str, src=None, note: str = "") -> str:
    cap = f"<code>{esc((title or 'clip')[:150])}</code>"
    if note:
        cap += f"\n{esc(note)}"
    return f"{cap}\n\n{footer(src)}"


def audio_caption(info=None) -> str:
    if info:
        return f'@{esc(BOT_USERNAME)} | <a href="{esc(info, quote=True)}">info</a>'
    return f"@{esc(BOT_USERNAME)}"


def result_keyboard(sid: str, song: dict):
    q = quote(f"{song['artist']} - {song['title']}")
    link = song.get("link") or ""
    sp = link if link.startswith("https://open.spotify.com") else f"https://open.spotify.com/search/{q}"
    return Markup([
        [Btn("Google", url=f"https://www.google.com/search?q={q}"),
         Btn("YouTube Music", url=f"https://music.youtube.com/search?q={q}"),
         Btn("Spotify", url=sp)],
        [Btn("🔍 جستجو بر اساس هنرمند", switch_inline_query_current_chat=song["artist"])],
        [Btn("❌ آهنگ اشتباه ❌", callback_data=f"bad:{sid}")],
    ])


def audio_keyboard(tid: str, song: dict):
    q = f"{song.get('artist', '')} {song.get('title', '')}".strip()[:200]
    return Markup([[
        Btn("متن ترانه 🔤", callback_data=f"lyr:{tid}"),
        Btn("🔍", switch_inline_query_current_chat=q),
    ]])


# ---------- Sending audio ----------
def song_key(song: dict) -> str:
    return "s:" + f"{song['artist']}|{song['title']}".lower()


async def fetch_audio(target: str, song=None) -> dict:
    """Download step. On a cache hit returns the file_id right away."""
    keys = [target] + ([song_key(song)] if song else [])
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


async def deliver_audio(msg, a: dict, song=None):
    title = (song or {}).get("title") or a["title"]
    performer = (song or {}).get("artist") or None
    tsong = {"title": title, "artist": performer or ""}
    kb = audio_keyboard(track_put(tsong), tsong)
    if "file_id" in a:
        return await msg.reply_audio(a["file_id"], title=title, performer=performer,
                                     caption=audio_caption(a.get("info")), parse_mode="HTML",
                                     reply_markup=kb)
    info = f"https://song.link/y/{a['vid']}" if a.get("vid") else None
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


async def send_mp3(msg, target: str, title=None, performer=None):
    song = {"title": title, "artist": performer or ""} if title else None
    a = await fetch_audio(target, song)
    await deliver_audio(msg, a, song)


# ---------- Clips ----------
async def upload_video(msg, path: str, caption: str):
    with open(path, "rb") as f:
        return await msg.reply_video(f, caption=caption, parse_mode="HTML", supports_streaming=True,
                                     read_timeout=300, write_timeout=300)


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


def _msg_video_id(sent):
    for attr in ("video", "animation", "document"):
        obj = getattr(sent, attr, None)
        if obj:
            return obj.file_id
    return None


async def clip_from_cache(msg, src: str, hit):
    song = json.loads(hit["song"])
    sid = uuid.uuid4().hex[:10]
    add_sample(sid, "", src)
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", ("cache#0", sid))
    audio_task = asyncio.create_task(prepare_song_audio(song))
    try:
        await msg.reply_video(hit["file_id"], caption=result_caption(song, src), parse_mode="HTML",
                              reply_markup=result_keyboard(sid, song), supports_streaming=True)
    except Exception:
        audio_task.cancel()
        raise
    try:
        a = await audio_task
        await deliver_audio(msg, a, song)
    except Exception as e:
        log.exception("cached clip audio failed")
        await msg.reply_text(f"❌ خطا: {str(e)[:200]}")


async def do_clip(msg, url: str):
    uid = msg.from_user.id
    src = norm_url(url)
    cleanup_samples()

    hit = clip_get(src)
    if hit and hit["song"]:
        try:
            return await clip_from_cache(msg, src, hit)
        except Exception:
            log.exception("clip cache send failed, re-downloading")
            clip_delete(src)

    with tempfile.TemporaryDirectory() as tmp:
        path, title, meta = await asyncio.to_thread(download_clip, src, tmp)
        if not path:
            return await msg.reply_text("⚠️ کلیپ پیدا نشد یا بزرگ‌تر از ۵۰ مگابایته.")
        sid = uuid.uuid4().hex[:10]
        add_sample(sid, "", src)

        # everything below runs at the same time:
        samples_task = asyncio.create_task(asyncio.to_thread(make_samples, path, sid))
        song_task = asyncio.create_task(identify_clip(uid, sid, meta, src, samples_task))
        upload_task = asyncio.create_task(
            upload_video(msg, path, f"⏳ در حال شناسایی موسیقی...\n\n{footer(src)}"))
        audio_task = None
        try:
            song, blocked = await song_task
            if song:
                with db() as c:
                    c.execute("UPDATE samples SET last=? WHERE id=?", (song.get("engine"), sid))
                # start downloading the mp3 while the video is still uploading
                audio_task = asyncio.create_task(prepare_song_audio(song))
            sent = await upload_task

            if song:
                cap, kb = result_caption(song, src), result_keyboard(sid, song)
                fid = _msg_video_id(sent)
                if fid:
                    clip_put(src, fid, song)
            else:
                note = "⛔ سقف تشخیص امروزت پر شده" if blocked else ""
                cap = plain_caption(title, src, note)
                kb = None if blocked else Markup([[Btn("🎵 شناسایی موسیقی", callback_data=f"rec:{sid}")]])
            try:
                await sent.edit_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
            except Exception:
                log.warning("edit caption failed", exc_info=True)

            if audio_task:
                try:
                    a = await audio_task
                    await deliver_audio(msg, a, song)
                except Exception as e:
                    log.exception("clip audio failed")
                    await msg.reply_text(f"❌ خطا: {str(e)[:200]}")
        finally:
            for t in (audio_task, upload_task):
                if t and not t.done():
                    t.cancel()
            await asyncio.gather(samples_task, return_exceptions=True)


# ---------- Voice / audio from user ----------
async def handle_media(msg, media, status):
    if not use_quota(msg.from_user.id):
        return await status.edit_text("⛔ سقف تشخیص امروزت پر شده، فردا دوباره امتحان کن.")
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
        return await status.edit_text("😕 آهنگ رو نشناختم.")
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (song["engine"], sid))
    audio_task = asyncio.create_task(prepare_song_audio(song))
    await status.edit_text(result_caption(song), parse_mode="HTML",
                           reply_markup=result_keyboard(sid, song), **NO_PREVIEW)
    try:
        a = await audio_task
        await deliver_audio(msg, a, song)
    except Exception as e:
        log.exception("mp3 failed")
        await msg.reply_text(f"❌ خطا: {str(e)[:200]}")


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user:
        return
    uid = msg.from_user.id
    touch_user(uid)
    st = ctx.user_data.get("await")
    if st and uid in ADMIN_IDS and msg.text:
        return await admin_input(update, ctx, st)
    status = await msg.reply_text("⏳ در حال پردازش...")
    try:
        media = msg.voice or msg.audio or msg.video_note
        if media:
            await handle_media(msg, media, status)
            return
        text = (msg.text or "").strip()
        if text.startswith(MP3_PREFIX):
            await send_mp3(msg, text[len(MP3_PREFIX):].strip())
        else:
            m = URL_RE.search(text)
            if m and any(h in m.group(0) for h in CLIP_HOSTS):
                await do_clip(msg, m.group(0))
            else:
                target, _ = await asyncio.to_thread(resolve, text)
                await send_mp3(msg, target)
        await status.delete()
    except Exception as e:
        log.exception("handle failed")
        await status.edit_text(f"❌ خطا: {str(e)[:200]}")


# ---------- Buttons ----------
async def on_rec(q, data: str):
    action, sid = data.split(":", 1)
    uid = q.from_user.id
    with db() as c:
        row = c.execute("SELECT * FROM samples WHERE id=?", (sid,)).fetchone()
    if row and action == "bad" and row["src"]:
        clip_delete(row["src"])  # wrong result must not stay in the clip cache
    paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, f"{sid}_*")))
    mpath = os.path.join(SAMPLES_DIR, f"{sid}.json")
    if not row or not (paths or os.path.exists(mpath)):
        return await q.answer("نمونه منقضی شده، لینک یا فایل رو دوباره بفرست.", show_alert=True)
    tried = [t for t in (row["tried"] or "").split(",") if t]
    if action == "bad" and row["last"]:
        tried.append(row["last"])
        with db() as c:
            c.execute("UPDATE samples SET tried=? WHERE id=?", (",".join(tried), sid))
    song = None
    if "meta#0" not in tried and os.path.exists(mpath):
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
            return await q.answer("نتیجه‌ی دیگه‌ای نیست.", show_alert=True)
        if not use_quota(uid):
            return await q.answer("⛔ سقف تشخیص امروزت پر شده.", show_alert=True)
        await q.answer("⏳ در حال شناسایی...")
        song = await asyncio.to_thread(recognize, paths, tuple(tried))
    if not song:
        txt = "😕 آهنگ رو نشناختم." if not tried else "😕 موتور دیگه‌ای برای امتحان نمونده."
        return await q.message.reply_text(txt)
    with db() as c:
        c.execute("UPDATE samples SET last=? WHERE id=?", (song["engine"], sid))
    src = row["src"]
    kb = result_keyboard(sid, song)
    cap = result_caption(song, src)
    audio_task = asyncio.create_task(prepare_song_audio(song))
    try:
        if q.message.text:
            await q.edit_message_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
        else:
            await q.edit_message_caption(caption=cap, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await q.message.reply_text(cap, parse_mode="HTML", reply_markup=kb, **NO_PREVIEW)
    if src and getattr(q.message, "video", None):
        clip_put(src, q.message.video.file_id, song)
    try:
        a = await audio_task
        await deliver_audio(q.message, a, song)
    except Exception as e:
        log.exception("mp3 failed")
        await q.message.reply_text(f"❌ خطا: {str(e)[:200]}")


async def on_lyrics(q, data: str):
    tid = data.split(":", 1)[1]
    with db() as c:
        row = c.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if not row:
        return await q.answer("اطلاعات آهنگ پیدا نشد.", show_alert=True)
    await q.answer("⏳ در حال پیدا کردن متن ترانه...")
    text = await asyncio.to_thread(get_lyrics, row["artist"], row["title"])
    if not text:
        return await q.message.reply_text("😕 متن این آهنگ پیدا نشد.")
    for chunk in split_text(f"🎤 {row['title']}\n\n{text}"):
        await q.message.reply_text(chunk)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    touch_user(q.from_user.id)
    if data.startswith(("rec:", "bad:")):
        return await on_rec(q, data)
    if data.startswith("lyr:"):
        return await on_lyrics(q, data)
    if data.startswith(("adm:", "acc:")):
        if q.from_user.id not in ADMIN_IDS:
            return await q.answer("⛔", show_alert=True)
        return await admin_cb(q, ctx, data)
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


# ---------- Super admin panel ----------
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, ctx)
    return wrapper


BACK = Markup([[Btn("⬅️ بازگشت", callback_data="adm:menu")]])


def admin_menu():
    return Markup([
        [Btn("➕ ACRCloud", callback_data="adm:addacr"), Btn("➕ AudD", callback_data="adm:addaudd")],
        [Btn("📋 اکانت‌ها", callback_data="adm:list")],
        [Btn("⚙️ سقف روزانه", callback_data="adm:limit"), Btn("📊 آمار", callback_data="adm:stats")],
    ])


def stats_text() -> str:
    day = datetime.date.today().isoformat()
    with db() as c:
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM users WHERE first_seen=?", (day,)).fetchone()[0]
        act, rec = c.execute("SELECT COUNT(*), COALESCE(SUM(n),0) FROM usage WHERE day=?", (day,)).fetchone()
        songs = c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]
        clips = c.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        accs = c.execute("SELECT COUNT(*) FROM accounts WHERE enabled=1").fetchone()[0]
    lim = daily_limit()
    return (f"📊 آمار\n👥 کل کاربران: {users}\n🆕 جدید امروز: {new}\n"
            f"🎧 شناسایی امروز: {rec} (از {act} نفر)\n💾 آهنگ‌های کش‌شده: {songs}\n"
            f"🎬 کلیپ‌های کش‌شده: {clips}\n"
            f"🟢 اکانت فعال: {accs}\n⚙️ سقف روزانه: {'نامحدود' if lim <= 0 else lim}")


async def show_accounts(q):
    with db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    if not rows:
        text = "هیچ اکانتی نیست. از منو اضافه کن."
    else:
        text = "\n".join(
            f'{"🟢" if r["enabled"] else "⚪️"} #{r["id"]} {r["type"]} …{(r["key"] or "")[-4:]} | استفاده {r["uses"]} | خطا {r["errors"]}'
            for r in rows)
    kb = [[Btn(f'{"⏸" if r["enabled"] else "▶️"} #{r["id"]}', callback_data=f'acc:t:{r["id"]}'),
           Btn(f'🗑 #{r["id"]}', callback_data=f'acc:d:{r["id"]}')] for r in rows]
    kb.append([Btn("⬅️ بازگشت", callback_data="adm:menu")])
    try:
        await q.edit_message_text(text, reply_markup=Markup(kb))
    except Exception:
        pass


async def admin_cb(q, ctx, data: str):
    await q.answer()
    parts = data.split(":")
    if parts[0] == "adm":
        act = parts[1]
        if act == "menu":
            ctx.user_data.pop("await", None)
            await q.edit_message_text("🛠 پنل مدیریت", reply_markup=admin_menu())
        elif act == "addacr":
            ctx.user_data["await"] = "acr"
            await q.edit_message_text("اطلاعات ACRCloud رو توی یه پیام بفرست:\nHOST ACCESS_KEY ACCESS_SECRET\n(بعد از ذخیره، پیامت پاک میشه)", reply_markup=BACK)
        elif act == "addaudd":
            ctx.user_data["await"] = "audd"
            await q.edit_message_text("توکن AudD رو بفرست:\n(بعد از ذخیره، پیامت پاک میشه)", reply_markup=BACK)
        elif act == "limit":
            ctx.user_data["await"] = "limit"
            await q.edit_message_text(f"سقف روزانه‌ی هر کاربر الان {daily_limit()} هست. عدد جدید رو بفرست (۰ = نامحدود):", reply_markup=BACK)
        elif act == "list":
            await show_accounts(q)
        elif act == "stats":
            await q.edit_message_text(stats_text(), reply_markup=BACK)
    elif parts[0] == "acc":
        _, op, aid = parts
        with db() as c:
            if op == "t":
                c.execute("UPDATE accounts SET enabled=1-enabled WHERE id=?", (int(aid),))
            elif op == "d":
                c.execute("DELETE FROM accounts WHERE id=?", (int(aid),))
        await show_accounts(q)


async def _hide_secret(update: Update):
    try:
        await update.message.delete()
    except Exception:
        pass


async def admin_input(update: Update, ctx, st: str):
    msg = update.message
    parts = msg.text.split()
    ctx.user_data.pop("await", None)
    chat = update.effective_chat.id
    if st == "acr" and len(parts) == 3:
        with db() as c:
            cur = c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", tuple(parts))
        reply = f"✅ ACRCloud اضافه شد (id={cur.lastrowid})"
    elif st == "audd" and len(parts) == 1:
        with db() as c:
            cur = c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (parts[0],))
        reply = f"✅ AudD اضافه شد (id={cur.lastrowid})"
    elif st == "limit" and len(parts) == 1 and parts[0].isdigit():
        set_setting("daily_limit", int(parts[0]))
        reply = f"✅ سقف روزانه شد {parts[0]}"
    else:
        return await msg.reply_text("❌ فرمت اشتباه بود، دوباره از پنل شروع کن.", reply_markup=admin_menu())
    await _hide_secret(update)
    await ctx.bot.send_message(chat, reply, reply_markup=admin_menu())


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    touch_user(update.effective_user.id)
    await update.message.reply_text(
        "سلام 👋\nلینک یوتیوب یا اینستاگرام بفرست تا کلیپش رو بگیری و بتونی موسیقی‌اش رو شناسایی کنی.\n"
        "ویس هم می‌تونی بفرستی، یا اسم آهنگ رو تایپ کن.")


@admin_only
async def cmd_admin(update, ctx):
    await update.message.reply_text("🛠 پنل مدیریت", reply_markup=admin_menu())


@admin_only
async def cmd_addacr(update, ctx):
    if len(ctx.args) != 3:
        return await update.message.reply_text("/addacr HOST ACCESS_KEY ACCESS_SECRET")
    with db() as c:
        cur = c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", tuple(ctx.args))
    await _hide_secret(update)
    await ctx.bot.send_message(update.effective_chat.id, f"✅ ACRCloud اضافه شد (id={cur.lastrowid})")


@admin_only
async def cmd_addaudd(update, ctx):
    if len(ctx.args) != 1:
        return await update.message.reply_text("/addaudd API_TOKEN")
    with db() as c:
        cur = c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (ctx.args[0],))
    await _hide_secret(update)
    await ctx.bot.send_message(update.effective_chat.id, f"✅ AudD اضافه شد (id={cur.lastrowid})")


@admin_only
async def cmd_accounts(update, ctx):
    with db() as c:
        rows = c.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    if not rows:
        return await update.message.reply_text("هیچ اکانتی نیست. با /admin اضافه کن.")
    lines = [f'{"🟢" if r["enabled"] else "⚪️"} #{r["id"]} {r["type"]} …{(r["key"] or "")[-4:]} | استفاده {r["uses"]} | خطا {r["errors"]}' for r in rows]
    await update.message.reply_text("\n".join(lines))


@admin_only
async def cmd_remove(update, ctx):
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("/remove ID")
    with db() as c:
        n = c.execute("DELETE FROM accounts WHERE id=?", (int(ctx.args[0]),)).rowcount
    await update.message.reply_text("🗑 حذف شد" if n else "پیدا نشد")


@admin_only
async def cmd_toggle(update, ctx):
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("/toggle ID")
    with db() as c:
        n = c.execute("UPDATE accounts SET enabled=1-enabled WHERE id=?", (int(ctx.args[0]),)).rowcount
    await update.message.reply_text("✅ تغییر کرد" if n else "پیدا نشد")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.from_user or msg.from_user.id not in ADMIN_IDS:
        return
    doc = msg.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        return
    if (doc.file_size or 0) > 3 * 1024 * 1024:
        return await msg.reply_text("❌ فایل خیلی بزرگه.")
    tmp = COOKIES + ".tmp"
    await (await doc.get_file()).download_to_drive(tmp)
    with open(tmp, encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if "youtube.com" not in content and "instagram.com" not in content:
        os.remove(tmp)
        return await msg.reply_text("❌ این فایل کوکی یوتیوب یا اینستاگرام نیست.")
    os.replace(tmp, COOKIES)
    await msg.reply_text("✅ کوکی ذخیره شد، لینک رو دوباره امتحان کن.")
    try:
        await msg.delete()
    except Exception:
        pass


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("update error", exc_info=ctx.error)


def main():
    migrate()
    seed_from_env()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    for name, fn in [("start", cmd_start), ("admin", cmd_admin), ("addacr", cmd_addacr),
                     ("addaudd", cmd_addaudd), ("accounts", cmd_accounts),
                     ("remove", cmd_remove), ("toggle", cmd_toggle)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
        handle,
    ))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
