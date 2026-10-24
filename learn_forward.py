"""
learn_forward.py - teach the bot songs by forwarding them to its private chat (hashes only, no api_id needed).

Usage in Telegram (only the owner):
    /learn on    -> every audio you send/forward is fingerprinted and learned (not recognised)
    /learn off   -> normal behaviour again
A summary with the learned names is sent after you stop forwarding.

NAMES: channel tags are often broken ("To Aziz Delami To Chesmat ~ UpMusic"), so a 20 s piece of every song is
also sent to Shazam and ITS name is stored. The tag name is used only when Shazam does not know the song (marked
with ⚠️ in the summary) or when the tag says remix / slowed / sped up (those are kept on purpose).
Set LEARN_USE_SHAZAM=0 to always use the tags.

Hook it into bot.py (apply_learn_patch.py does it):
    import learn_forward
    learn_forward.register(app)

Who may teach the bot: LEARN_OWNER_ID (comma separated ids) if set, otherwise ADMIN_IDS from the env.
Bots cannot download files bigger than 20 MB: those are reported as failed.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
import shutil

from telegram import Update
from telegram.ext import (ApplicationHandlerStop, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from fp_engine import Engine, fingerprint_file
from ingest_channel import clean_name, name_from_file

log = logging.getLogger(__name__)


def _owner_ids() -> set:
    raw = os.environ.get("LEARN_OWNER_ID") or os.environ.get("ADMIN_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()}


OWNER_IDS = _owner_ids()
DB = os.environ.get("FP_DB", "fingerprints.db")
MIN_SEC = int(os.environ.get("LEARN_MIN_SEC", "60"))
MAX_SEC = int(os.environ.get("LEARN_MAX_SEC", "720"))
USE_SHAZAM = os.environ.get("LEARN_USE_SHAZAM", "1") == "1"
SUMMARY_DELAY = 8  # seconds of silence before the summary is sent
MAX_NAMES = 25     # names listed in one summary

# versions the owner put in the channel on purpose: keep their tag name, Shazam would name the original
_KEEP_TAG = re.compile(r"remix|slowed|sped\s?up|speed\s?up|reverb|nightcore|karaoke|ریمیکس|ریمیک|اسلو", re.I)

_eng = None
_sh = None
_SEM = asyncio.Semaphore(2)  # bot.py runs with concurrent_updates: never process 50 forwards at once


def _engine() -> Engine:
    global _eng
    if _eng is None:
        _eng = Engine(DB)
        _eng.db.conn.execute("PRAGMA busy_timeout=60000")
    return _eng


def _learn(path: str, title: str, artist: str) -> int:
    hashes = fingerprint_file(path)
    if not hashes:
        raise ValueError("no fingerprints (silent / too short)")
    return _engine().ingest_verified(hashes, title, artist or "Unknown")


def _cut(src: str, out: str, dur: int):
    start = max(0, (dur or 60) / 2 - 10)
    subprocess.run(["ffmpeg", "-y", "-ss", str(start), "-i", src, "-t", "20", "-vn", "-ac", "1",
                    "-ar", "44100", "-b:a", "128k", out],
                   check=True, capture_output=True, timeout=120)


async def shazam_name(path: str, dur: int):
    """(title, artist) from Shazam for a 20 s piece from the middle of the song, or None."""
    global _sh
    try:
        from shazamio import Shazam
    except Exception:
        return None
    piece = path + "_s.mp3"
    try:
        await asyncio.to_thread(_cut, path, piece, dur)
        if _sh is None:
            _sh = Shazam()
        fn = getattr(_sh, "recognize", None) or getattr(_sh, "recognize_song")
        out = await asyncio.wait_for(fn(piece), timeout=40)
        t = (out or {}).get("track")
        if t and t.get("title"):
            return t["title"], t.get("subtitle", "")
    except Exception as e:
        log.info("learn_forward: shazam failed: %s", e)
    return None


async def _process(att, tmp: str):
    """Returns (kind, label); kind is added / had / skipped / failed."""
    fname = getattr(att, "file_name", "") or ""
    dur = getattr(att, "duration", 0) or 0
    if dur and not (MIN_SEC <= dur <= MAX_SEC):
        return "skipped", ""
    title = clean_name(getattr(att, "title", "") or "")
    artist = clean_name(getattr(att, "performer", "") or "")
    if not title:
        title, artist2 = name_from_file(fname)
        artist = artist or artist2
    tgfile = await att.get_file()          # fails above 20 MB
    path = os.path.join(tmp, "a")
    await tgfile.download_to_drive(path)
    flag = ""
    if USE_SHAZAM and not (title and _KEEP_TAG.search(title)):
        got = await shazam_name(path, dur)
        if got:
            title, artist = got
        else:
            flag = "⚠️ "                   # Shazam does not know it: the tag name is kept
    if not title:
        return "skipped", ""
    if artist.strip().lower() == title.strip().lower():
        artist = ""                        # broken tags: same text in both fields
    n = await asyncio.to_thread(_learn, path, title, artist)
    label = f"{flag}{artist or 'Unknown'} - {title}"
    return ("added" if n else "had"), label


async def _send_summary(bot, chat_id: int, stats: dict):
    await asyncio.sleep(SUMMARY_DELAY)
    names = stats["names"][:MAX_NAMES]
    more = len(stats["names"]) - len(names)
    text = (f"✅ یاد گرفت: {stats['added']}\n"
            f"♻️ از قبل داشت: {stats['had']}\n"
            f"⏭ رد شد: {stats['skipped']}\n"
            f"❌ خطا: {stats['failed']}")
    if names:
        text += "\n\n" + "\n".join(names) + (f"\n… و {more} مورد دیگر" if more > 0 else "")
        if any(n.startswith("⚠️") for n in names):
            text += "\n\n⚠️ = شازم نشناخت، اسم از تگ فایل گرفته شد"
    if stats["errors"]:
        text += "\n\n" + "\n".join(stats["errors"][:5])
    for k in ("added", "had", "skipped", "failed"):
        stats[k] = 0
    stats["names"] = []
    stats["errors"] = []
    await bot.send_message(chat_id, text[:4000])


async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        state = "روشن" if context.bot_data.get("learn_mode") else "خاموش"
        await update.effective_message.reply_text(f"حالت یادگیری: {state}\n/learn on  یا  /learn off")
        return
    context.bot_data["learn_mode"] = arg == "on"
    await update.effective_message.reply_text(
        "حالت یادگیری روشن شد. آهنگ‌ها رو فوروارد کن." if arg == "on" else "حالت یادگیری خاموش شد.")


async def on_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.bot_data.get("learn_mode"):
        return  # normal recognition flow continues
    msg = update.effective_message
    att = msg.audio or msg.document
    stats = context.bot_data.setdefault(
        "learn_stats", {"added": 0, "had": 0, "skipped": 0, "failed": 0, "names": [], "errors": []})
    tmp = tempfile.mkdtemp(prefix="fwd_")
    try:
        async with _SEM:
            kind, label = await _process(att, tmp)
    except Exception as e:
        kind, label = "failed", ""
        stats["errors"].append(f"{getattr(att, 'file_name', '') or msg.message_id}: {str(e)[:80]}")
        log.warning("learn_forward failed: %s", e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    stats[kind] += 1
    if label and kind == "added":
        stats["names"].append(label)
    task = context.bot_data.get("learn_task")
    if task and not task.done():
        task.cancel()
    context.bot_data["learn_task"] = asyncio.create_task(
        _send_summary(context.bot, msg.chat_id, stats))
    raise ApplicationHandlerStop  # never run normal recognition on a file sent in learn mode


def register(app):
    if not OWNER_IDS:
        log.warning("learn_forward: no LEARN_OWNER_ID / ADMIN_IDS, handler disabled")
        return
    owner = filters.User(user_id=list(OWNER_IDS)) & filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("learn", cmd_learn, filters=owner), group=-1)
    app.add_handler(MessageHandler(owner & (filters.AUDIO | filters.Document.AUDIO), on_audio), group=-1)
    log.info("learn_forward: enabled for %d user(s)", len(OWNER_IDS))
