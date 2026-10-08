import json, asyncio, sqlite3, datetime, os, re, tempfile, glob, time, hmac, hashlib, base64, uuid, subprocess, logging
from urllib.parse import quote
import requests
import yt_dlp
from telegram import (Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup,
                      InlineQueryResultArticle, InputTextMessageContent)
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          InlineQueryHandler, filters, ContextTypes)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ACR_HOST = os.environ.get("ACR_HOST")
ACR_KEY = os.environ.get("ACR_KEY")
ACR_SECRET = os.environ.get("ACR_SECRET")
AUDD_TOKEN = os.environ.get("AUDD_TOKEN")
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
    if ext.get("spotify", {}).get("track", {}).get("id"):
        link = "https://open.spotify.com/track/" + ext["spotify"]["track"]["id"]
    elif ext.get("youtube", {}).get("vid"):
        link = "https://www.youtube.com/watch?v=" + ext["youtube"]["vid"]
    return {"title": m["title"], "artist": m["artists"][0]["name"], "link": link}


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
def odesli_to_youtube(link: str):
    r = requests.get("https://api.song.link/v1-alpha.1/links", params={"url": link}, timeout=20)
    if not r.ok:
        return None, None
    j = r.json()
    plat = j.get("linksByPlatform", {})
    yt = (plat.get("youtube") or plat.get("youtubeMusic") or {}).get("url")
    ent = j.get("entitiesByUniqueId", {}).get(j.get("entityUniqueId"), {})
    name = f"{ent.get('artistName', '')} - {ent.get('title', '')}".strip(" -")
    return yt, name


# ---------- yt-dlp ----------
def ydl_base():
    o = {"quiet": True, "noplaylist": True}
    if os.path.exists(COOKIES):
        o["cookiefile"] = COOKIES
    return o


def download_mp3(target: str, outdir: str, fallback=None):
    if not URL_RE.match(target) and not target.startswith("ytsearch"):
        target = f"ytsearch1:{target}"
    opts = {**ydl_base(), "format": "bestaudio/best",
            "outtmpl": f"{outdir}/%(title).80s.%(ext)s",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}

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
    return glob.glob(f"{outdir}/*.mp3")[0], info.get("title", "music")


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
    outs, seen = [], set()
    for i, o in enumerate(offs):
        o = round(o)
        if o in seen:
            continue
        seen.add(o)
        out = os.path.join(SAMPLES_DIR, f"{sid}_{i}.mp3")
        if cut_sample(src, out, o, win):
            outs.append(out)
    return outs


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
        yt, name = odesli_to_youtube(url)
        if not yt and not name:
            raise ValueError("این لینک پشتیبانی نمیشه")
        return (yt or f"ytsearch1:{name}"), name
    return text, text


# ---------- SQLite ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, host TEXT, key TEXT, secret TEXT,
        enabled INTEGER DEFAULT 1, uses INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS songs(key TEXT PRIMARY KEY, file_id TEXT, title TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS usage(uid INTEGER, day TEXT, n INTEGER, PRIMARY KEY(uid, day))")
    c.execute("CREATE TABLE IF NOT EXISTS users(uid INTEGER PRIMARY KEY, first_seen TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS samples(
        id TEXT PRIMARY KEY, path TEXT, tried TEXT DEFAULT '', last TEXT, created INTEGER)""")
    return c


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


def cache_get(key):
    with db() as c:
        return c.execute("SELECT file_id, title FROM songs WHERE key=?", (key,)).fetchone()


def cache_put(key, file_id, title):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO songs VALUES(?,?,?)", (key, file_id, title))


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


def add_sample(sid: str, path: str):
    with db() as c:
        c.execute("INSERT INTO samples(id,path,created) VALUES(?,?,?)", (sid, path, int(time.time())))


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


# ---------- Sending ----------
async def send_mp3(msg, target: str, title=None, performer=None):
    hit = cache_get(target)
    if hit:
        return await msg.reply_audio(hit[0], title=title or hit[1], performer=performer)
    with tempfile.TemporaryDirectory() as tmp:
        fb = f"{performer} - {title}" if (title and performer) else (target[10:] if target.startswith("ytsearch1:") else None)
        path, ytitle = await asyncio.to_thread(download_mp3, target, tmp, fb)
        with open(path, "rb") as f:
            sent = await msg.reply_audio(f, title=title or ytitle, performer=performer,
                                         read_timeout=300, write_timeout=300)
    if sent.audio:
        cache_put(target, sent.audio.file_id, title or ytitle)


async def send_song_mp3(msg, song: dict):
    target = None
    if song.get("link"):
        try:
            target, _ = await asyncio.to_thread(odesli_to_youtube, song["link"])
        except Exception:
            target = None
    target = target or f"ytsearch1:{song['artist']} - {song['title']}"
    await send_mp3(msg, target, title=song["title"], performer=song["artist"])


async def do_clip(msg, url: str):
    cleanup_samples()
    with tempfile.TemporaryDirectory() as tmp:
        path, title, meta = await asyncio.to_thread(download_clip, url, tmp)
        if not path:
            return await msg.reply_text("⚠️ کلیپ پیدا نشد یا بزرگ‌تر از ۵۰ مگابایته.")
        sid = uuid.uuid4().hex[:10]
        outs = await asyncio.to_thread(make_samples, path, sid)
        kb = None
        if outs or meta:
            if meta:
                if "youtu" in url:
                    meta["link"] = url
                with open(os.path.join(SAMPLES_DIR, sid + ".json"), "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False)
            add_sample(sid, outs[0] if outs else "")
            kb = Markup([[Btn("🎵 شناسایی موسیقی", callback_data=f"rec:{sid}")]])
        with open(path, "rb") as f:
            await msg.reply_video(f, caption=title[:200], reply_markup=kb, supports_streaming=True,
                                  read_timeout=300, write_timeout=300)


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
    caption = f"🎵 {song['artist']} - {song['title']}\n({song['engine']})"
    await status.edit_text(caption, reply_markup=result_keyboard(sid, song))
    await send_song_mp3(msg, song)


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
    paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, f"{sid}_*")))
    mpath = os.path.join(SAMPLES_DIR, f"{sid}.json")
    if not row or not (paths or os.path.exists(mpath)):
        return await q.answer("نمونه منقضی شده، دوباره بفرست.", show_alert=True)
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
    caption = f"🎵 {song['artist']} - {song['title']}\n({song['engine']})"
    kb = result_keyboard(sid, song)
    try:
        if q.message.text:
            await q.edit_message_text(caption, reply_markup=kb)
        else:
            await q.edit_message_caption(caption=caption, reply_markup=kb)
    except Exception:
        await q.message.reply_text(caption, reply_markup=kb)
    try:
        await send_song_mp3(q.message, song)
    except Exception as e:
        log.exception("mp3 failed")
        await q.message.reply_text(f"❌ خطا: {str(e)[:200]}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    touch_user(q.from_user.id)
    if data.startswith(("rec:", "bad:")):
        return await on_rec(q, data)
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
        accs = c.execute("SELECT COUNT(*) FROM accounts WHERE enabled=1").fetchone()[0]
    lim = daily_limit()
    return (f"📊 آمار\n👥 کل کاربران: {users}\n🆕 جدید امروز: {new}\n"
            f"🎧 شناسایی امروز: {rec} (از {act} نفر)\n💾 آهنگ‌های کش‌شده: {songs}\n"
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
