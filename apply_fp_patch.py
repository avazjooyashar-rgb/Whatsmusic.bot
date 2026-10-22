#!/usr/bin/env python3
"""
apply_fp_patch.py - wires our own fingerprint engine (fp_engine.py) into bot.py.

Run from the bot folder:   ./venv/bin/python apply_fp_patch.py
 - every edit must match EXACTLY ONE place in bot.py, otherwise nothing is written
 - bot.py is backed up to bot.py.before_fp
 - the result is syntax-checked before it replaces bot.py
"""
import os
import py_compile
import shutil
import sys
import tempfile

BOT = "bot.py"
EDITS = []


def edit(old, new):
    EDITS.append((old, new))


# 1) config + pools -------------------------------------------------------------------
edit('''ENGINE_POOL = ThreadPoolExecutor(max_workers=ENGINE_THREADS, thread_name_prefix="engine")
''', '''ENGINE_POOL = ThreadPoolExecutor(max_workers=ENGINE_THREADS, thread_name_prefix="engine")

# ---- our own fingerprint engine (fp_engine.py): asked BEFORE every limited / paid engine ----
FP_ENABLED = os.environ.get("FP_ENABLED", "1") == "1"
FP_MIN_SCORE = int(os.environ.get("FP_MIN_SCORE", "25"))  # aligned hashes needed to trust our own DB
FP_LEARN = os.environ.get("FP_LEARN", "1") == "1"  # store hashes of songs the other engines agreed on
FP_LEARN_PIECES = int(os.environ.get("FP_LEARN_PIECES", "3"))  # how many pieces of a clip are learned
FP_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="fp")  # learning: one at a time, off the hot path
_FP_LOCK = threading.Lock()  # one sqlite connection is shared, so access is serialized
_FP = {"engine": None, "failed": False}
''')

# 2) own-engine functions, placed right before recognize() -------------------------------
edit('''def recognize(paths, skip=(), extra=None):''', '''# ======================================================================================
# Our own fingerprint engine
# ======================================================================================
def _fp_engine():
    # created on first use; None when fp_engine.py / numpy / scipy are missing (the bot keeps working)
    if not FP_ENABLED or _FP["failed"]:
        return None
    if _FP["engine"] is None:
        try:
            from fp_engine import Engine
            _FP["engine"] = Engine(os.environ.get("FP_DB", "fingerprints.db"))
        except Exception:
            _FP["failed"] = True
            log.exception("own fingerprint engine disabled (import failed)")
            return None
    return _FP["engine"]


def fp_identify(paths, rejected=()):
    # Looks the pieces up in our own database. Free, instant, uses no quota of any API.
    eng = _fp_engine()
    if not eng:
        return None
    best = None
    for p in paths[:3]:
        try:
            with _FP_LOCK:
                r = eng.recognize(p)
        except Exception as e:
            log.warning("own engine failed on a piece: %s", e)
            continue
        if r:
            log.info("own engine: %s - %s score=%s", r["artist"], r["title"], r["score"])
        if r and r["score"] >= FP_MIN_SCORE and (best is None or r["score"] > best["score"]):
            best = r
    if not best or title_key(best["title"]) in rejected:
        return None
    return {"title": best["title"], "artist": best["artist"], "link": None, "engine": "local#0"}


def _fp_trusted(hits, top) -> bool:
    # Learn only what real engines confirmed: two different engines, or two different pieces.
    # Instagram / YouTube name hints, lyrics guesses and melody (humming) matches are never learned.
    real = [h for h in hits
            if _base(h["engine"]) in ("acr", "shazam", "audd")
            and h["song"].get("wmul") is None
            and same_song(h["song"], top)]
    return len({_base(h["engine"]) for h in real}) >= 2 or len({h["sample"] for h in real}) >= 2


def fp_learn_async(paths, song):
    # Store the fingerprints (never the audio) of the pieces under the confirmed song.
    if not FP_LEARN or not _fp_engine():
        return
    title, artist = song.get("title"), song.get("artist")
    pieces = list(paths[:FP_LEARN_PIECES])
    if not (title and artist and pieces):
        return

    def job():
        for p in pieces:
            try:
                with _FP_LOCK:
                    _fp_engine().learn(p, title, artist, source="api")
            except Exception as e:
                log.warning("own engine learn failed: %s", e)

    FP_POOL.submit(job)


def fp_forget(key: str):
    # The user pressed "wrong song": delete every entry with that title from our own database.
    eng = _fp_engine()
    if not eng or not key:
        return
    with _FP_LOCK:
        conn = eng.db.conn
        ids = [r[0] for r in conn.execute("SELECT id, title FROM tracks").fetchall()
               if title_key(r[1]) == key]
        if ids:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fp_track ON fingerprints(track_id)")
            for i in ids:
                conn.execute("DELETE FROM fingerprints WHERE track_id=?", (i,))
                conn.execute("DELETE FROM tracks WHERE id=?", (i,))
            conn.commit()
            log.info("own engine: forgot %d wrong entr%s", len(ids), "y" if len(ids) == 1 else "ies")


def _fp_stats():
    eng = _fp_engine()
    if not eng:
        return None
    with _FP_LOCK:
        return eng.db.stats()


def recognize(paths, skip=(), extra=None):''')

