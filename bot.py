import asyncio, sqlite3, datetime, os, re, tempfile, glob, time, hmac, hashlib, base64
import requests
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = os.environ["BOT_TOKEN"]
ACR_HOST = os.environ.get("ACR_HOST")   # e.g. identify-eu-west-1.acrcloud.com
ACR_KEY = os.environ.get("ACR_KEY")
ACR_SECRET = os.environ.get("ACR_SECRET")
AUDD_TOKEN = os.environ.get("AUDD_TOKEN")
ENGINE_ORDER = os.environ.get("ENGINES", "acr,audd").split(",")  # order of fallback
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "20"))            # 0 = unlimited
DB_PATH = os.environ.get("DB_PATH", "bot.db")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

URL_RE = re.compile(r"https?://\S+")
DIRECT_HOSTS = ("youtube.com", "youtu.be", "instagram.com", "music.youtube.com")


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
        data={
            "access_key": acc["key"], "data_type": "audio", "signature_version": "1",
            "signature": sig, "sample_bytes": len(data), "timestamp": ts,
        },
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


# ---------- Odesli: any music link -> YouTube link ----------
def odesli_to_youtube(link: str):
    r = requests.get(
        "https://api.song.link/v1-alpha.1/links", params={"url": link}, timeout=20
    )
    if not r.ok:
        return None, None
    j = r.json()
    plat = j.get("linksByPlatform", {})
    yt = (plat.get("youtube") or plat.get("youtubeMusic") or {}).get("url")
    ent = j.get("entitiesByUniqueId", {}).get(j.get("entityUniqueId"), {})
    name = f"{ent.get('artistName', '')} - {ent.get('title', '')}".strip(" -")
    return yt, name


# ---------- yt-dlp ----------
def download_mp3(target: str, outdir: str):
    if not URL_RE.match(target):
        target = f"ytsearch1:{target}"
    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{outdir}/%(title).80s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=True)
        if "entries" in info:
            info = info["entries"][0]
    return glob.glob(f"{outdir}/*.mp3")[0], info.get("title", "music")


def resolve(text: str):
    """Decide what yt-dlp should download. Returns (target, label)."""
    if URL_RE.match(text):
        if any(h in text for h in DIRECT_HOSTS):
            return text, None
        yt, name = odesli_to_youtube(text)      # Spotify, Apple Music, Deezer...
        return (yt or f"ytsearch1:{name}"), name
    return text, text                            # plain text search


ENGINE_FN = {"acr": acr_identify, "audd": audd_identify}


def recognize(path: str):
    """Types are tried in ENGINE_ORDER. Inside a type, move to the next account only on errors."""
    with db() as c:
        rows = c.execute("SELECT * FROM accounts WHERE enabled=1").fetchall()
    order = {n: i for i, n in enumerate(ENGINE_ORDER)}
    rows = sorted(rows, key=lambda r: (order.get(r["type"], 99), r["id"]))
    answered = set()
    for r in rows:
        if r["type"] in answered or r["type"] not in ENGINE_FN:
            continue
        try:
            res = ENGINE_FN[r["type"]](path, r)
            answered.add(r["type"])
            with db() as c:
                c.execute("UPDATE accounts SET uses=uses+1 WHERE id=?", (r["id"],))
            if res:
                res["engine"] = f'{r["type"]}#{r["id"]}'
                return res
        except Exception as e:
            print("account", r["id"], "failed:", e)
            with db() as c:
                c.execute("UPDATE accounts SET errors=errors+1 WHERE id=?", (r["id"],))
    return None


