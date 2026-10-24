"""
learn_forward.py - teach the bot songs by forwarding them to its private chat (hashes only, no api_id needed).

Usage in Telegram (only the owner):
    /learn on    -> every audio you send/forward is fingerprinted and learned (not recognised)
    /learn off   -> normal behaviour again
A short summary is sent after you stop forwarding.

Hook it into bot.py (after the Application object exists):
    import learn_forward
    learn_forward.register(app)        # 'app' = your Application variable

Who may teach the bot: LEARN_OWNER_ID (comma separated ids) if set, otherwise ADMIN_IDS from the env.
Bots cannot download files bigger than 20 MB: those are reported as failed.
"""
import asyncio
import logging
import os
import shutil
import tempfile

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
SUMMARY_DELAY = 8  # seconds of silence before the summary is sent

_eng = None
_lock = asyncio.Lock()


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


async def _send_summary(bot, chat_id: int, stats: dict):
    await asyncio.sleep(SUMMARY_DELAY)
    text = (f"✅ یاد گرفت: {stats['added']}\n"
            f"♻️ از قبل داشت: {stats['had']}\n"
            f"⏭ رد شد: {stats['skipped']}\n"
            f"❌ خطا: {stats['failed']}")
    if stats["errors"]:
        text += "\n\n" + "\n".join(stats["errors"][:5])
    for k in ("added", "had", "skipped", "failed"):
        stats[k] = 0
    stats["errors"] = []
    await bot.send_message(chat_id, text)


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
        "learn_stats", {"added": 0, "had": 0, "skipped": 0, "failed": 0, "errors": []})
    try:
        fname = getattr(att, "file_name", "") or ""
        dur = getattr(att, "duration", 0) or 0
        if dur and not (MIN_SEC <= dur <= MAX_SEC):
            stats["skipped"] += 1
            return
        title = clean_name(getattr(att, "title", "") or "")
        artist = clean_name(getattr(att, "performer", "") or "")
        if not title:
            title, artist2 = name_from_file(fname)
            artist = artist or artist2
        if not title:
            stats["skipped"] += 1
            return
        tmp = tempfile.mkdtemp(prefix="fwd_")
        try:
            tgfile = await att.get_file()          # fails above 20 MB
            path = os.path.join(tmp, "a")
            await tgfile.download_to_drive(path)
            async with _lock:
                n = await asyncio.to_thread(_learn, path, title, artist)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        stats["added" if n else "had"] += 1
    except Exception as e:
        stats["failed"] += 1
        stats["errors"].append(f"{(att.file_name if att else '?') or msg.message_id}: {str(e)[:80]}")
        log.warning("learn_forward failed: %s", e)
    finally:
        task = context.bot_data.get("learn_task")
        if task and not task.done():
            task.cancel()
        context.bot_data["learn_task"] = asyncio.create_task(
            _send_summary(context.bot, msg.chat_id, stats))
    raise ApplicationHandlerStop  # do not run normal recognition on this file


def register(app):
    if not OWNER_IDS:
        log.warning("learn_forward: no LEARN_OWNER_ID / ADMIN_IDS, handler disabled")
        return
    owner = filters.User(user_id=list(OWNER_IDS)) & filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("learn", cmd_learn, filters=owner), group=-1)
    app.add_handler(MessageHandler(owner & (filters.AUDIO | filters.Document.AUDIO), on_audio), group=-1)
    log.info("learn_forward: enabled for %d user(s)", len(OWNER_IDS))