# 3) local lookup is the very first stage of recognize() --------------------------------
edit('''    rejected = {t[2:] for t in skip if t.startswith("k:")}  # songs the user said are wrong
''', '''    rejected = {t[2:] for t in skip if t.startswith("k:")}  # songs the user said are wrong
    if "local#0" not in skip:  # our own database first: free, instant, no API quota
        own = fp_identify(paths, rejected)
        if own:
            own["alts"] = []
            return own
''')

# 4) learn from confirmed answers ---------------------------------------------------------
edit('''    top = ranked[0]["song"]
    top["alts"] = [r["song"] for r in ranked[1:4]]
    return top
''', '''    top = ranked[0]["song"]
    if (top.get("engine") != "local#0" and ranked[0]["solid"] and not ranked[0]["bad"]
            and _fp_trusted(hits, top)):
        fp_learn_async(paths, top)  # background: the next person asking for this song is answered locally
    top["alts"] = [r["song"] for r in ranked[1:4]]
    return top
''')

# 5) "wrong song" also purges the entry from our own database -----------------------------
edit('''        if key:
            tried.append("k:" + key)  # this song is wrong, whoever suggested it
''', '''        if key:
            tried.append("k:" + key)  # this song is wrong, whoever suggested it
            await asyncio.to_thread(fp_forget, key)  # ...and it must not come back from our own database
''')

# 6) admin command /fpstats -----------------------------------------------------------------
edit('''async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):''', '''async def cmd_fpstats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or u.id not in ADMIN_IDS:
        return
    s = await asyncio.to_thread(_fp_stats)
    if not s:
        return await update.message.reply_text("own engine: off")
    await update.message.reply_text(f"own engine: {s['tracks']} tracks, {s['hashes']} hashes")


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):''')

edit('''    for name, fn in [("start", cmd_start), ("lang", cmd_lang)]:''',
     '''    for name, fn in [("start", cmd_start), ("lang", cmd_lang), ("fpstats", cmd_fpstats)]:''')

edit('''    ENGINE_POOL.shutdown(wait=False, cancel_futures=True)
''', '''    ENGINE_POOL.shutdown(wait=False, cancel_futures=True)
    FP_POOL.shutdown(wait=False, cancel_futures=True)
''')


def main():
    if not os.path.exists(BOT):
        sys.exit("bot.py not found: run this from the bot folder")
    src = open(BOT, encoding="utf-8").read()
    if "fp_identify(" in src:
        sys.exit("bot.py already contains the patch: nothing to do")
    for n, (old, new) in enumerate(EDITS, 1):
        c = src.count(old)
        if c != 1:
            sys.exit(f"edit #{n} matches {c} places (must be exactly 1) - bot.py was NOT changed. "
                     f"Send me this line: {old.strip().splitlines()[0][:100]}")
        src = src.replace(old, new)
    fd, tmp = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    try:
        py_compile.compile(tmp, doraise=True)
    except py_compile.PyCompileError as e:
        sys.exit(f"patched file has a syntax error, bot.py was NOT changed:\n{e}")
    finally:
        os.remove(tmp)
    shutil.copy(BOT, BOT + ".before_fp")
    with open(BOT, "w", encoding="utf-8") as f:
        f.write(src)
    if os.path.exists("requirements.txt"):
        req = open("requirements.txt", encoding="utf-8").read()
        add = [p for p in ("numpy", "scipy") if p not in req.lower()]
        if add:
            with open("requirements.txt", "a", encoding="utf-8") as f:
                f.write(("" if req.endswith("\n") else "\n") + "\n".join(add) + "\n")
    print(f"OK: {len(EDITS)} edits applied. Backup: {BOT}.before_fp")


if __name__ == "__main__":
    main()