# ---------- SQLite: cache + daily limit ----------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT, host TEXT, key TEXT, secret TEXT,
        enabled INTEGER DEFAULT 1, uses INTEGER DEFAULT 0, errors INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS songs(key TEXT PRIMARY KEY, file_id TEXT, title TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS usage(uid INTEGER, day TEXT, n INTEGER, PRIMARY KEY(uid, day))")
    return c


def seed_from_env():
    with db() as c:
        if c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
            return
        if all([ACR_HOST, ACR_KEY, ACR_SECRET]):
            c.execute("INSERT INTO accounts(type,host,key,secret) VALUES('acr',?,?,?)", (ACR_HOST, ACR_KEY, ACR_SECRET))
        if AUDD_TOKEN:
            c.execute("INSERT INTO accounts(type,key) VALUES('audd',?)", (AUDD_TOKEN,))


def cache_get(key):
    with db() as c:
        return c.execute("SELECT file_id, title FROM songs WHERE key=?", (key,)).fetchone()


def cache_put(key, file_id, title):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO songs VALUES(?,?,?)", (key, file_id, title))


def use_quota(uid: int) -> bool:
    """Counts one recognition for today; False if over the limit."""
    if DAILY_LIMIT <= 0:
        return True
    day = datetime.date.today().isoformat()
    with db() as c:
        row = c.execute("SELECT n FROM usage WHERE uid=? AND day=?", (uid, day)).fetchone()
        n = row[0] if row else 0
        if n >= DAILY_LIMIT:
            return False
        c.execute("INSERT OR REPLACE INTO usage VALUES(?,?,?)", (uid, day, n + 1))
    return True


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    status = await msg.reply_text("⏳ در حال پردازش...")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            media = msg.voice or msg.audio or msg.video_note
            if media:
                if not use_quota(msg.from_user.id):
                    return await status.edit_text("⛔ سقف تشخیص امروزت پر شده، فردا دوباره امتحان کن.")
                src = os.path.join(tmp, "in.ogg")
                await (await media.get_file()).download_to_drive(src)
                song = await asyncio.to_thread(recognize, src)
                os.remove(src)
                if not song:
                    return await status.edit_text("😕 آهنگ رو نشناختم.")
                label = f"{song['artist']} - {song['title']}"
                await status.edit_text(f"🎵 {label}\n({song['engine']})\nدر حال دانلود...")
                target = None
                if song["link"]:
                    target, _ = await asyncio.to_thread(odesli_to_youtube, song["link"])
                target = target or f"ytsearch1:{label}"
            else:
                target, _ = await asyncio.to_thread(resolve, msg.text.strip())

            hit = cache_get(target)
            if hit:
                await status.delete()
                return await msg.reply_audio(hit[0], title=hit[1])
            path, title = await asyncio.to_thread(download_mp3, target, tmp)
            await status.delete()
            with open(path, "rb") as f:
                sent = await msg.reply_audio(f, title=title)
            if sent.audio:
                cache_put(target, sent.audio.file_id, title)
    except Exception as e:
        await status.edit_text(f"❌ خطا: {str(e)[:200]}")


# ---------- Admin panel ----------
def admin_only(fn):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id not in ADMIN_IDS:
            return
        return await fn(update, ctx)
    return wrapper


async def _hide_secret(update: Update):
    try:
        await update.message.delete()   # remove the message containing keys
    except Exception:
        pass


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
        return await update.message.reply_text("هیچ اکانتی نیست. با /addacr یا /addaudd اضافه کن.")
    lines = [
        f'{"🟢" if r["enabled"] else "⚪️"} #{r["id"]} {r["type"]} …{(r["key"] or "")[-4:]} | استفاده {r["uses"]} | خطا {r["errors"]}'
        for r in rows
    ]
    await update.message.reply_text("\n".join(lines) + "\n\n/toggle ID  |  /remove ID")


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


def main():
    seed_from_env()
    app = Application.builder().token(BOT_TOKEN).build()
    for name, fn in [("addacr", cmd_addacr), ("addaudd", cmd_addaudd), ("accounts", cmd_accounts),
                     ("remove", cmd_remove), ("toggle", cmd_toggle)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE,
        handle,
    ))
    app.run_polling()


if __name__ == "__main__":
    main()
